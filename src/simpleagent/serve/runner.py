"""Runner：后台 asyncio 线程，把一次用户输入交给对应空间的 Agent 执行。

职责：
- 按空间的执行者分两条路径：`simpleagent` 用内置 loop（按目录 / profile 构造 Agent、注入审批器）；
  `claude-code` / `opencode` 起无头 CLI，把它的 NDJSON 翻译成同一套事件（见 agents/）。
- 两条路径的产出汇合在 `_on_event`：先落盘再广播。
- 跑 agent.run() / CLI，把事件实时发到事件总线，同时把消息和元信息落盘。
- 支持取消（内置 loop 取消底层 asyncio 任务；外部 CLI 直接 terminate 进程）。
- 支持手动触发验证命令，并把结果写成 verification 帧 + 落盘。
- 持有一个 McpManager：MCP server 在后台事件循环里启动一次，所有空间、所有会话共用。
- 每个会话第一次跑时读一次项目指令、记忆索引、技能清单（M7），连同拼好的 system prompt 记下来，
  之后每轮都用这一份：Agent 每轮新建，system prompt 要是每轮重拼，中途改个记忆就整段缓存失效。
- 指挥台（保留空间 sp_command）的会话走调度者：只有 4 个调度工具（见 command/），
  dispatch / followup 通过 `run_child` 在目标空间新建子会话或追问已有会话、等它跑完，
  把结论交回调度者。
- 同一个会话同一时间只跑一轮：`claim` 占住、收口时放开，被占着时再发输入抛 `SessionBusy`。

线程模型：Runner 自己起一个线程跑 asyncio 事件循环；HTTP 层在另一个线程，通过
run_coroutine_threadsafe / call_soon_threadsafe 与它通信，两者用总线（线程安全）解耦。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from simpleagent.agent.loop import Agent
from simpleagent.agent.prompt import build_system_prompt
from simpleagent.agent.session import Session
from simpleagent.agents import adapter_for
from simpleagent.command import ChildResult, SessionBrief, command_prompt, command_tools
from simpleagent.config import TOOL_OUTPUT_DIRNAME, Config, home_dir
from simpleagent.events import Event, MessageDone, ToolResult
from simpleagent.knowledge import Knowledge
from simpleagent.mcp.manager import McpManager
from simpleagent.panel.quote import has_quote
from simpleagent.panel.store import PanelStore
from simpleagent.panel.summary import summarize
from simpleagent.permissions import Policy
from simpleagent.serve.approval import APIApprover, ApprovalDecision, PendingApprovals
from simpleagent.serve.bus import EventBus
from simpleagent.serve.frames import (
    error_frame,
    event_to_frame,
    status_frame,
    verification_frame,
)
from simpleagent.spaces.describe import TITLE_LIMIT, DescribeError, describe_space, gather_material
from simpleagent.spaces.models import COMMAND_SPACE_ID, SessionMeta, Space, locked_reason
from simpleagent.spaces.store import SpaceStore
from simpleagent.spaces.verify import changed_since, fingerprint
from simpleagent.tools import ToolRegistry, builtin_tools
from simpleagent.tools.base import ToolError

# (profile_name, Profile) -> LLM 实例；测试时注入 FakeLLM
LLMFactory = Callable[[str, Any], Any]

# 终态 → (消息级别, 标题用词)。只有 INBOX_LEVELS 里的级别才进控制面板的消息，
# 其余只在指挥台显示当前状态（meta + status 帧已经够它用了）
FINAL_NOTICE = {
    "done": ("success", "完成"),
    "cancelled": ("warn", "已取消"),
    "error": ("error", "失败"),
}
INBOX_LEVELS = frozenset({"error"})


def _log_future_error(fut: Any) -> None:
    """兜底日志：协程里漏出去的异常不取出来的话，asyncio 会一声不响地吞掉。"""
    if fut.cancelled():
        return
    exc = fut.exception()
    if exc is not None:
        print(f"[runner] 未处理的异常：{type(exc).__name__}: {exc}", file=sys.stderr)


class SessionBusy(RuntimeError):
    """会话正在跑一轮，不能再开一轮（HTTP 层转 409）。"""


def _action_to_decision(action: str) -> ApprovalDecision:
    action = (action or "").lower()
    if action == "always":
        return ApprovalDecision(allow=True, always=True)
    if action == "deny":
        return ApprovalDecision(allow=False)
    return ApprovalDecision(allow=True)


class Runner:
    def __init__(
        self,
        config: Config,
        *,
        store: SpaceStore | None = None,
        bus: EventBus | None = None,
        pending: PendingApprovals | None = None,
        llm_factory: LLMFactory | None = None,
    ) -> None:
        self.config = config
        self.store = store or SpaceStore()
        self.bus = bus or EventBus()
        self.pending = pending or PendingApprovals()
        self.panel = PanelStore()
        self.llm_factory = llm_factory or self._default_llm_factory
        # 跨轮有效的「本次会话始终允许」记录
        self.always_allow: dict[str, set[str]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._agents: dict[str, Agent] = {}
        # 外部 CLI 执行者：正在跑的子进程，以及「这次是用户主动取消的」标记
        self._procs: dict[str, Any] = {}
        self._cancelled: set[str] = set()
        # MCP server 跟着 serve 进程走，不跟着会话走：start() 时启动一次，所有空间共用
        self.mcp = McpManager(config.mcp_servers)
        self._mcp_started: concurrent.futures.Future[None] | None = None
        # 会话 id → (会话开始时读到的 Knowledge, 拼好的 system prompt)。serve 重启后重新读
        self._prompts: dict[str, tuple[Knowledge, str]] = {}
        # 调度者正在等的子会话 → 这一轮的收口状态和失败原因（还没收口是 None）。
        # run_child 先登记、_finalize 只填登记过的、run_child 最后取走：普通会话不进来，不会越攒越多
        self._outcomes: dict[str, tuple[str, str | None] | None] = {}
        # 正在跑一轮的会话 → 占用凭据。你在界面上发、调度者追问，可能同时冲着同一个会话来，
        # 同一个会话同时跑两轮会把消息交错写进 jsonl。HTTP 线程和事件循环线程都会占，所以加锁
        self._claimed: dict[str, object] = {}
        self._claim_lock = threading.Lock()

    # ----------------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        # 在主线程先把 loop 造好，保证 start() 返回后 _schedule 就能用，避免竞态
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="simpleagent-runner"
        )
        self._thread.start()
        self._mcp_started = asyncio.run_coroutine_threadsafe(self.mcp.start(), self._loop)
        self._mcp_started.add_done_callback(_log_future_error)
        self._mcp_started.add_done_callback(self._report_mcp)

    def _report_mcp(self, fut: Any) -> None:
        """MCP 启动完打一行状态到 stderr：serve 在后台跑，server 起不来要让人看得到。"""
        if fut.cancelled() or fut.exception() is not None:
            return
        if summary := self.mcp.summary():
            print(summary, file=sys.stderr)
            for server in self.mcp.servers:
                if server.state == "failed" and server.error:
                    for line in server.error.splitlines():
                        print(f"  {line}", file=sys.stderr)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def shutdown(self) -> None:
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            # 先关 MCP server 再停循环：子进程和管道绑在这个循环上，循环停了就关不掉了
            closing = asyncio.run_coroutine_threadsafe(self.mcp.close(), self._loop)
            try:
                closing.result(timeout=10)
            except Exception:  # noqa: BLE001  关不干净也要继续退出
                pass
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _schedule(self, coro: Any) -> None:
        assert self._loop is not None
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        # 兜底：协程里漏掉的异常如果不取出来，asyncio 会静默吞掉，排查时毫无痕迹
        fut.add_done_callback(_log_future_error)

    # --------------------------------------------------- 对 HTTP 层暴露的同步接口
    def run_input(self, space_id: str, session_id: str, user_input: str) -> None:
        """在会话里跑一轮。会话正在跑就抛 SessionBusy，不排队：排队的话界面上看不出来。"""
        token = self.claim(session_id)
        if token is None:
            raise SessionBusy("这个会话正在跑，等它这一轮结束再发")
        try:
            self._schedule(self._run_input(space_id, session_id, user_input, token))
        except BaseException:
            self._release(session_id, token)
            raise

    def claim(self, session_id: str) -> object | None:
        """占住这个会话跑一轮，返回占用凭据；已经有人在跑返回 None。

        不用 _tasks 判断：它要等 _run_input 真正开始执行（还要先等 MCP 启动完）才登记，
        从收到请求到登记之间有空档，两个请求能同时过检查。
        """
        with self._claim_lock:
            if session_id in self._claimed:
                return None
            token = object()
            self._claimed[session_id] = token
            return token

    def _release(self, session_id: str, token: object | None = None) -> None:
        """放开占用。带 token 时只放自己那一份：收口时已经放过、又被别人占上的，不能误放。"""
        with self._claim_lock:
            if token is None or self._claimed.get(session_id) is token:
                self._claimed.pop(session_id, None)

    def session_busy(self, session_id: str) -> bool:
        with self._claim_lock:
            return session_id in self._claimed

    def cancel(self, session_id: str) -> None:
        if self._loop is None:
            return
        agent = self._agents.get(session_id)
        if agent is not None:
            self._loop.call_soon_threadsafe(agent.cancel)
            return
        # 外部 CLI：杀进程就是取消。进程死了读循环自然结束，再按 cancelled 收口。
        proc = self._procs.get(session_id)
        if proc is not None:
            self._cancelled.add(session_id)
            self._loop.call_soon_threadsafe(self._terminate, proc)

    def _terminate(self, proc: Any) -> None:
        """杀**整个进程组**，不是只杀父进程。

        CLI 自己会再 fork（opencode 每次 run 都会起一个本地 server），只 terminate 父进程的话
        子进程还攥着 stdout 管道，我们这边就等不到 EOF、取消像没生效一样。
        """
        self._killpg(proc, signal.SIGTERM)

        def force() -> None:
            if proc.returncode is None:  # 3 秒还不走就强杀
                self._killpg(proc, signal.SIGKILL)

        assert self._loop is not None
        self._loop.call_later(3.0, force)

    @staticmethod
    def _killpg(proc: Any, sig: int) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                pass

    def approve(self, approval_id: str, action: str) -> bool:
        if self._loop is None:
            return False
        decision = _action_to_decision(action)
        self._loop.call_soon_threadsafe(self.pending.resolve, approval_id, decision)
        return True

    def space_busy(self, space_id: str) -> bool:
        """这个空间有没有会话正在跑。切执行者前要问：跑到一半换人会乱。

        看内存里的 _tasks 而不是 meta.status：进程崩过的话 meta 会一直停在 running。
        _tasks 只以 session id 为键，归属靠「meta 在不在这个空间目录下」判断；
        list() 拷一份，因为事件循环线程可能正在增删它。
        """
        return any(
            self.store.get_session_meta(space_id, sid) is not None for sid in list(self._tasks)
        )

    def verify(self, space_id: str, session_id: str) -> None:
        self._schedule(self._verify(space_id, session_id))

    def describe(self, space_id: str) -> concurrent.futures.Future[str]:
        """自动生成空间简介（HTTP 层等它的结果）。模型调用要在后台循环里跑，所以返回 Future。"""
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(self._describe(space_id), self._loop)

    # --------------------------------------------------------- 指挥台调度（Dispatcher）
    def targets(self) -> list[Space]:
        """调度者能派的空间：左栏打开着的那些，不含指挥台自己。关掉的空间不派。"""
        return [s for s in self.store.list_spaces(opened_only=True) if s.id != COMMAND_SPACE_ID]

    def sessions(self, space_id: str | None = None, limit: int = 10) -> list[SessionBrief]:
        """打开着的空间里最近的会话，新的在前（不含指挥台）。调度者追问前靠它找会话。

        先只看 meta 排序、截断，最后几条才去读 jsonl 拿最后一句回复：每个会话都读太慢。
        """
        found: list[tuple[Space, SessionMeta]] = []
        for space in self.targets():
            if space_id is not None and space.id != space_id:
                continue
            for meta in self.store.list_sessions(space.id, limit=limit, include_pinned=False):
                found.append((space, meta))
        found.sort(key=lambda pair: pair[1].updated_at or "", reverse=True)
        return [self._brief(space, meta) for space, meta in found[:limit]]

    def find_session(self, session_id: str) -> SessionBrief | None:
        """按 id 找会话，不管它在哪个空间（关掉的、指挥台的也找得到，能不能追问由调用方判断）。"""
        space_id = self.store.find_session_space(session_id)
        space = self.store.get_space(space_id) if space_id else None
        meta = self.store.get_session_meta(space_id, session_id) if space else None
        if space is None or meta is None:
            return None
        return self._brief(space, meta)

    def _brief(self, space: Space, meta: SessionMeta) -> SessionBrief:
        messages = self.store.load_session(space.id, meta.id).messages
        return SessionBrief(
            space_id=space.id,
            space_name=space.name,
            session_id=meta.id,
            title=meta.title,
            status=meta.status,
            updated_at=meta.updated_at,
            dispatched=meta.parent_session_id is not None,
            locked=meta.agent != space.executor,
            busy=self.session_busy(meta.id),
            last_text=summarize(messages, meta)["last_text"],
        )

    async def run_child(
        self, space_id: str, task: str, *, parent: str, session_id: str | None = None
    ) -> ChildResult:
        """在目标空间跑 task，等它收口后把结论交回调度者。

        session_id 为空：新建子会话；不为空：追问这个已有的会话，沿用它的上下文。
        结果只统计这一轮：追问前记下已有几条消息，之后的才拿去摘要、取回复——
        不然这一轮没给回复时，会把上一轮的旧回复当成结论交回去。

        子会话单独起一个 asyncio 任务再等它，不能直接 await _run_input：它用 current_task()
        把自己登记进 _tasks，直接 await 的话登记的是调度者的任务，取消子会话会连调度者一起取消。
        外面套 shield：调度者被取消时，不让 CancelledError 直接打进子任务——外部 CLI 的子进程
        得靠 cancel() 杀进程组才收得干净，直接取消任务会把进程留在后台。
        """
        space = self.store.get_space(space_id)
        name = space.name if space else space_id
        if session_id is None:
            meta = self.store.create_session(space_id, parent=parent)
        else:
            found = self.store.get_session_meta(space_id, session_id)
            if found is None:
                raise ToolError(f"会话不存在：{session_id}")
            meta = found
        # 工具那边查过一次，这里再占一次：查完到这里之间，你可能刚在界面上给它发了一句
        token = self.claim(meta.id)
        if token is None:
            raise ToolError(f"会话 {meta.id} 正在跑，等它这一轮结束再追问")
        start = len(self.store.load_session(space_id, meta.id).messages)
        # 卡片挂在「最近一次让它跑的调度者」下面；parent_session_id 记的是出身，不变
        self.store.update_meta(space_id, meta.id, dispatched_by=parent)
        self._outcomes[meta.id] = None
        child = asyncio.create_task(self._run_input(space_id, meta.id, task, token))
        try:
            await asyncio.shield(child)
        except asyncio.CancelledError:
            # 调度者被取消：子任务跟着停。外部 CLI 杀进程组，内置 loop 直接取消它的任务
            if meta.id in self._procs:
                self.cancel(meta.id)
            else:
                child.cancel()
            await asyncio.wait({child})
            raise
        finally:
            outcome = self._outcomes.pop(meta.id, None)
        status, reason = outcome or ("error", "子会话没有跑起来")
        final = self.store.get_session_meta(space_id, meta.id)
        messages = self.store.load_session(space_id, meta.id).messages[start:]
        facts = summarize(messages, final)
        reply = next(
            (
                str(m["content"]).strip()
                for m in reversed(messages)
                if m.get("role") == "assistant"
                and isinstance(m.get("content"), str)
                and m["content"].strip()
            ),
            "",
        )
        return ChildResult(
            space_id=space_id,
            space_name=name,
            session_id=meta.id,
            status=status,
            reply=reply,
            reason=reason,
            files=facts["files"],
            verification=facts["verification"],
            followup=session_id is not None,
        )

    # --------------------------------------------------------------- 内部实现
    async def _run_input(
        self, space_id: str, session_id: str, user_input: str, token: object
    ) -> None:
        """跑一轮。调用方已经 claim 住会话（token）；这里保证无论怎么结束都放开。

        正常收口在 _finalize 里就放了（要赶在终态帧之前，见那里的注释），这里是兜底：
        提前 return、构造阶段之前就抛出的异常，都靠它放开，不然会话永远显示「正在跑」。
        """
        try:
            await self._run_turn(space_id, session_id, user_input)
        finally:
            self._release(session_id, token)

    async def _run_turn(self, space_id: str, session_id: str, user_input: str) -> None:
        space = self.store.get_space(space_id)
        meta = self.store.get_session_meta(space_id, session_id)
        if space is None or meta is None:
            return
        # 会话的执行者在创建时就定下了（meta.agent）；空间的 executor 只决定新会话用谁。
        # 两边对不上说明空间中途切过执行者：历史在原执行者那边，接不过来，只能拒绝。
        # 什么都没跑，所以不落用户消息、不改状态、不走 _finalize。
        executor = meta.agent
        if executor != space.executor:
            self._release(session_id)  # 先放开再报错：客户端看到报错就可能换个说法再发
            self.bus.publish(error_frame(session_id, locked_reason(executor, space.executor)))
            return
        # 内置 agent 用「模型看到的历史」（清理、压缩、中断修复都落在里面）；外部 CLI 的上下文
        # 由它自己维护，这里只要用量。都要在落用户消息之前读，否则这条输入会被算进历史两次
        session = (
            self.store.load_model_session(space_id, session_id)
            if executor == "simpleagent"
            else self.store.load_session(space_id, session_id)
        )
        try:
            # 先落用户消息再构造 agent：起不来的时候（没配 key、profile 不存在、外部 CLI
            # 还没接入）也要把用户说的这句留下来，否则刷新页面它就不见了。
            self.store.append_message(space_id, session_id, {"role": "user", "content": user_input})
            self.store.update_meta(space_id, session_id, status="running")
            if executor == "simpleagent":
                # MCP server 还在启动就等它：工具列表要在第一次请求前定下来
                if self._mcp_started is not None:
                    await asyncio.wrap_future(self._mcp_started)
                self._agents[session_id] = self._build_agent(space, session, executor)
                # `/技能名 补充说明`：模型看到技能全文，界面上的消息还是用户敲的原话
                knowledge, _ = self._prompts[session_id]
                user_input = knowledge.skills.expand_command(user_input) or user_input
        except Exception as e:  # noqa: BLE001
            # 起不来必须让客户端知道：这一段在原来是在 try 之外，异常会被 asyncio future
            # 吞掉，表现是「发了消息没有任何反应，且永远停在运行中」。典型触发：没配 API key、
            # profile 不存在、cwd 不存在。CancelledError 继承自 BaseException，不会被这里吃掉。
            # 也走 _finalize：起不来正是最该进消息提醒的失败，不能绕开收口点。
            message = f"启动失败：{type(e).__name__}: {e}"
            self.bus.publish(error_frame(session_id, message))
            self._finalize(space_id, session_id, session, "error", reason=message)
            self._agents.pop(session_id, None)
            return
        # 广播一帧 running，面板才知道"开始了"
        self.bus.publish(status_frame(session_id, "running", {"space_id": space_id}))

        task = asyncio.current_task()
        if task is not None:
            self._tasks[session_id] = task
        try:
            reason: str | None = None
            if executor == "simpleagent":
                agent = self._agents[session_id]
                async for event in agent.run(session, user_input):
                    self._on_event(space_id, session_id, event)
                status = "done"
            else:
                # 外部 CLI 走完全不同的执行路径，但生成的是同一套事件
                status, reason = await self._run_cli(space, session, user_input, executor)
        except asyncio.CancelledError:
            self._finalize(space_id, session_id, session, "cancelled")
            raise
        except Exception as e:  # noqa: BLE001  任何异常都转成 error 帧并落盘状态
            message = f"{type(e).__name__}: {e}"
            self.bus.publish(error_frame(session_id, message))
            self._finalize(space_id, session_id, session, "error", reason=message)
        else:
            self._finalize(space_id, session_id, session, status, reason=reason)
        finally:
            self._tasks.pop(session_id, None)
            self._agents.pop(session_id, None)

    def _on_event(self, space_id: str, session_id: str, event: Event) -> None:
        # 先落盘再广播：保证订阅者看到 message_done 帧时，消息已经写进 jsonl，
        # 避免「刷新页面消息丢了」这类竞态
        if isinstance(event, MessageDone):
            self.store.append_message(space_id, session_id, event.message)
        elif isinstance(event, ToolResult):
            self.store.append_message(space_id, session_id, event.as_message())
            self._maybe_mark_stale(space_id, session_id, event)
        self.bus.publish(event_to_frame(event, session_id))

    def _maybe_mark_stale(self, space_id: str, session_id: str, event: ToolResult) -> None:
        """写工具真的改动了文件之后，已通过的验证降级为 `stale`。

        用指纹而不是「跑过写工具就 stale」：bash 也是写工具，但 `ls` 之类并不改文件，
        指纹没变就不该让验证结果失效。
        """
        if event.is_error:
            return
        agent = self._agents.get(session_id)
        if agent is None or agent.tools.is_readonly(event.name):
            return
        meta = self.store.get_session_meta(space_id, session_id)
        if meta is None or meta.verification.status != "passed":
            return
        space = self.store.get_space(space_id)
        if space is None or not changed_since(meta.verification.fingerprint, self.cwd_for(space)):
            return
        verification = meta.verification.to_dict()
        verification["status"] = "stale"
        self.store.update_meta(space_id, session_id, verification=verification)
        self.bus.publish(verification_frame(session_id, verification))

    def _finalize(
        self,
        space_id: str,
        session_id: str,
        session: Session,
        status: str,
        *,
        reason: str | None = None,
    ) -> None:
        """一轮结束的唯一收口点：落盘终态 + 广播一帧 status + 失败时往控制面板的消息里落一条。

        正常结束时没有 error 帧，客户端和控制面板都要靠这帧把「运行中」切成终态，
        所以三个终态（done / error / cancelled）统一在这里广播。
        """
        usage = session.usage.__dict__
        self.store.update_meta(space_id, session_id, status=status, usage=usage)
        if session_id in self._outcomes:  # 调度者在等它：把结果留给 run_child
            self._outcomes[session_id] = (status, reason)
        extra: dict[str, Any] = {"space_id": space_id, "usage": usage}
        if reason is not None:
            extra["reason"] = reason
        # 放开占用要赶在终态帧之前：客户端收到这帧马上再发一句，不能撞上还没放开的占用。
        # 这时占用一定还是这一轮自己的，可以不带 token 直接放；之后到 _run_turn 退出都没有
        # await，下一轮要等这段同步代码跑完才开始，不会和它的收尾交错
        self._release(session_id)
        self.bus.publish(status_frame(session_id, status, extra))
        if status in FINAL_NOTICE:
            self._notify(space_id, session_id, status, reason)

    def _notify(self, space_id: str, session_id: str, status: str, reason: str | None) -> None:
        """级别够高的终态才往控制面板的消息里落一条：消息只放要你去处理的事。

        完成 / 取消已经随 meta 落盘、随 status 帧广播，指挥台的任务卡自己就能显示当前状态；
        再各落一条消息只会把左栏角标刷高，真正的失败反而被淹掉。
        """
        level, head = FINAL_NOTICE[status]
        if level not in INBOX_LEVELS:
            return
        space = self.store.get_space(space_id)
        meta = self.store.get_session_meta(space_id, session_id)
        title = (meta.title if meta else "") or "会话"
        name = space.name if space else space_id
        self.panel.add_message(
            source="system",
            title=f"{name} · {head}：{title}",
            body=reason or "运行出错，去那个会话的日志 tab 看原因",
            level=level,
            ref={"space_id": space_id, "session_id": session_id},
        )

    async def _run_cli(
        self, space: Space, session: Session, user_input: str, executor: str
    ) -> tuple[str, str | None]:
        """把一次输入交给外部 CLI（claude / opencode），返回这次运行的收口状态和失败原因。

        和内置 loop 的路径**汇合在同一套事件上**：翻译出来的 TextDelta / ToolCallStart /
        ToolResult / MessageDone 照常走 `_on_event`（先落盘再广播），所以总线、存储、
        前端一行都不用改——区别只是「谁在生成这些事件」。

        对面的会话历史由它自己维护（我们只存 session id 用于 resume），
        我们的 jsonl 是给人看的展示层，不是喂给它的上下文。
        """
        meta = self.store.get_session_meta(space.id, session.id)
        resume = (meta.agent_session_id if meta else None) or None
        adapter = adapter_for(executor)
        argv = adapter.command(
            user_input,
            command=space.agent.command if space.agent else None,
            model=space.cli_model,
            resume=resume,
            mode=space.permission,
        )
        cwd = self.cwd_for(space)
        # 本机默认 = 只继承现有环境；adapter.env() 只在需要注入配置时才有内容
        env = {**os.environ, **adapter.env(space.permission)}
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # 独立进程组：取消时要连它 fork 出来的子进程一起收（见 _terminate）
                start_new_session=True,
                # 一行的默认上限只有 64KB，工具结果（大文件、base64）很容易超过它
                limit=4 * 1024 * 1024,
            )
        except FileNotFoundError:
            message = f"找不到可执行文件：{argv[0]}"
            self.bus.publish(error_frame(session.id, message))
            return "error", message
        except OSError as e:
            message = f"启动 {argv[0]} 失败：{e}"
            self.bus.publish(error_frame(session.id, message))
            return "error", message

        self._procs[session.id] = proc
        ok: bool | None = None
        error: str | None = None
        stderr_task = asyncio.create_task(self._drain(proc.stderr))
        try:
            async for raw in proc.stdout:  # type: ignore[union-attr]
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    turn = adapter.parse(line)
                except Exception as e:  # noqa: BLE001  一行解析失败不该毁掉整轮
                    print(f"[runner] 解析 {executor} 事件失败：{e}", file=sys.stderr)
                    continue
                if adapter.session_id and adapter.session_id != resume:
                    # 第一次拿到对面的会话 id：存下来，下次追问带 resume 参数
                    self.store.update_meta(
                        space.id, session.id, agent_session_id=adapter.session_id
                    )
                    resume = adapter.session_id
                for event in turn.events:
                    self._on_event(space.id, session.id, event)
                if turn.error:
                    error = turn.error
                if turn.finished:
                    ok = turn.ok
            code = await proc.wait()
        finally:
            self._procs.pop(session.id, None)
            stderr_tail = await stderr_task

        session.usage = adapter.usage
        if session.id in self._cancelled:
            self._cancelled.discard(session.id)
            return "cancelled", None
        if ok is None:
            # 对面没给结束事件（进程被信号打死、崩了）：只能拿退出码兜底
            ok = code == 0
            if not ok and not error:
                error = f"{executor} 退出码 {code}"
                if stderr_tail:
                    error += f"：{stderr_tail}"
        if not ok:
            message = error or f"{executor} 运行失败"
            self.bus.publish(error_frame(session.id, message))
            return "error", message
        return "done", None

    @staticmethod
    async def _drain(stream: Any, keep: int = 4000) -> str:
        """把 stderr 读干净（不读会写满管道把子进程卡死），只留最后一段用于报错。"""
        if stream is None:
            return ""
        buf = bytearray()
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > keep * 2:
                del buf[:-keep]
        return buf.decode("utf-8", "replace")[-keep:]

    def _build_agent(self, space: Space, session: Session, executor: str) -> Agent:
        # 外部执行者走 _run_cli，不在这里造 Agent。走到这儿说明调用方忘了分支——
        # 宁可炸掉也不能静默用内置 loop 跑，那会让用户以为在跑 claude。
        if executor != "simpleagent":
            raise RuntimeError(
                f"{executor} 要走 CLI 路径（_run_cli），_build_agent 只服务内置 loop"
            )
        if space.id == COMMAND_SPACE_ID:
            return self._build_commander(space, session)
        if space.profile not in self.config.profiles:
            raise RuntimeError(
                f"空间用的 profile「{space.profile}」不在 config.toml 里，"
                f"可选：{'、'.join(sorted(self.config.profiles))}"
            )
        profile = self.config.profiles[space.profile]
        llm = self.llm_factory(space.profile, profile)
        cwd = self.cwd_for(space)
        knowledge, system_prompt = self._session_prompt(session.id, cwd)
        tools = ToolRegistry(
            [*builtin_tools(), *knowledge.tools(), *self.mcp.tools()],
            max_output_chars=self.config.tool_output.max_chars,
            max_output_lines=self.config.tool_output.max_lines,
        )
        approver = APIApprover(self.bus, self.pending, self.always_allow)
        tools.approver = approver
        # 客户端模式也走同一套权限判定：越界和危险命令先被拦掉，剩下的才去问客户端
        tools.policy = Policy(cwd)
        output_dir = home_dir() / TOOL_OUTPUT_DIRNAME
        return Agent(
            llm=llm,
            tools=tools,
            system_prompt=system_prompt,
            cwd=cwd,
            max_steps=self.config.max_steps,
            output_dir=output_dir,
            hidden_env=self.config.api_key_env_names(),
            context=self.config.context,
        )

    def _build_commander(self, space: Space, session: Session) -> Agent:
        """指挥台的调度者：只有 propose_plan / dispatch，没有文件工具、记忆、技能、MCP。

        模型看 config.toml 的 [command].profile（不填用默认 profile），不看空间自己的 profile。
        工具每轮新造：「本轮派过哪些空间、批准过什么计划」只在这一轮有效。
        审批器给一份独立的「始终允许」记录：计划每次都要人看，不能被一次「始终允许」放过去。
        引用过控制面板消息的调度会话，派发前一律先出计划卡（command_tools 的 require_plan）。
        """
        name = self.config.command.profile or self.config.default_profile
        llm = self.llm_factory(name, self.config.profiles[name])
        cached = self._prompts.get(session.id)
        if cached is None:
            # 空间清单在会话开始时拼一次，之后不变（前缀缓存）。Knowledge 为空：
            # _run_input 要拿它展开 /技能名，调度者没有技能
            cached = (Knowledge(), command_prompt(self.targets()))
            self._prompts[session.id] = cached
        approver = APIApprover(self.bus, self.pending, {})
        # 这个调度会话引用过消息（这一轮或之前）：之后每次派发都要先出计划卡。
        # 看落盘的原始历史：本轮的输入在构造 agent 之前已经落了，模型那份历史可能被压缩过
        quoted = any(
            m.get("role") == "user" and has_quote(str(m.get("content") or ""))
            for m in self.store.load_session(space.id, session.id).messages
        )
        tools = ToolRegistry(
            command_tools(self, parent=session.id, approver=approver, require_plan=quoted),
            max_output_chars=self.config.tool_output.max_chars,
            max_output_lines=self.config.tool_output.max_lines,
        )
        tools.approver = approver
        cwd = self.cwd_for(space)
        tools.policy = Policy(cwd)
        return Agent(
            llm=llm,
            tools=tools,
            system_prompt=cached[1],
            cwd=cwd,
            max_steps=self.config.max_steps,
            output_dir=home_dir() / TOOL_OUTPUT_DIRNAME,
            hidden_env=self.config.api_key_env_names(),
            context=self.config.context,
        )

    def _session_prompt(self, session_id: str, cwd: Path) -> tuple[Knowledge, str]:
        """这个会话的 Knowledge 和 system prompt：第一次用到时读、拼，之后原样复用。

        连日期一起冻住：跨过午夜的会话，system prompt 也不变。
        """
        cached = self._prompts.get(session_id)
        if cached is None:
            knowledge = Knowledge.load(self.config, cwd)
            system_prompt = build_system_prompt(
                self.config.system_prompt, cwd=cwd, knowledge=knowledge
            )
            cached = (knowledge, system_prompt + self.mcp.prompt_section())
            self._prompts[session_id] = cached
        return cached

    def cwd_for(self, space: Space) -> Path:
        """空间的工作目录：有 cwd 用它，没有就用 spaces/<id>/tmp。

        HTTP 层（文件树、验证）也要用同一个口径，所以是公开方法。
        """
        if space.cwd:
            return Path(space.cwd).expanduser()
        return self.store._space_dir(space.id) / "tmp"

    async def _describe(self, space_id: str) -> str:
        """读空间的目录和最近的会话标题，让默认 profile 写一段简介。不保存，由人改完再存。

        素材为空抛 ValueError（HTTP 400）；模型那边出错抛 DescribeError（HTTP 502）。
        """
        space = self.store.get_space(space_id)
        if space is None:
            raise KeyError(f"空间不存在: {space_id}")
        metas = self.store.list_sessions(space_id, limit=TITLE_LIMIT, include_pinned=False)
        material = gather_material(space, self.cwd_for(space), [m.title for m in metas])
        if material is None:
            raise ValueError("这个空间还没有会话，目录里也没有东西可读，先手写一句")
        # 统一用默认 profile：外部 CLI 空间没有我们的 profile，口径简单
        name = self.config.default_profile
        try:
            llm = self.llm_factory(name, self.config.profiles[name])
        except Exception as e:  # noqa: BLE001  没配 key 之类，和 API 报错一样当生成失败
            raise DescribeError(f"{type(e).__name__}: {e}") from e
        try:
            return await describe_space(llm, material)
        finally:
            await llm.close()

    async def _verify(self, space_id: str, session_id: str) -> None:
        space = self.store.get_space(space_id)
        if space is None or not space.verify or not space.verify.command:
            self.bus.publish(error_frame(session_id, "该空间没有配置验证命令"))
            return
        v = space.verify
        cwd = self.cwd_for(space)
        started = datetime.now(UTC).astimezone().isoformat(timespec="milliseconds")
        self.bus.publish(status_frame(session_id, "verifying"))
        try:
            # 用阻塞式 subprocess 跑在默认线程池里，避免 asyncio 子进程 watcher
            # 在非主线程的 loop 上不好使的问题
            result = await asyncio.to_thread(
                subprocess.run,
                v.command,
                shell=True,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=v.timeout,
            )
        except subprocess.TimeoutExpired:
            self.bus.publish(error_frame(session_id, f"验证命令超时（>{v.timeout}s）"))
            return
        except Exception as e:  # noqa: BLE001
            self.bus.publish(error_frame(session_id, f"验证命令执行失败：{e}"))
            return
        status = "passed" if result.returncode == 0 else "failed"
        verification = {
            "status": status,
            "command": v.command,
            "exit_code": result.returncode,
            "started_at": started,
            "finished_at": datetime.now(UTC).astimezone().isoformat(timespec="milliseconds"),
            "output": (result.stdout or "")[-2000:],
            # 只在通过时冻结指纹：失败的结果本来就要重跑，没有「失效」一说
            "fingerprint": fingerprint(cwd) if status == "passed" else None,
            "source": "auto",
        }
        self.store.update_meta(space_id, session_id, verification=verification)
        self.bus.publish(verification_frame(session_id, verification))
        if status == "failed":
            self.panel.add_message(
                source="system",
                title=f"{space.name} · 验证未通过",
                body=f"{v.command}　退出码 {result.returncode}",
                level="error",
                ref={"space_id": space_id, "session_id": session_id},
            )

    def _default_llm_factory(self, name: str, profile: Any) -> Any:
        from simpleagent.llm.client import LLMClient
        from simpleagent.trace import Tracer

        tracer = Tracer(
            home_dir() / "traces",
            "serve",
            enabled=self.config.trace.enabled,
            raw_chunks=self.config.trace.raw_chunks,
        )
        return LLMClient(name, profile, tracer=tracer)
