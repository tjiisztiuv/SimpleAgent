"""SpaceStore：空间与会话的持久化与查询。

落盘布局（在 <SIMPLEAGENT_HOME>/spaces/ 下）：
    <space-id>/space.toml          空间定义（唯一真值）
    <space-id>/tmp/                没有固定 cwd 的空间的工作目录
    <space-id>/sessions/<sid>.jsonl   消息流，只追加：界面显示的是实际发生过什么
    <space-id>/sessions/<sid>.meta.json  会话元信息（标题/状态/验证），可变
    <space-id>/sessions/model/<sid>.jsonl  模型看到的历史（M6）：agent/session.py 的操作记录，
                                        清理旧工具结果、摘要压缩、中断修复都在这里

不用额外依赖：TOML 用标准库 tomllib 读、自己拼字符串写；JSON 用标准库 json。
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any

from simpleagent.agents.base import SAFE
from simpleagent.config import home_dir
from simpleagent.spaces.models import (
    COMMAND_SPACE_ID,
    COMMAND_SPACE_NAME,
    DEFAULT_CLI_COMMAND,
    AgentBinding,
    GenericConfig,
    SessionMeta,
    Space,
    SpaceSpec,
    VerifyConfig,
    _now,
    clean_description,
    normalize_permission,
    validate_executor,
)

SPACES_DIRNAME = "spaces"
# update_space 能逐个改的字段。「谁跑」那组（executor 等）不在这里，走 change_executor
UPDATABLE_FIELDS = frozenset(
    {"name", "description", "profile", "opened", "pinned", "last_opened_at", "keep_sessions"}
)


def new_id(prefix: str) -> str:
    import time

    return f"{prefix}_{int(time.time() * 1000)}_{os.urandom(3).hex()}"


def _write_atomic(path: Path, text: str) -> None:
    """先写临时文件再 os.replace 替换，读者只会看到旧内容或新内容。

    serve 里 HTTP 线程读 meta / space.toml 时，runner 线程可能正在写；直接 write_text
    会先清空文件，读者读到空文件就 JSONDecodeError。临时文件名不能匹配 *.meta.json。
    """
    tmp = path.with_name(f"{path.name}.{os.urandom(3).hex()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _toml_str(s: str) -> str:
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'


def _space_to_toml(space: Space) -> str:
    out: list[str] = [
        f"id = {_toml_str(space.id)}",
        f"name = {_toml_str(space.name)}",
        f"kind = {_toml_str(space.kind)}",
        f"executor = {_toml_str(space.executor)}",
        f"profile = {_toml_str(space.profile)}",
    ]
    if space.description:
        out.insert(2, f"description = {_toml_str(space.description)}")
    if space.cli_model:
        out.append(f"cli_model = {_toml_str(space.cli_model)}")
    if space.permission:  # 没单独设过就不写，读回来还是「跟配置默认」
        out.append(f"permission = {_toml_str(space.permission)}")
    if space.kind == "agent" and space.cwd:
        out.append(f"cwd = {_toml_str(space.cwd)}")
    out += [
        f"opened = {str(space.opened).lower()}",
        f"pinned = {str(space.pinned).lower()}",
        f"created_at = {_toml_str(space.created_at)}",
        f"last_opened_at = {_toml_str(space.last_opened_at)}",
        f"keep_sessions = {space.keep_sessions}",
    ]
    # [generic] 和 [agent] 是两张独立的表，可以同时存在（通用任务 + 外部 agent）
    if space.kind == "generic" and space.generic:
        out += ["", "[generic]", f"tmp_dir = {_toml_str(space.generic.tmp_dir)}"]
    if space.executor != "simpleagent" and space.agent:
        a = space.agent
        out += [
            "",
            "[agent]",
            f"command = {_toml_str(a.command)}",
            f"args = {json.dumps(a.args, ensure_ascii=False)}",
        ]
        if a.resume_flag:
            out.append(f"resume_flag = {_toml_str(a.resume_flag)}")
    if space.verify and space.verify.command:
        v = space.verify
        out += [
            "",
            "[verify]",
            f"command = {_toml_str(v.command)}",
            f"trigger = {_toml_str(v.trigger)}",
            f"timeout = {v.timeout}",
        ]
    return "\n".join(out) + "\n"


def _load_permission(value: Any) -> str | None:
    """space.toml 里的 permission。旧文件没有这个字段（内置执行者以前不写）就是 None；
    手改写错了按只读处理，不让整个空间从列表里消失，也宁可少给权限。"""
    try:
        return normalize_permission(value if isinstance(value, str) else None)
    except ValueError:
        return SAFE


def _space_from_toml(data: dict[str, Any], space_id: str) -> Space:
    # 兼容 W1~W5 写下的旧文件：那时执行者叫 [agent].name，cwd 也藏在 [agent] 里
    agent_data = data.get("agent") or {}
    executor = data.get("executor") or agent_data.get("name") or "simpleagent"
    return Space(
        id=space_id,
        name=data.get("name", space_id),
        kind=data.get("kind", "generic"),
        description=data.get("description", ""),
        executor=executor,
        profile=data.get("profile", "default"),
        cli_model=data.get("cli_model") or None,
        permission=_load_permission(data.get("permission")),
        cwd=data.get("cwd") or agent_data.get("cwd"),
        opened=bool(data.get("opened", True)),
        pinned=bool(data.get("pinned", False)),
        created_at=data.get("created_at", ""),
        last_opened_at=data.get("last_opened_at", ""),
        keep_sessions=int(data.get("keep_sessions", 50)),
        generic=GenericConfig.from_dict(data["generic"]) if data.get("generic") else None,
        agent=AgentBinding.from_dict(agent_data) if agent_data else None,
        verify=VerifyConfig.from_dict(data["verify"]) if data.get("verify") else None,
    )


def _user_text(msg: dict) -> str:
    content = msg.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "".join(parts).strip()
    return ""


class SpaceStore:
    """空间与会话的读写入口。"""

    def __init__(self, home: Path | None = None):
        self.home = Path(home or home_dir())
        self.spaces_dir = self.home / SPACES_DIRNAME

    # ----- 路径 -----
    def _space_dir(self, space_id: str) -> Path:
        return self.spaces_dir / space_id

    def _space_toml(self, space_id: str) -> Path:
        return self._space_dir(space_id) / "space.toml"

    def _sessions_dir(self, space_id: str) -> Path:
        return self._space_dir(space_id) / "sessions"

    def _session_jsonl(self, space_id: str, session_id: str) -> Path:
        return self._sessions_dir(space_id) / f"{session_id}.jsonl"

    def _session_meta(self, space_id: str, session_id: str) -> Path:
        return self._sessions_dir(space_id) / f"{session_id}.meta.json"

    # ----- 空间 -----
    def list_spaces(self, opened_only: bool = True) -> list[Space]:
        if not self.spaces_dir.exists():
            return []
        spaces: list[Space] = []
        for d in sorted(self.spaces_dir.iterdir()):
            if not d.is_dir():
                continue
            toml = d / "space.toml"
            if not toml.exists():
                continue
            try:
                with toml.open("rb") as f:
                    data = tomllib.load(f)
            except tomllib.TOMLDecodeError:
                continue
            sp = _space_from_toml(data, d.name)
            if opened_only and not sp.opened:
                continue
            spaces.append(sp)
        spaces.sort(key=lambda s: s.last_opened_at or "", reverse=True)
        return spaces

    def get_space(self, space_id: str) -> Space | None:
        toml = self._space_toml(space_id)
        if not toml.exists():
            return None
        with toml.open("rb") as f:
            data = tomllib.load(f)
        return _space_from_toml(data, space_id)

    def create_space(self, spec: SpaceSpec) -> Space:
        # 先构造再落盘：from_spec 会校验组合是否合法，非法时不该留下半个空间目录
        space = Space.from_spec(spec, new_id("sp"))
        sd = self._space_dir(space.id)
        sd.mkdir(parents=True, exist_ok=True)
        # 没有 cwd 的空间（kind=generic，或将来允许的空目录）工作目录落在 tmp，先建好
        if not space.cwd:
            (sd / "tmp").mkdir(exist_ok=True)
        self._sessions_dir(space.id).mkdir(exist_ok=True)
        self._write_space_toml(space)
        return space

    def ensure_command_space(self) -> Space:
        """指挥台调度者住的系统空间：不存在就建一个。`sa serve` 启动时调。

        opened=False，左栏不显示；执行者固定是内置 loop（调度工具只有内置 loop 能用）。
        用哪个模型不看这里的 profile，看 config.toml 的 [command]，改配置不用动这个文件。
        """
        space = self.get_space(COMMAND_SPACE_ID)
        if space is not None:
            return space
        now = _now()
        space = Space(
            id=COMMAND_SPACE_ID,
            name=COMMAND_SPACE_NAME,
            kind="generic",
            description="调度者：把指挥台收到的任务派给合适的空间，再汇总结果",
            opened=False,
            created_at=now,
            last_opened_at=now,
            generic=GenericConfig(tmp_dir="auto"),
        )
        sd = self._space_dir(space.id)
        (sd / "tmp").mkdir(parents=True, exist_ok=True)
        self._sessions_dir(space.id).mkdir(exist_ok=True)
        self._write_space_toml(space)
        return space

    def _write_space_toml(self, space: Space) -> None:
        _write_atomic(self._space_toml(space.id), _space_to_toml(space))

    def update_space(self, space_id: str, **fields: Any) -> Space:
        space = self.get_space(space_id)
        if space is None:
            raise KeyError(f"空间不存在: {space_id}")
        for k, v in fields.items():
            if k not in UPDATABLE_FIELDS:
                raise ValueError(f"不能修改字段 {k}")
            if k == "description":
                v = clean_description(v)
            setattr(space, k, v)
        self._write_space_toml(space)
        return space

    def change_executor(
        self,
        space_id: str,
        executor: str,
        *,
        profile: str | None = None,
        cli_model: str | None = None,
        permission: str | None = None,
        command: str | None = None,
        args: list[str] | None = None,
    ) -> Space:
        """切换空间的执行者。只影响之后新建的会话：老会话的执行者锁在 meta.agent 里。

        不走 update_space 的逐字段 setattr：executor / cli_model / permission / [agent]
        必须一起变，否则会落下「内置执行者还挂着 [agent] 段」这种半新半旧的状态。
        """
        space = self.get_space(space_id)
        if space is None:
            raise KeyError(f"空间不存在: {space_id}")
        permission = normalize_permission(permission)
        validate_executor(executor, cli_model=cli_model, permission=permission, command=command)
        # 权限模式两种执行者都有；没给就回到「没单独设过」（跟配置默认 / 外部 CLI 只读）
        space.permission = permission
        if executor == "simpleagent":
            # 外部 CLI 的那几项全部清掉，免得 space.toml 里留着不生效的配置
            space.agent = None
            space.cli_model = None
        else:
            # 执行者没变、也没指定新命令（比如只改权限）：保留手写的启动命令和参数
            if space.executor == executor and space.agent and command is None and args is None:
                binding = space.agent
            else:
                binding = AgentBinding(
                    command=command or DEFAULT_CLI_COMMAND[executor], args=list(args or [])
                )
            space.agent = binding
            space.cli_model = cli_model
        # profile 两种执行者都存着：切到外部 CLI 时也记下来，切回内置时接着用
        if profile:
            space.profile = profile
        space.executor = executor
        self._write_space_toml(space)
        return space

    def close_space(self, space_id: str) -> None:
        """只从“打开的空间”列表移除，不删数据。"""
        self.update_space(space_id, opened=False)

    def delete_space(self, space_id: str) -> None:
        import shutil

        sd = self._space_dir(space_id)
        if sd.exists():
            shutil.rmtree(sd)

    # ----- 会话 -----
    def list_sessions(
        self, space_id: str, limit: int = 5, include_pinned: bool = True
    ) -> list[SessionMeta]:
        """最近 N 个 session。pin 和 running 永远在列表里、不占 N 个名额。"""
        sdir = self._sessions_dir(space_id)
        if not sdir.exists():
            return []
        metas: list[SessionMeta] = []
        for m in sorted(sdir.glob("*.meta.json")):
            data = json.loads(m.read_text(encoding="utf-8"))
            metas.append(SessionMeta.from_dict(data))

        if include_pinned:
            pinned = [s for s in metas if s.pinned]
            non_pinned = [s for s in metas if not s.pinned]
        else:
            pinned = []
            non_pinned = list(metas)

        running = [s for s in non_pinned if s.status == "running"]
        others = sorted(
            (s for s in non_pinned if s.status != "running"),
            key=lambda s: s.updated_at or "",
            reverse=True,
        )
        return list(pinned) + running + others[:limit]

    def create_session(
        self, space_id: str, agent: str | None = None, *, parent: str | None = None
    ) -> SessionMeta:
        """parent：指挥台派发的子会话记下调度者的会话 id，面板靠它把卡片挂到调度者下面。"""
        space = self.get_space(space_id)
        if space is None:
            raise KeyError(f"空间不存在: {space_id}")
        session_id = new_id("se")
        now = _now()
        meta = SessionMeta(
            id=session_id,
            space_id=space_id,
            status="idle",
            agent=agent or space.executor,
            parent_session_id=parent,
            created_at=now,
            updated_at=now,
        )
        self._sessions_dir(space_id).mkdir(parents=True, exist_ok=True)
        self._write_meta(space_id, meta)
        self._session_jsonl(space_id, session_id).write_text("", encoding="utf-8")
        return meta

    def get_session_meta(self, space_id: str, session_id: str) -> SessionMeta | None:
        m = self._session_meta(space_id, session_id)
        if not m.exists():
            return None
        return SessionMeta.from_dict(json.loads(m.read_text(encoding="utf-8")))

    def find_session_space(self, session_id: str) -> str | None:
        """跨空间定位某个 session 属于哪个空间（session id 全局唯一）。

        用于按 session id 直接访问（GET /api/sessions/{id}）而不必带上 space id。
        """
        if not self.spaces_dir.exists():
            return None
        for d in self.spaces_dir.iterdir():
            if not d.is_dir():
                continue
            if (d / "sessions" / f"{session_id}.meta.json").exists():
                return d.name
        return None

    def _write_meta(self, space_id: str, meta: SessionMeta) -> None:
        _write_atomic(
            self._session_meta(space_id, meta.id),
            json.dumps(meta.to_dict(), ensure_ascii=False, indent=2),
        )

    def load_session(self, space_id: str, session_id: str):
        """重建 Session（消息历史 + 用量）。依赖 agent/session.py。"""
        from simpleagent.agent.session import Session
        from simpleagent.events import Usage

        jsonl = self._session_jsonl(space_id, session_id)
        messages: list[dict] = []
        if jsonl.exists():
            for line in jsonl.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    messages.append(json.loads(line))
        meta = self.get_session_meta(space_id, session_id)
        usage = Usage(
            **{
                k: (meta.usage.get(k, 0) if meta else 0)
                for k in ("prompt_tokens", "completion_tokens", "cached_tokens", "reasoning_tokens")
            }
        )
        return Session(id=session_id, messages=messages, usage=usage, requests=len(messages))

    def model_sessions(self, space_id: str):
        """模型看到的历史存在 sessions/model/<sid>.jsonl，读写交给 SessionStore。"""
        from simpleagent.agent.session import SessionStore

        return SessionStore(self._sessions_dir(space_id) / "model")

    def load_model_session(self, space_id: str, session_id: str):
        """内置 agent 用的 Session：模型看到的历史。

        和展示用的 jsonl 分开：展示那份是按事件镜像的，清理、压缩、中断修复都不产生消息事件，
        从它重建历史的话，这些改动每次输入都会丢——压缩还得每轮重新调一次 LLM，中断后留下的
        悬空 tool_call 会让下一次请求被拒。这份走 Session 的方法，改动随手落盘。

        第一次用到时从展示用的 jsonl 迁移：顺手补上悬空的 tool_call，以前被中断弄坏的会话
        也能接着用；用量从 meta 接过来，接着累计。
        """
        from simpleagent.agent.loop import fill_missing_results
        from simpleagent.agent.session import Session

        store = self.model_sessions(space_id)
        session = store.load(session_id)
        if session is not None:
            return session
        legacy = self.load_session(space_id, session_id)
        space = self.get_space(space_id)
        session = store.start(Session(session_id), profile=space.profile if space else "")
        session.add_many(fill_missing_results(legacy.messages))
        session.record_stats(legacy.usage)
        return session

    def append_message(self, space_id: str, session_id: str, msg: dict) -> None:
        jsonl = self._session_jsonl(space_id, session_id)
        jsonl.parent.mkdir(parents=True, exist_ok=True)
        with jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        meta = self.get_session_meta(space_id, session_id)
        if meta is None:
            return
        meta.updated_at = _now()
        # 默认标题取第一条用户消息截断 40 字
        if meta.title == "新会话" and msg.get("role") == "user":
            text = _user_text(msg)
            if text:
                meta.title = text[:40]
        self._write_meta(space_id, meta)

    def update_meta(self, space_id: str, session_id: str, **fields: Any) -> SessionMeta:
        meta = self.get_session_meta(space_id, session_id)
        if meta is None:
            raise KeyError(f"会话不存在: {session_id}")
        allowed = {
            "title",
            "status",
            "pinned",
            "agent",
            "agent_session_id",
            "dispatched_by",
            "usage",
            "verification",
        }
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"不能修改字段 {k}")
            setattr(meta, k, v)
        meta.updated_at = _now()
        self._write_meta(space_id, meta)
        return meta
