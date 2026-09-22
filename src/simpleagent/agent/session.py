"""会话状态：消息历史、用量，以及 JSONL 落盘。

落盘格式是 append-only 的 JSONL，每行一条**操作记录**而不是一条消息：

    {"op": "meta", "id": "...", "profile": "...", "cwd": "...", "created_at": "..."}
    {"op": "append", "message": {...}}
    {"op": "truncate", "n": 5}                     截断到前 5 条
    {"op": "replace", "changes": [[3, {...}]]}     原地替换第 3 条（清理旧工具结果，M6）
    {"op": "compact", "cut": 12, "messages": [...]} 前 12 条换成这几条（摘要压缩，M6）
    {"op": "stats", "requests": 3, "usage": {...}}

为什么不直接一行一条消息：Agent 被 Ctrl+C 打断时要「撤回」写进去的半条对话
（见 `Agent._repair`），`append` 之外需要一种表达撤销的手段。全量重写整个文件的话，
进程崩在写一半的时候就丢掉整份历史；追加一条 truncate 记录则任何时刻重放出来的
都是最后一次一致的状态——这也是 `spaces/` 那边 jsonl 用的同一个道理。

代价是文件会慢慢变长（撤回多了会有冗余），收益是永远不会写坏。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from simpleagent.agent.context import Calibration, ContextAnchor
from simpleagent.events import Usage

SESSION_DIRNAME = "sessions"  # 相对于数据目录：<home>/sessions/<id>.jsonl
TITLE_LIMIT = 40  # 列表里标题的字数上限


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _title(content: Any, limit: int = TITLE_LIMIT) -> str:
    """从一条用户消息里取列表用的标题。content 可能是字符串，也可能是多模态 block 列表。"""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    else:
        text = ""
    text = " ".join(text.split())  # 换行和连续空白压成一个空格
    return text if len(text) <= limit else text[:limit] + "…"


@dataclass(frozen=True)
class SessionInfo:
    """列表用的一份摘要：不含完整消息。"""

    id: str
    title: str = ""
    profile: str = ""
    cwd: str = ""
    created_at: str = ""
    updated_at: str = ""
    requests: int = 0
    messages: int = 0


@dataclass
class Session:
    """一次对话的全部状态。

    消息历史只允许通过 add / truncate / replace / compact 改动：这几个方法负责同步写盘，
    直接 `session.messages.append(...)` 会漏掉持久化，恢复时就会丢内容。
    """

    id: str
    messages: list[dict[str, Any]] = field(default_factory=list)  # 不含 system prompt
    usage: Usage = field(default_factory=Usage)
    requests: int = 0
    # 上次请求的实际输入用量，上下文预算拿它当基准。不落盘：恢复会话后请求一次就有了
    context_anchor: ContextAnchor | None = field(default=None, repr=False, compare=False)
    # 实际用量 ÷ 字符估算。历史改动只作废上面的基准、不作废它：没有基准时拿它修正估算
    context_calibration: Calibration | None = field(default=None, repr=False, compare=False)
    _store: SessionStore | None = field(default=None, repr=False, compare=False)

    def attach(self, store: SessionStore | None) -> None:
        """挂上存储之后，后续改动自动落盘；传 None 则变成纯内存会话。"""
        self._store = store

    @property
    def persisted(self) -> bool:
        return self._store is not None

    def add(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        self._log({"op": "append", "message": message})

    def add_many(self, messages: Iterable[dict[str, Any]]) -> None:
        for message in messages:
            self.add(message)

    def truncate(self, n: int) -> None:
        """截断到前 n 条消息。中断后撤回半条对话时用。"""
        if len(self.messages) > n:
            del self.messages[n:]
            self._log({"op": "truncate", "n": n})
        # 删到了上次请求覆盖的范围之内：那个实际用量不再对应现在的历史
        if self.context_anchor is not None and n < self.context_anchor.messages:
            self.context_anchor = None

    def replace(self, changes: dict[int, dict[str, Any]]) -> None:
        """原地替换几条消息，条数不变（清理旧工具结果用）。

        所有改动写成一条记录：要么全部生效，要么都没发生。记录里存替换后的完整消息，
        而不是「第几条要清」——占位符里有落盘路径，重放时算不出来。
        """
        if not changes:
            return
        for index, message in changes.items():
            self.messages[index] = message
        self._log({"op": "replace", "changes": [[i, changes[i]] for i in sorted(changes)]})
        # 改到了上次请求覆盖的范围之内：那个实际用量不再对应现在的历史
        if self.context_anchor is not None and min(changes) < self.context_anchor.messages:
            self.context_anchor = None

    def compact(self, cut: int, head: list[dict[str, Any]]) -> None:
        """前 cut 条消息换成 head（摘要）。一条记录完成替换，被压掉的原文还在前面的 append 行里。"""
        self.messages[:cut] = head
        self._log({"op": "compact", "cut": cut, "messages": head})
        self.context_anchor = None  # 整段前缀都变了

    def mark_sent(self, prompt_tokens: int, messages: int, model: str, estimate: int) -> None:
        """记下上次请求的实际输入用量：它精确覆盖了 system + 工具 + messages[:messages]。

        estimate 是同一段请求发出前按字符估的值，顺带更新校准系数。
        """
        self.context_anchor = ContextAnchor(prompt_tokens, messages, model, estimate)
        self.context_calibration = Calibration.of(self.context_anchor) or self.context_calibration

    def record_stats(self, usage: Usage | None = None, requests: int = 0) -> None:
        """累加用量。usage 为 None 时只累加请求次数。"""
        if usage is not None:
            self.usage += usage
        self.requests += requests
        self._log({"op": "stats", "requests": self.requests, "usage": asdict(self.usage)})

    def title(self) -> str:
        for message in self.messages:
            if message.get("role") == "user":
                return _title(message.get("content"))
        return ""

    def _log(self, record: dict[str, Any]) -> None:
        if self._store is not None:
            self._store.append(self.id, record)


class SessionStore:
    """会话的读写，`<root>/<id>.jsonl`。

    写盘失败一律静默：持久化是加分项，不能因为磁盘满了就让对话中断。
    """

    def __init__(self, root: Path, *, enabled: bool = True) -> None:
        self.root = Path(root)
        self.enabled = enabled

    def path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.jsonl"

    def append(self, session_id: str, record: dict[str, Any]) -> None:
        if not self.enabled:
            return
        path = self.path(session_id)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                # 上一行没写完（进程被 kill）时先补个换行，把损坏的行隔离掉，
                # 否则新记录会接在那半行后面，两条一起报废
                if path.stat().st_size and not self._ends_with_newline(path):
                    f.write("\n")
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    @staticmethod
    def _ends_with_newline(path: Path) -> bool:
        with path.open("rb") as f:
            f.seek(-1, 2)
            return f.read(1) == b"\n"

    def start(self, session: Session, *, profile: str = "", cwd: str = "") -> Session:
        """写 meta 行并给会话挂上存储。返回的还是同一个 Session。"""
        session.attach(self)
        self.append(
            session.id,
            {
                "op": "meta",
                "id": session.id,
                "profile": profile,
                "cwd": str(cwd),
                "created_at": _now(),
            },
        )
        return session

    def load(self, session_id: str) -> Session | None:
        """重放操作记录，恢复出一个 Session。会话不存在返回 None。"""
        path = self.path(session_id)
        if not path.exists():
            return None
        session = Session(session_id)
        for record in self._records(path):
            op = record.get("op")
            if op == "append":
                session.messages.append(record.get("message") or {})
            elif op == "truncate":
                n = int(record.get("n") or 0)
                del session.messages[n:]
            elif op == "replace":
                for index, message in record.get("changes") or []:
                    if 0 <= index < len(session.messages):
                        session.messages[index] = message
            elif op == "compact":
                session.messages[: int(record.get("cut") or 0)] = record.get("messages") or []
            elif op == "stats":
                session.requests = int(record.get("requests") or session.requests)
                if "usage" in record:
                    session.usage = Usage(**record["usage"])
        # 恢复完成之前不落盘，否则重放过程会被写成一批新的 append 记录
        session.attach(self)
        return session

    def latest(self) -> str | None:
        """最近改动过的会话 id。"""
        try:
            paths = sorted(self.root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return None
        return paths[0].stem if paths else None

    def list(self, limit: int = 20) -> list[SessionInfo]:
        """按修改时间倒序列出会话摘要（最近改动的在最前）。"""
        try:
            paths = sorted(self.root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return []
        return [self._summarize(p) for p in paths[:limit]]

    def delete(self, session_id: str) -> bool:
        try:
            self.path(session_id).unlink(missing_ok=True)
        except OSError:
            return False
        return True

    def _summarize(self, path: Path) -> SessionInfo:
        meta: dict[str, Any] = {}
        title = ""
        requests = 0
        count = 0
        for record in self._records(path):
            op = record.get("op")
            if op == "meta" and not meta:
                meta = record
            elif op == "stats":
                requests = int(record.get("requests") or 0)
            elif op == "append":
                count += 1
                if not title and (record.get("message") or {}).get("role") == "user":
                    title = _title((record.get("message") or {}).get("content"))
        try:
            updated = (
                datetime.fromtimestamp(path.stat().st_mtime)
                .astimezone()
                .isoformat(timespec="seconds")
            )
        except OSError:
            updated = ""
        return SessionInfo(
            id=path.stem,
            title=title,
            profile=str(meta.get("profile") or ""),
            cwd=str(meta.get("cwd") or ""),
            created_at=str(meta.get("created_at") or ""),
            updated_at=updated,
            requests=requests,
            messages=count,
        )

    @staticmethod
    def _records(path: Path) -> Iterable[dict[str, Any]]:
        """逐行读出 JSON 记录；坏行跳过不动（崩了一半的行别把整个会话带走）。"""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return []
        records = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records
