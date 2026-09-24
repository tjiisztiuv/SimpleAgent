"""HTTP + SSE 服务：把 Runner / 总线 / 存储 暴露成设计文档 6.2 的本地 API。

技术选型走文档里的 A 方案：标准库 http.server + 手写路由 + 手写 SSE，零新依赖。
客户端→服务端都是普通请求（发消息 / 取消 / 审批），服务端→客户端是单向 SSE 事件流，
断线重连用 SSE 原生的 Last-Event-ID 即可。
"""

from __future__ import annotations

import json
import queue
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from simpleagent.agents.base import PERMISSION_LABELS, PERMISSIONS, SAFE
from simpleagent.config import Config
from simpleagent.panel.store import PanelStore
from simpleagent.panel.summary import one_line, summarize
from simpleagent.serve.bus import frame_to_sse
from simpleagent.serve.runner import Runner
from simpleagent.serve.static import asset_bytes
from simpleagent.spaces.models import EXECUTOR_LABELS, EXECUTORS, SpaceSpec, locked_reason
from simpleagent.spaces.store import UPDATABLE_FIELDS, SpaceStore
from simpleagent.tools.walk import IGNORED_DIRS

# SSE 空闲时多久发一次 keepalive 注释帧。它同时承担「探测客户端是否还活着」的职责：
# 写入失败是服务端发现对方已经走了的唯一信号（TCP 不会主动告诉我们）。
KEEPALIVE_INTERVAL = 15.0

# 控制面板的"近期活动流"：任务跑完不在面板上立刻消失，而是在 recent 里留存一段时间。
# 不留存的话，用户看到的是"列表少了一条"而不是"这条跑完了"，等于看不到状态变化。
RECENT_DONE_LIMIT = 10
RECENT_DONE_WINDOW_MINUTES = 24 * 60
FINAL_STATUSES = frozenset({"done", "error", "cancelled"})


def _within_window(ts: str, cutoff: float) -> bool:
    """时间戳是否晚于 cutoff。解析不出来就当它不在窗口内，别让脏数据把接口打挂。"""
    try:
        return datetime.fromisoformat(ts).timestamp() > cutoff
    except (TypeError, ValueError):
        return False


def _verification_status(meta: Any) -> str:
    """取验证状态。meta 里既可能是 Verification 对象，也可能是手写进去的 dict。"""
    v = getattr(meta, "verification", None)
    if isinstance(v, dict):
        return v.get("status", "unknown")
    return getattr(v, "status", "unknown")


