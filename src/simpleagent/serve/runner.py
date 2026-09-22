"""Runner：后台 asyncio 线程，把一次用户输入交给对应空间的 Agent 执行。

职责：
- 按空间的执行者分两条路径：`simpleagent` 用内置 loop（按目录 / profile 构造 Agent、注入审批器）；
  `claude-code` / `opencode` 起无头 CLI，把它的 NDJSON 翻译成同一套事件（见 agents/）。
- 两条路径的产出汇合在 `_on_event`：先落盘再广播。
- 跑 agent.run() / CLI，把事件实时发到事件总线，同时把消息和元信息落盘。
- 支持取消（内置 loop 取消底层 asyncio 任务；外部 CLI 直接 terminate 进程）。
- 支持手动触发验证命令，并把结果写成 verification 帧 + 落盘。
- 持有一个 McpManager：MCP server 在后台事件循环里启动一次，所有空间、所有会话共用。

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
from simpleagent.config import TOOL_OUTPUT_DIRNAME, Config, home_dir
from simpleagent.events import Event, MessageDone, ToolResult
from simpleagent.mcp.manager import McpManager
from simpleagent.panel.store import PanelStore
from simpleagent.permissions import Policy
from simpleagent.serve.approval import APIApprover, ApprovalDecision, PendingApprovals
from simpleagent.serve.bus import EventBus
from simpleagent.serve.frames import (
    error_frame,
    event_to_frame,
    status_frame,
    verification_frame,
)
from simpleagent.spaces.models import Space
from simpleagent.spaces.store import SpaceStore
from simpleagent.spaces.verify import changed_since, fingerprint
from simpleagent.tools import ToolRegistry, builtin_tools

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
        self._schedule(self._run_input(space_id, session_id, user_input))

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

    def verify(self, space_id: str, session_id: str) -> None:
        self._schedule(self._verify(space_id, session_id))

    # --------------------------------------------------------------- 内部实现
    async def _run_input(self, space_id: str, session_id: str, user_input: str) -> None:
        space = self.store.get_space(space_id)
        if space is None:
            return
        # 内置 agent 用「模型看到的历史」（清理、压缩、中断修复都落在里面）；外部 CLI 的上下文
        # 由它自己维护，这里只要用量。都要在落用户消息之前读，否则这条输入会被算进历史两次
        session = (
            self.store.load_model_session(space_id, session_id)
            if space.executor == "simpleagent"
            else self.store.load_session(space_id, session_id)
        )
        try:
            # 先落用户消息再构造 agent：起不来的时候（没配 key、profile 不存在、外部 CLI
            # 还没接入）也要把用户说的这句留下来，否则刷新页面它就不见了。
            self.store.append_message(space_id, session_id, {"role": "user", "content": user_input})
            self.store.update_meta(space_id, session_id, status="running")
            if space.executor == "simpleagent":
                # MCP server 还在启动就等它：工具列表要在第一次请求前定下来
                if self._mcp_started is not None:
                    await asyncio.wrap_future(self._mcp_started)
                self._agents[session_id] = self._build_agent(space, session)
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
            if space.executor == "simpleagent":
                agent = self._agents[session_id]
                async for event in agent.run(session, user_input):
                    self._on_event(space_id, session_id, event)
                status = "done"
            else:
                # 外部 CLI 走完全不同的执行路径，但生成的是同一套事件
                status, reason = await self._run_cli(space, session, user_input)
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
        extra: dict[str, Any] = {"space_id": space_id, "usage": usage}
        if reason is not None:
            extra["reason"] = reason
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
        self, space: Space, session: Session, user_input: str
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
        adapter = adapter_for(space.executor)
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
                    print(f"[runner] 解析 {space.executor} 事件失败：{e}", file=sys.stderr)
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
                error = f"{space.executor} 退出码 {code}"
                if stderr_tail:
                    error += f"：{stderr_tail}"
        if not ok:
            message = error or f"{space.executor} 运行失败"
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

    def _build_agent(self, space: Space, session: Session) -> Agent:
        # 外部执行者走 _run_cli，不在这里造 Agent。走到这儿说明调用方忘了分支——
        # 宁可炸掉也不能静默用内置 loop 跑，那会让用户以为在跑 claude。
        if space.executor != "simpleagent":
            raise RuntimeError(
                f"{space.executor} 要走 CLI 路径（_run_cli），_build_agent 只服务内置 loop"
            )
        if space.profile not in self.config.profiles:
            raise RuntimeError(
                f"空间用的 profile「{space.profile}」不在 config.toml 里，"
                f"可选：{'、'.join(sorted(self.config.profiles))}"
            )
        profile = self.config.profiles[space.profile]
        llm = self.llm_factory(space.profile, profile)
        tools = ToolRegistry(
            [*builtin_tools(), *self.mcp.tools()],
            max_output_chars=self.config.tool_output.max_chars,
            max_output_lines=self.config.tool_output.max_lines,
        )
        approver = APIApprover(self.bus, self.pending, self.always_allow)
        tools.approver = approver
        cwd = self.cwd_for(space)
        # 客户端模式也走同一套权限判定：越界和危险命令先被拦掉，剩下的才去问客户端
        tools.policy = Policy(cwd)
        output_dir = home_dir() / TOOL_OUTPUT_DIRNAME
        system_prompt = build_system_prompt(self.config.system_prompt, cwd=cwd)
        system_prompt += self.mcp.prompt_section()
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

    def cwd_for(self, space: Space) -> Path:
        """空间的工作目录：有 cwd 用它，没有就用 spaces/<id>/tmp。

        HTTP 层（文件树、验证）也要用同一个口径，所以是公开方法。
        """
        if space.cwd:
            return Path(space.cwd).expanduser()
        return self.store._space_dir(space.id) / "tmp"

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