class Response:
    def __init__(
        self,
        status: int,
        body: Any = None,
        headers: dict[str, str] | None = None,
        stream: Iterator[str] | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.stream = stream  # SSE 时为帧生成器，否则为 None


class Server:
    def __init__(
        self,
        config: Config,
        *,
        store: SpaceStore | None = None,
        runner: Runner | None = None,
        llm_factory=None,
    ) -> None:
        self.config = config
        self.store = store or SpaceStore()
        self.panel = PanelStore(archive_after_minutes=config.panel.archive_after_minutes)
        self.runner = runner or Runner(config, store=self.store, llm_factory=llm_factory)

    def start(self) -> None:
        self.runner.start()

    # ------------------------------------------------------------------- 路由
    def handle(self, method: str, path: str, headers: dict[str, str], body: bytes) -> Response:
        # 去掉 query string
        path_only = urlparse(path).path
        # 前端页面与静态资源（W3）：放在最前面，避免被 /api 之外的兜底 404 吃掉
        if method == "GET" and path_only in ("/", "/index.html"):
            return self._static("index.html")
        m = re.match(r"^/assets/([^/]+)$", path_only)
        if m and method == "GET":
            return self._static(m.group(1))
        if method == "GET" and path_only == "/api/meta":
            return self._meta()
        if method == "GET" and path_only == "/api/spaces":
            return self._list_spaces(headers)
        if method == "POST" and path_only == "/api/spaces":
            return self._create_space(body)
        m = re.match(r"^/api/spaces/([^/]+)$", path_only)
        if m:
            space_id = m.group(1)
            if method == "GET":
                return self._space_detail(space_id)
            if method == "PATCH":
                return self._update_space(space_id, body)
            if method == "DELETE":
                return self._delete_space(space_id)
        m = re.match(r"^/api/spaces/([^/]+)/sessions$", path_only)
        if m:
            space_id = m.group(1)
            if method == "GET":
                return self._list_sessions(space_id, headers)
            if method == "POST":
                return self._create_session(space_id, body)
        m = re.match(r"^/api/spaces/([^/]+)/files$", path_only)
        if m and method == "GET":
            return self._space_files(m.group(1))
        m = re.match(r"^/api/sessions/([^/]+)$", path_only)
        if m and method == "GET":
            return self._session_messages(m.group(1))
        m = re.match(r"^/api/sessions/([^/]+)$", path_only)
        if m and method == "PATCH":
            return self._update_session(m.group(1), body)
        m = re.match(r"^/api/sessions/([^/]+)/input$", path_only)
        if m and method == "POST":
            return self._session_input(m.group(1), body)
        m = re.match(r"^/api/sessions/([^/]+)/cancel$", path_only)
        if m and method == "POST":
            return self._session_cancel(m.group(1))
        m = re.match(r"^/api/sessions/([^/]+)/rerun$", path_only)
        if m and method == "POST":
            return self._session_rerun(m.group(1))
        m = re.match(r"^/api/sessions/([^/]+)/verify$", path_only)
        if m and method == "POST":
            return self._session_verify(m.group(1))
        m = re.match(r"^/api/sessions/([^/]+)/verification$", path_only)
        if m and method == "PATCH":
            return self._mark_verified(m.group(1), body)
        m = re.match(r"^/api/sessions/([^/]+)/events$", path_only)
        if m and method == "GET":
            return self._sse(m.group(1), headers)
        if method == "GET" and path_only == "/api/approvals":
            return self._list_approvals()
        m = re.match(r"^/api/approvals/([^/]+)$", path_only)
        if m and method == "POST":
            return self._resolve_approval(m.group(1), body)
        if method == "GET" and path_only == "/api/panel/summary":
            return self._panel_summary()
        m = re.match(r"^/api/sessions/([^/]+)/summary$", path_only)
        if m and method == "GET":
            return self._session_summary(m.group(1))
        if method == "GET" and path_only == "/api/inbox":
            return self._inbox_list(headers)
        if method == "POST" and path_only == "/api/inbox":
            return self._inbox_add(body)
        # count 要排在 /api/inbox/{id} 前面，不然会被当成一条 id 叫 count 的消息
        if method == "GET" and path_only == "/api/inbox/count":
            return Response(200, self.panel.counts())
        m = re.match(r"^/api/inbox/([^/]+)$", path_only)
        if m and method == "GET":
            return self._inbox_detail(m.group(1))
        m = re.match(r"^/api/inbox/([^/]+)/read$", path_only)
        if m and method == "POST":
            return self._inbox_read(m.group(1))
        m = re.match(r"^/api/inbox/([^/]+)/archive$", path_only)
        if m and method == "POST":
            return self._inbox_archive(m.group(1))
        if path_only == "/api/todos":
            if method == "GET":
                return self._todo_list()
            if method == "POST":
                return self._todo_add(body)
        m = re.match(r"^/api/todos/([^/]+)$", path_only)
        if m:
            if method == "PATCH":
                return self._todo_update(m.group(1), body)
            if method == "DELETE":
                return self._todo_delete(m.group(1))
        return Response(404, {"error": "not found", "path": path_only})

    # ------------------------------------------------------------ 静态资源 / 元信息
    def _static(self, name: str) -> Response:
        got = asset_bytes(name)
        if got is None:
            return Response(404, {"error": "asset not found", "name": name})
        data, content_type = got
        return Response(
            200,
            body=data,
            headers={"Content-Type": content_type, "Content-Length": str(len(data))},
        )

    def _meta(self) -> Response:
        """前端启动时要知道的东西：有哪些 profile、默认哪个、能选哪些执行者、max_steps。

        executors 由后端给，前端不硬编码 agent 名单——以后加外部 agent 的模型 preset，
        只改这里的数据，不用动 app.js。
        """
        profiles = sorted(self.config.profiles)
        executors = [
            {
                "name": name,
                "label": EXECUTOR_LABELS[name],
                "external": name != "simpleagent",
                # 外部 CLI 的模型本次只支持「本机默认（不注入配置）」，所以是空列表；
                # 权限档两份，向导里的下拉直接照这个渲染
                "models": [] if name != "simpleagent" else profiles,
                "permissions": []
                if name == "simpleagent"
                else [{"name": p, "label": PERMISSION_LABELS[p]} for p in PERMISSIONS],
                "default_permission": SAFE,
            }
            for name in EXECUTORS
        ]
        return Response(
            200,
            {
                "profiles": profiles,
                "default_profile": self.config.default_profile,
                "executors": executors,
                "max_steps": self.config.max_steps,
            },
        )

    # ----------------------------------------------------------------- 空间
    def _list_spaces(self, headers: dict[str, str]) -> Response:
        opened_only = "all" not in headers.get("x-flags", "") and "all=1" not in headers.get(
            "x-query", ""
        )
        spaces = self.store.list_spaces(opened_only=opened_only)
        return Response(200, [self._space_view(s) for s in spaces])

    def _space_view(self, space) -> dict[str, Any]:
        data = space.to_dict()
        data["sessions"] = [m.to_dict() for m in self.store.list_sessions(space.id, limit=5)]
        return data

    def _create_space(self, body: bytes) -> Response:
        try:
            data = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return Response(400, {"error": "invalid json"})
        allowed = {
            "name",
            "kind",
            "executor",
            "profile",
            "cli_model",
            "permission",
            "pin_dir",
            "cwd",
            "command",
            "args",
            "verify_command",
            "verify_trigger",
            "verify_timeout",
        }
        try:
            spec = SpaceSpec(**{k: v for k, v in data.items() if k in allowed})
            space = self.store.create_space(spec)
        except (TypeError, ValueError) as e:
            # 组合非法（如绑定目录却不给 cwd）要变成 400，不能让它逃出去把连接掐断
            return Response(400, {"error": f"参数错误：{e}"})
        return Response(201, self._space_view(space))

    def _space_detail(self, space_id: str) -> Response:
        space = self.store.get_space(space_id)
        if space is None:
            return Response(404, {"error": "space not found"})
        return Response(200, self._space_view(space))

    def _update_space(self, space_id: str, body: bytes) -> Response:
        try:
            data = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return Response(400, {"error": "invalid json"})
        if not isinstance(data, dict):
            return Response(400, {"error": "body 必须是 JSON 对象"})
        current = self.store.get_space(space_id)
        if current is None:
            return Response(404, {"error": "space not found"})
        profile = data.get("profile")
        if profile is not None and profile not in self.config.profiles:
            return Response(
                400,
                {"error": f"profile「{profile}」不在 config.toml 里"},
            )
        # 「谁跑」这组字段必须和 executor 一起提交，由 change_executor 一次改完；
        # 单改 permission 之类会落下半新半旧的状态
        exec_keys = {"executor", "cli_model", "permission", "command", "args"}
        exec_fields = {k: data.pop(k) for k in list(data) if k in exec_keys}
        # 先把字段全部查一遍再动手：不然执行者已经切了，后面的字段才报错，落下改了一半的空间
        unknown = set(data) - UPDATABLE_FIELDS
        if unknown:
            return Response(400, {"error": f"不能修改字段 {'、'.join(sorted(unknown))}"})
        if exec_fields and "executor" not in exec_fields:
            return Response(
                400, {"error": "改 cli_model / permission / command / args 要带上 executor"}
            )
        try:
            if exec_fields:
                # 只有真换执行者才要等会话停下。「空间设置」保存时总会带上 executor，
                # 执行者没变（只改名、改权限）就不拦：跑着的那轮已经按旧配置拉起来了
                if exec_fields["executor"] != current.executor and self.runner.space_busy(space_id):
                    return Response(409, {"error": "这个空间还有会话在跑，停下来再切执行者"})
                self.store.change_executor(
                    space_id,
                    exec_fields.pop("executor"),
                    profile=data.pop("profile", None),
                    permission=exec_fields.pop("permission", None) or SAFE,
                    **exec_fields,
                )
            space = (
                self.store.update_space(space_id, **data)
                if data
                else self.store.get_space(space_id)
            )
        except (KeyError, TypeError, ValueError) as e:
            return Response(400, {"error": str(e)})
        return Response(200, self._space_view(space))

    def _delete_space(self, space_id: str) -> Response:
        self.store.delete_space(space_id)
        return Response(200, {"deleted": space_id})

    # ----------------------------------------------------------------- 会话
    def _list_sessions(self, space_id: str, headers: dict[str, str]) -> Response:
        if self.store.get_space(space_id) is None:
            return Response(404, {"error": "space not found"})
        metas = self.store.list_sessions(space_id, limit=self._limit(headers))
        return Response(200, [m.to_dict() for m in metas])

    @staticmethod
    def _limit(headers: dict[str, str]) -> int:
        """从 query 取 limit：默认 5（左栏只显示最近 5 条），上限 200（「查看全部」用）。"""
        raw = parse_qs(headers.get("x-query", "")).get("limit", [""])[0]
        try:
            n = int(raw)
        except ValueError:
            return 5
        return max(1, min(n, 200))

    def _create_session(self, space_id: str, body: bytes) -> Response:
        if self.store.get_space(space_id) is None:
            return Response(404, {"error": "space not found"})
        data = self._safe_json(body)
        agent = (data or {}).get("agent")
        meta = self.store.create_session(space_id, agent=agent)
        return Response(201, meta.to_dict())

    def _session_messages(self, session_id: str) -> Response:
        space_id = self.store.find_session_space(session_id)
        if space_id is None:
            return Response(404, {"error": "session not found"})
        session = self.store.load_session(space_id, session_id)
        return Response(200, {"session_id": session_id, "messages": session.messages})

    def _update_session(self, session_id: str, body: bytes) -> Response:
        """改会话的可变元信息：目前开放重命名（title）与置顶（pinned）。"""
        space_id = self.store.find_session_space(session_id)
        if space_id is None:
            return Response(404, {"error": "session not found"})
        data = self._safe_json(body) or {}
        fields = {k: v for k, v in data.items() if k in ("title", "pinned")}
        if not fields:
            return Response(400, {"error": "只能改 title / pinned"})
        if "title" in fields and not str(fields["title"]).strip():
            return Response(400, {"error": "title 不能为空"})
        try:
            meta = self.store.update_meta(space_id, session_id, **fields)
        except (KeyError, ValueError) as e:
            return Response(400, {"error": str(e)})
        return Response(200, meta.to_dict())

    def _session_input(self, session_id: str, body: bytes) -> Response:
        space_id = self.store.find_session_space(session_id)
        if space_id is None:
            return Response(404, {"error": "session not found"})
        data = self._safe_json(body) or {}
        text = data.get("text")
        if not text or not str(text).strip():
            return Response(400, {"error": "text 不能为空"})
        if reason := self._locked(space_id, session_id):
            return Response(409, {"error": reason})
        self.runner.run_input(space_id, session_id, str(text))
        return Response(202, {"accepted": True})

    def _space_files(self, space_id: str) -> Response:
        """工作目录的文件树（右栏「文件」tab）。只看两层，够定位文件就行。"""
        space = self.store.get_space(space_id)
        if space is None:
            return Response(404, {"error": "space not found"})
        cwd = self.runner.cwd_for(space)
        return Response(200, {"cwd": str(cwd), "tree": file_tree(cwd, depth=2)})

    def _session_rerun(self, session_id: str) -> Response:
        """重跑最后一条用户消息：改了 prompt / 换了模型后想再试一次时用。"""
        space_id = self.store.find_session_space(session_id)
        if space_id is None:
            return Response(404, {"error": "session not found"})
        messages = self.store.load_session(space_id, session_id).messages
        last = next((m for m in reversed(messages) if m.get("role") == "user"), None)
        if not last:
            return Response(400, {"error": "这个会话还没有用户消息，没什么可重跑的"})
        text = last.get("content")
        if not isinstance(text, str) or not text.strip():
            return Response(400, {"error": "最后一条用户消息不是纯文本，无法重跑"})
        if reason := self._locked(space_id, session_id):
            return Response(409, {"error": reason})
        self.runner.run_input(space_id, session_id, text)
        return Response(202, {"accepted": True, "text": text})

    def _locked(self, space_id: str, session_id: str) -> str | None:
        """会话被锁住（空间中途切过执行者）时返回原因。runner 里还有一道同样的检查，
        这里先挡一次是为了让前端同步拿到 409，而不是等 SSE 里的 error 帧。"""
        space = self.store.get_space(space_id)
        meta = self.store.get_session_meta(space_id, session_id)
        if space is None or meta is None or meta.agent == space.executor:
            return None
        return locked_reason(meta.agent, space.executor)

    def _session_cancel(self, session_id: str) -> Response:
        self.runner.cancel(session_id)
        return Response(200, {"cancelled": session_id})

    def _session_verify(self, session_id: str) -> Response:
        space_id = self.store.find_session_space(session_id)
        if space_id is None:
            return Response(404, {"error": "session not found"})
        self.runner.verify(space_id, session_id)
        return Response(202, {"accepted": True})

    def _mark_verified(self, session_id: str, body: bytes) -> Response:
        space_id = self.store.find_session_space(session_id)
        if space_id is None:
            return Response(404, {"error": "session not found"})
        data = self._safe_json(body) or {}
        verification = {
            "status": data.get("status", "passed"),
            "command": data.get("command"),
            "exit_code": data.get("exit_code"),
            "source": "manual",
        }
        self.store.update_meta(space_id, session_id, verification=verification)
        return Response(200, {"verification": verification})

    # ----------------------------------------------------------------- SSE
    def _sse(self, session_id: str, headers: dict[str, str]) -> Response:
        # 浏览器断线自动重连时会带 Last-Event-ID 头；前端主动续传（标签页切回前台时重新
        # 订阅）没法给 EventSource 设头，只能走 query。两者都有时以头为准，它更新。
        last_event_id = (
            headers.get("last-event-id")
            or parse_qs(headers.get("x-query", "")).get("last_event_id", [None])[0]
        )
        q, replay = self.runner.bus.subscribe(session_id, last_event_id)

        def stream() -> Iterator[str]:
            try:
                for frame in replay:
                    yield frame_to_sse(frame)
                while True:
                    try:
                        frame = q.get(timeout=KEEPALIVE_INTERVAL)
                    except queue.Empty:
                        yield ": keepalive\n\n"
                        continue
                    yield frame_to_sse(frame)
            finally:
                self.runner.bus.unsubscribe(session_id, q)

        return Response(200, stream=stream())

    # ----------------------------------------------------------------- 控制面板
    def _panel_summary(self) -> Response:
        """控制面板顶部那条状态带 + 指挥台要用的运行中列表 + 近期活动流。

        只列 running 的话，任务一结束就从列表消失，用户看到的是"少了一条"而不是"跑完了"。
        所以完成的（done / error / cancelled）带终态、验证结果和 updated_at 在 recent 里
        留存 24 小时，前端据此显示"刚完成 ✓ / ✗ 未通过"。
        """
        cutoff = datetime.now(UTC).timestamp() - RECENT_DONE_WINDOW_MINUTES * 60
        running: list[dict[str, Any]] = []
        recent: list[dict[str, Any]] = []
        total_tokens = 0
        for space in self.store.list_spaces(opened_only=False):
            for meta in self.store.list_sessions(space.id, limit=50, include_pinned=False):
                usage = meta.usage or {}
                total_tokens += (usage.get("prompt_tokens") or 0) + (
                    usage.get("completion_tokens") or 0
                )
                item = {
                    "space_id": space.id,
                    "space_name": space.name,
                    "session_id": meta.id,
                    "title": meta.title,
                    "status": meta.status,
                    "updated_at": meta.updated_at,
                    "verification": _verification_status(meta),
                }
                if meta.status == "running":
                    running.append(item)
                elif meta.status in FINAL_STATUSES and _within_window(meta.updated_at, cutoff):
                    recent.append(item)
        recent.sort(key=lambda item: item["updated_at"], reverse=True)
        return Response(
            200,
            {
                "running": running,
                "recent": recent[:RECENT_DONE_LIMIT],
                "opened_spaces": len(self.store.list_spaces(opened_only=True)),
                "today_tokens": total_tokens,  # 目前是累计口径，等 M4 用量统计再按天切
                "unread": self.panel.unread_count(),
                "todos": len([t for t in self.panel.list_todos() if not t["done"]]),
            },
        )

    def _session_summary(self, session_id: str) -> Response:
        """任务卡用的结构化摘要（不调模型）。"""
        space_id = self.store.find_session_space(session_id)
        if space_id is None:
            return Response(404, {"error": "session not found"})
        meta = self.store.get_session_meta(space_id, session_id)
        messages = self.store.load_session(space_id, session_id).messages
        summary = summarize(messages, meta)
        summary["session_id"] = session_id
        summary["space_id"] = space_id
        summary["title"] = meta.title if meta else ""
        summary["status"] = meta.status if meta else "idle"
        summary["line"] = one_line(summary)
        return Response(200, summary)

    def _inbox_list(self, headers: dict[str, str]) -> Response:
        """`?view=active|archived`（默认 active）；条目只带 preview，全文走详情。"""
        query = parse_qs(headers.get("x-query", ""))
        view = query.get("view", ["active"])[0]
        if view not in ("active", "archived"):
            return Response(400, {"error": "view 只能是 active / archived"})
        raw = query.get("limit", [""])[0]
        limit = int(raw) if raw.isdigit() else 50
        unread_only = query.get("unread", [""])[0] in ("1", "true", "yes")
        return Response(
            200,
            self.panel.list_messages(
                view=view, limit=max(1, min(limit, 200)), unread_only=unread_only
            ),
        )

    def _inbox_detail(self, item_id: str) -> Response:
        item = self.panel.get_message(item_id)
        if item is None:
            return Response(404, {"error": "message not found"})
        return Response(200, item)

    def _inbox_add(self, body: bytes) -> Response:
        """外部消息源（定时任务 / 邮件适配器）投递用；不开服务时可以用 `sa inbox push`。

        正文截断、level 白名单都在 store 里做，两条投递路径口径一致。
        """
        data = self._safe_json(body) or {}
        title = str(data.get("title", "")).strip()
        if not title:
            return Response(400, {"error": "title 不能为空"})
        item = self.panel.add_message(
            source=str(data.get("source", "manual")),
            title=title,
            body=str(data.get("body", "")),
            level=str(data.get("level", "info")),
            ref=data.get("ref") if isinstance(data.get("ref"), dict) else {},
        )
        return Response(201, item.to_dict())

    def _inbox_read(self, item_id: str) -> Response:
        """标记已读。单条时顺带返回更新后的条目：前端马上就能显示「N 分钟后归档」。"""
        changed = self.panel.mark_read(item_id)
        if item_id in ("*", "all"):
            return Response(200, {"read": item_id, "changed": changed})
        item = self.panel.get_message(item_id)
        if item is None:
            return Response(404, {"error": "message not found"})
        item.pop("body", None)  # 和列表条目同形，前端直接替换
        return Response(200, {"read": item_id, "changed": changed, "item": item})

    def _inbox_archive(self, item_id: str) -> Response:
        item = self.panel.archive(item_id)
        if item is None:
            return Response(404, {"error": "message not found"})
        return Response(200, item)

    def _todo_list(self) -> Response:
        return Response(200, self.panel.list_todos())

    def _todo_add(self, body: bytes) -> Response:
        data = self._safe_json(body) or {}
        text = str(data.get("text", "")).strip()
        if not text:
            return Response(400, {"error": "备忘内容不能为空"})
        kind = data.get("kind") if data.get("kind") in ("text", "session") else "text"
        ref = data.get("ref") if isinstance(data.get("ref"), dict) else {}
        return Response(201, self.panel.add_todo(text, kind=kind, ref=ref).to_dict())

    def _todo_update(self, todo_id: str, body: bytes) -> Response:
        data = self._safe_json(body) or {}
        fields = {k: v for k, v in data.items() if k in ("text", "done")}
        if not fields:
            return Response(400, {"error": "只能改 text / done"})
        todo = self.panel.update_todo(todo_id, **fields)
        if todo is None:
            return Response(404, {"error": "todo not found"})
        return Response(200, todo.to_dict())

    def _todo_delete(self, todo_id: str) -> Response:
        if not self.panel.delete_todo(todo_id):
            return Response(404, {"error": "todo not found"})
        return Response(200, {"deleted": todo_id})

    # ----------------------------------------------------------------- 审批
    def _list_approvals(self) -> Response:
        """待审批的详情。客户端刚连上时靠它补出没收到的那张审批卡。"""
        return Response(200, {"pending": self.runner.pending.details()})

    def _resolve_approval(self, approval_id: str, body: bytes) -> Response:
        data = self._safe_json(body) or {}
        ok = self.runner.approve(approval_id, data.get("action", "allow"))
        if not ok:
            return Response(404, {"error": "approval not found or runner not started"})
        return Response(200, {"resolved": approval_id})

    # ----------------------------------------------------------------- 工具
    @staticmethod
    def _safe_json(body: bytes) -> dict[str, Any] | None:
        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None


# ----------------------------------------------------------------------- 文件树
def file_tree(root: Path, depth: int = 2, limit: int = 300) -> list[dict[str, Any]]:
    """工作目录的浅层文件树。

    只看 depth 层、条目数封顶：右栏「文件」tab 是拿来定位文件的，不是文件管理器，
    真要深挖让 agent 用 list_dir / glob。
    """
    budget = [limit]

    def walk(dir_path: Path, left: int) -> list[dict[str, Any]]:
        if left <= 1:
            return []
        items: list[dict[str, Any]] = []
        try:
            entries = sorted(dir_path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return items
        for p in entries:
            if budget[0] <= 0:
                break
            if p.name.startswith(".") or (p.is_dir() and p.name in IGNORED_DIRS):
                continue
            budget[0] -= 1
            node: dict[str, Any] = {
                "name": p.name,
                "path": str(p),
                "type": "dir" if p.is_dir() else "file",
            }
            if p.is_dir():
                node["children"] = walk(p, left - 1)
            items.append(node)
        return items

    if not root.exists():
        return []
    top = walk(root, depth)
    return [{"name": root.name, "path": str(root), "type": "dir", "children": top}]


# ----------------------------------------------------------------------- 适配 http.server
def _make_handler(app: Server):
    class Handler(BaseHTTPRequestHandler):
        def handle(self) -> None:
            """兜住「客户端中途消失」。

            关标签页、断网、切 Wi-Fi 都会让已建立的连接突然消失，之后读写它就是
            ConnectionResetError / BrokenPipeError。http.server 对这些异常不设防，
            会一路冒到 socketserver 的 handle_error，打一整段 traceback 到 stderr
            —— 看起来像服务崩了，其实只是有人走了。
            """
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def _dispatch(self) -> None:
            method = self.command
            parsed = urlparse(self.path)
            headers = {k.lower(): v for k, v in self.headers.items()}
            headers["x-query"] = parsed.query
            length = int(headers.get("content-length", 0) or 0)
            body = self.rfile.read(length) if length else b""
            resp = app.handle(method, parsed.path, headers, body)

            self.send_response(resp.status)
            for k, v in resp.headers.items():
                self.send_header(k, v)
            if resp.stream is not None:
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                # 必须放在 send_header("Connection", ...) 之后：BaseHTTPRequestHandler
                # 会在这个方法里把 close_connection 置成 False。不盖回来，这条连接
                # 处理完 SSE 后还会回到 rfile.readline() 等下一个请求，线程要多挂一个
                # keepalive 周期才释放，断开时还会在那儿抛 ConnectionResetError。
                # SSE 是要么一直流、要么断开的单向连接，没有「下一个请求」可言。
                self.close_connection = True
                self.end_headers()
                try:
                    for chunk in resp.stream:
                        data = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
                        self.wfile.write(data)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if isinstance(resp.body, (bytes, bytearray)):  # 静态资源
                self.send_header(
                    "Content-Type", resp.headers.get("Content-Type", "application/octet-stream")
                )
                self.send_header("Content-Length", str(len(resp.body)))
                self.end_headers()
                self.wfile.write(resp.body)
                return
            if resp.body is not None:
                out = json.dumps(resp.body, ensure_ascii=False).encode("utf-8")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
            else:
                self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def do_PATCH(self) -> None:  # noqa: N802
            self._dispatch()

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch()

        def log_message(self, *args: Any) -> None:  # 安静：不刷访问控制日志
            pass

    return Handler


class ApiServer(ThreadingHTTPServer):
    """HTTP server 关闭时顺带关掉 Runner：它的后台循环里跑着 MCP server 子进程。"""

    app: Server | None = None

    def server_close(self) -> None:
        super().server_close()
        if self.app is not None:
            self.app.runner.shutdown()


def make_server(
    config: Config, host: str = "127.0.0.1", port: int = 8384, llm_factory=None
) -> ApiServer:
    """构造并启动本地 API 服务，返回已 start() 的 http server（调用方负责 serve_forever）。

    先抢端口再 app.start()：端口被占用时直接抛 OSError，不会留下一个已经跑起来的
    Runner 线程（调用方拿到异常就没法再关它了）。
    """
    app = Server(config, llm_factory=llm_factory)
    handler = _make_handler(app)
    httpd = ApiServer((host, port), handler)
    httpd.app = app
    app.start()
    return httpd
