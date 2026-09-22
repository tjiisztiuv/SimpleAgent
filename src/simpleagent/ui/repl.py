"""交互式 REPL：读取输入 → 交给 Agent 执行 → 渲染事件。

状态（消息历史、用量、当前模型）在 Agent / Session 里，REPL 只负责输入输出。
用 asyncio.Runner 在多轮之间复用同一个事件循环（AsyncOpenAI 的连接池绑定在循环上）。
Runner 会把 Ctrl+C 转成当前任务的 CancelledError，Agent.run() 收到后修好历史再抛出。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

import openai

from simpleagent.agent.context import ContextUsage, last_turn_cut
from simpleagent.agent.loop import Agent, CompactError
from simpleagent.agent.prompt import build_system_prompt
from simpleagent.agent.session import Session, SessionStore
from simpleagent.config import (
    ENV_FILENAME,
    TOOL_OUTPUT_DIRNAME,
    Config,
    ConfigError,
    Profile,
    dev_checkout,
    home_dir,
)
from simpleagent.events import (
    ContextEdited,
    MaxStepsReached,
    MessageDone,
    ReasoningDelta,
    TextDelta,
    ToolCallStart,
    ToolResult,
)
from simpleagent.llm.client import LLM, LLMClient
from simpleagent.mcp.manager import McpManager
from simpleagent.permissions import Approver, Policy
from simpleagent.tools import ToolRegistry, builtin_tools
from simpleagent.trace import Tracer, new_session_id
from simpleagent.ui.approve import ConsoleApprover
from simpleagent.ui.debug import (
    DEBUG_LEVELS,
    DebugRenderer,
    DebugState,
    debug_level,
    supports_color,
)

try:
    import readline  # noqa: F401  让 input() 支持方向键和输入历史
except ImportError:  # pragma: no cover
    pass

DIM, RED, BOLD, RESET = "\033[2m", "\033[31m", "\033[1m", "\033[0m"

HELP = '''命令：
  /model [name]   查看或切换模型 profile（对话历史保留）
  /tools          列出当前可用的工具
  /mcp            查看 MCP server 的状态（连上没有、几个工具、重启过几次）
  /debug [LEVEL]  查看或切换 debug：off / on / verbose / full（过程输出走 stderr）
  /clear          清空对话历史
  /usage          本会话的 token 用量
  /context        上下文占了多少、离上限还有多远、由哪些部分构成
  /compact [重点]  把早期对话压成摘要，只留最后一轮原文；可以说明要保留的重点
  /help           显示帮助
  /exit           退出
多行输入：单独一行输入 """ 开始，再输入 """ 结束。
Ctrl+C 中断当前回复，Ctrl+D 退出。'''

TOOL_PREVIEW_LINES = 5  # 工具结果在终端里预览的行数；完整内容在 trace 里

LLMFactory = Callable[[str, Profile], LLM]


def format_stats(done: MessageDone, model: str) -> str:
    parts = [model]
    if done.usage:
        u = done.usage
        prompt = f"输入 {u.prompt_tokens:,}"
        if u.cached_tokens:
            prompt += f"（缓存 {u.cached_tokens:,}）"
        completion = f"输出 {u.completion_tokens:,}"
        if u.reasoning_tokens:
            completion += f"（思考 {u.reasoning_tokens:,}）"
        parts += [prompt, completion]
    else:
        parts.append("无 usage 数据")
    if done.ttft is not None:
        parts.append(f"首字 {done.ttft:.2f}s")
    parts.append(f"耗时 {done.elapsed:.2f}s")
    if done.finish_reason == "length":
        parts.append("输出达到 max_tokens 被截断")
    return "[" + " · ".join(parts) + "]"


def format_context(usage: ContextUsage, parts: dict[str, int], tool_count: int) -> list[str]:
    """/context 的输出：总量和上限、精确值与估算各占多少、构成、估算规则的误差。"""
    lines = [
        f"上下文 {usage.tokens:,} / {usage.limit:,} token（{usage.ratio:.0%}）"
        f" · 窗口 {usage.window:,}，给输出留 {usage.reserve:,}"
    ]
    if usage.exact:
        estimated = usage.tokens - usage.exact
        lines.append(f"  {usage.exact:,} 来自上次请求的实际用量，之后新增 {estimated:,} 为估算")
    elif usage.factor is not None:
        lines.append(
            f"  上次的实际用量已作废（清理 / 压缩过），按字符估再乘校准系数 {usage.factor:.2f}"
        )
    else:
        lines.append(
            "  还没有实际用量（没请求过、刚恢复会话、换过模型或服务不返回 usage），全部按字符估"
        )
    lines.append(
        "  构成（估算）："
        + " · ".join(
            [
                f"系统提示 {parts.get('system', 0):,}",
                f"工具 {tool_count} 个 {parts.get('tools', 0):,}",
                f"用户 {parts.get('user', 0):,}",
                f"助手 {parts.get('assistant', 0):,}",
                f"工具结果 {parts.get('tool', 0):,}",
            ]
        )
    )
    if usage.exact:
        diff = usage.exact_estimate / usage.exact - 1
        lines.append(
            f"  校准：上次请求实际 {usage.exact:,}，同一段按字符估 {usage.exact_estimate:,}"
            f"（{'高' if diff >= 0 else '低'}估 {abs(diff):.0%}）"
        )
    return lines


def describe_error(error: openai.APIError, profile: Profile) -> str:
    if isinstance(error, openai.APIStatusError):
        text = f"HTTP {error.status_code}：{error.message}"
    else:
        text = f"{type(error).__name__}：{error}"
    if isinstance(error, openai.AuthenticationError) and profile.api_key_env:
        text += (
            f"\n→ 检查 API key：{profile.api_key_env}，"
            f"环境变量优先，其次 {home_dir() / ENV_FILENAME}"
        )
    return text


def _clip(line: str, width: int = 200) -> str:
    return line if len(line) <= width else line[:width] + "…"


class Renderer:
    """把事件流渲染到终端：思考内容和工具调用灰色显示，正文正常显示，工具出错红色显示。"""

    def __init__(self, out: TextIO, color: bool, show_reasoning: bool):
        self.out = out
        self.color = color
        self.show_reasoning = show_reasoning
        self.mode: str | None = None  # None / "reasoning" / "text"
        self.last_call_id: str | None = None  # 刚显示过调用行、还没显示结果的 tool_call

    def _write(self, text: str, style: str = "") -> None:
        self.out.write(f"{style}{text}{RESET}" if style and self.color else text)
        self.out.flush()

    def on_event(self, event: TextDelta | ReasoningDelta | ToolCallStart | ToolResult) -> None:
        if isinstance(event, ToolCallStart):
            self.tool_start(event)
        elif isinstance(event, ToolResult):
            self.tool_result(event)
        elif isinstance(event, ReasoningDelta):
            if not self.show_reasoning:
                return
            if self.mode != "reasoning":
                self._write("思考：", DIM)
                self.mode = "reasoning"
            self._write(event.text, DIM)
        elif isinstance(event, TextDelta):
            if self.mode == "reasoning":
                self._write("\n\n")
            self.mode = "text"
            self._write(event.text)
        # 其余事件（ApiRequest / ApiResponse）只出现在 debug 输出里，普通渲染不处理

    def tool_start(self, event: ToolCallStart) -> None:
        self.end()
        self._write(f"→ {event.name} {_clip(event.arguments)}\n", DIM)
        self.last_call_id = event.call_id

    def tool_result(self, event: ToolResult) -> None:
        # 一次并行调用多个工具时，结果不紧跟在自己的调用行后面，加一行标明是谁的结果
        if event.call_id != self.last_call_id:
            self._write(f"← {event.name}\n", DIM)
        self.last_call_id = None
        lines = event.content.splitlines() or [""]
        shown = [_clip(line) for line in lines[:TOOL_PREVIEW_LINES]]
        if len(lines) > TOOL_PREVIEW_LINES:
            shown.append(f"…（共 {len(lines)} 行）")
        self._write("".join(f"  {line}\n" for line in shown), RED if event.is_error else DIM)

    def end(self) -> None:
        if self.mode is not None:
            self._write("\n")
            self.mode = None


class Repl:
    def __init__(
        self,
        config: Config,
        profile: str | None = None,
        *,
        llm_factory: LLMFactory | None = None,
        out: TextIO | None = None,
        input_fn: Callable[[str], str] = input,
        session: Session | None = None,
        store: SessionStore | None = None,
        approver: Approver | None = None,
        policy: Policy | None = None,
        err: TextIO | None = None,
        debug: str | None = None,  # off / on / verbose / full；不给就听配置的
    ):
        self.config = config
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self.color = supports_color(self.out)
        self.err_color = supports_color(self.err)
        self.debug = debug or debug_level(config)
        # full 档跨轮只打新增的消息：历史是往后追加的，前面那些上一轮已经打过了
        self.debug_state = DebugState()
        self.input_fn = input_fn
        self.store = store
        self.session = session or Session(new_session_id())
        self.resumed = session is not None
        self.tracer = Tracer(
            home_dir() / "traces",
            self.session.id,
            enabled=config.trace.enabled,
            raw_chunks=config.trace.raw_chunks,
        )
        self.llm_factory = llm_factory or (lambda name, p: LLMClient(name, p, tracer=self.tracer))
        cwd = Path.cwd()
        output_dir = home_dir() / TOOL_OUTPUT_DIRNAME
        name = profile or config.default_profile
        if name not in config.profiles:
            raise ConfigError(f"没有名为 '{name}' 的 profile")
        self.agent = Agent(
            llm=self._make_llm(name),
            tools=ToolRegistry(
                builtin_tools(),
                max_output_chars=config.tool_output.max_chars,
                max_output_lines=config.tool_output.max_lines,
                approver=approver or ConsoleApprover(input_fn=input_fn, out=self.out),
                policy=policy or Policy(cwd),
            ),
            system_prompt=build_system_prompt(config.system_prompt, cwd=cwd),
            cwd=cwd,
            max_steps=config.max_steps,
            output_dir=output_dir,
            hidden_env=config.api_key_env_names(),
            context=config.context,
        )
        # MCP server 在 run() 里、同一个事件循环中启动：子进程和管道都绑定在循环上
        self.mcp = McpManager(config.mcp_servers)
        # 新开的会话挂上存储：之后的每条消息都会写进 sessions/<id>.jsonl。
        # 放在创建模型客户端之后——缺 API key 时上面就抛错了，不能留下一条空会话
        if store is not None and session is None:
            store.start(self.session, profile=name, cwd=str(cwd))

    def _make_llm(self, name: str) -> LLM:
        if name not in self.config.profiles:
            raise ConfigError(f"没有名为 '{name}' 的 profile")
        return self.llm_factory(name, self.config.profiles[name])

    def print(self, text: str = "", style: str = "") -> None:
        if style and self.color:
            text = f"{style}{text}{RESET}"
        self.out.write(text + "\n")
        self.out.flush()

    # ------------------------------------------------------------------ 主循环

    def run(self) -> int:
        llm = self.agent.llm
        self.print(f"SimpleAgent · {llm.name}（{llm.profile.model}）", BOLD)
        if dev_checkout():
            self.print(f"开发模式：数据目录 {home_dir()}", DIM)
        if self.resumed:
            self.print(
                f"继续会话 {self.session.id}（已恢复 {len(self.session.messages)} 条历史）", DIM
            )
        if self.config.trace.enabled:
            self.print(f"trace：{self.tracer.dir}", DIM)
        if self.debug != "off":
            self.print(f"debug：{self.debug}，API 与工具调用的过程输出走 stderr", DIM)
        with asyncio.Runner() as runner:
            try:
                self._start_mcp(runner)
                self.print("输入 /help 查看命令", DIM)
                while True:
                    try:
                        line = self._read_input()
                    except KeyboardInterrupt:
                        self.print()
                        continue
                    except EOFError:
                        self.print()
                        break
                    if not line.strip():
                        continue
                    try:
                        if not runner.run(self.handle(line)):
                            break
                    except KeyboardInterrupt:
                        self.print("[已中断]", DIM)
            finally:
                runner.run(self.mcp.close())
                runner.run(self.agent.llm.close())
        return 0

    def _start_mcp(self, runner: asyncio.Runner) -> None:
        """启动配置里的 MCP server，把它们的工具注册进来。等全部有结果再接受第一个问题：
        工具列表在第一次请求前定下来，会话里就不再变，前缀缓存才能命中。"""
        names = [server.name for server in self.mcp.enabled]
        if not names:
            return
        self.print(f"启动 MCP server：{'、'.join(names)}（按 Ctrl+C 跳过）", DIM)
        try:
            runner.run(self.mcp.start())
        except KeyboardInterrupt:
            self.print("[已跳过还没启动好的 MCP server]", DIM)
        for tool in self.mcp.tools():
            self.agent.tools.register(tool)
        self.agent.system_prompt += self.mcp.prompt_section()
        if summary := self.mcp.summary():
            hint = "（/mcp 看详情）" if self.mcp.failed else ""
            self.print(summary + hint, RED if self.mcp.failed else DIM)

    def _read_input(self) -> str:
        line = self.input_fn("> ")
        if line.strip() != '"""':
            return line
        lines = []
        while (next_line := self.input_fn("... ")).strip() != '"""':
            lines.append(next_line)
        return "\n".join(lines)

    async def handle(self, line: str) -> bool:
        """处理一行输入；返回 False 表示退出。"""
        if line.startswith("/"):
            return await self.command(line)
        await self.chat(line)
        return True

    # ------------------------------------------------------------------ 对话

    async def chat(self, text: str) -> None:
        # 历史的维护（包括中断、出错后的修复）都在 Agent.run() 里，这里只负责显示
        renderer = Renderer(self.out, self.color, self.config.show_reasoning)
        # debug 行走 stderr，正文走 stdout：互不干扰，也方便 2> 单独存一份
        debug = (
            None
            if self.debug == "off"
            else DebugRenderer(
                renderer,
                self.err,
                self.err_color,
                level=self.debug,
                body_chars=self.config.debug.body_chars,
                state=self.debug_state,
            )
        )
        try:
            async for event in self.agent.run(self.session, text):
                if isinstance(event, MessageDone):
                    # 统计在 ApiResponse 行里已经打过了，debug 模式下不重复；
                    # full 档还要靠这个事件打模型返回的结构
                    if debug is not None:
                        debug.on_event(event)
                    else:
                        renderer.end()
                        self.print(format_stats(event, self.agent.llm.profile.model), DIM)
                elif isinstance(event, MaxStepsReached):
                    self.print(
                        f"[达到 max_steps={event.max_steps}，本轮停止；输入“继续”可以接着做]", DIM
                    )
                elif isinstance(event, ContextEdited):
                    renderer.end()
                    self.print(f"[{event.summary()}]", DIM)
                elif debug is not None:
                    debug.on_event(event)
                else:
                    renderer.on_event(event)
        except asyncio.CancelledError:
            renderer.end()
            raise
        except openai.APIError as e:
            renderer.end()
            self.print(f"请求失败：{describe_error(e, self.agent.llm.profile)}", RED)

    # ------------------------------------------------------------------ 命令

    async def command(self, line: str) -> bool:
        name, _, arg = line[1:].strip().partition(" ")
        arg = arg.strip()
        match name:
            case "exit" | "quit":
                return False
            case "help":
                self.print(HELP)
            case "clear":
                # 走 truncate 才会写进 JSONL；直接清列表的话，--resume 之后历史又回来了
                self.session.truncate(0)
                self.print("已清空对话历史")
            case "usage":
                u = self.session.usage
                self.print(
                    f"请求 {self.session.requests} 次"
                    f" · 输入 {u.prompt_tokens:,}（缓存 {u.cached_tokens:,}）"
                    f" · 输出 {u.completion_tokens:,}（思考 {u.reasoning_tokens:,}）"
                )
            case "context":
                lines = format_context(
                    self.agent.context_usage(self.session),
                    self.agent.context_breakdown(self.session),
                    len(self.agent.tools.schemas()),
                )
                self.print(lines[0])
                for line in lines[1:]:
                    self.print(line, DIM)
            case "compact":
                await self._compact(arg)
            case "model":
                await self._switch_model(arg)
            case "tools":
                for item in self.agent.tools.schemas():
                    function = item["function"]
                    self.print(f"  {function['name']}", BOLD)
                    self.print(f"      {function['description']}")
            case "mcp":
                if not self.mcp.servers:
                    self.print("没有配置 MCP server：在 config.toml 里加 [mcp_servers.<名字>]")
                for line in self.mcp.describe():
                    self.print(line)
            case "debug":
                self._set_debug(arg)
            case _:
                self.print(f"未知命令 /{name}，输入 /help 查看")
        return True

    async def _compact(self, instructions: str) -> None:
        # 只留最后一轮：手动压缩就是想腾地方，按自动压缩的 1/4 留，对话不长时几乎压不掉什么
        cut = last_turn_cut(self.session.messages)
        if cut is None:
            self.print("没有可以压缩的内容（历史太短，或者只剩上一次的摘要）", DIM)
            return
        try:
            edited = await self.agent.compact(self.session, instructions or None, cut=cut)
        except CompactError as e:
            self.print(f"压缩失败：{e}", RED)
            return
        if edited is None:
            self.print("没有可以压缩的内容（历史太短，或者只剩上一次的摘要）", DIM)
            return
        self.print(f"[{edited.summary()}]", DIM)

    def _set_debug(self, level: str) -> None:
        if not level:
            self.print(f"debug：{self.debug}（可选 {' / '.join(DEBUG_LEVELS)}）", DIM)
            return
        if level not in DEBUG_LEVELS:
            self.print(f"用法：/debug [{' | '.join(DEBUG_LEVELS)}]", RED)
            return
        self.debug = level
        self.debug_state = DebugState()  # 换档后重新打一遍完整上下文
        self.print(
            f"debug：{level}" + ("" if level == "off" else "，过程输出走 stderr"),
            DIM,
        )

    async def _switch_model(self, name: str) -> None:
        if not name:
            for profile_name, profile in self.config.profiles.items():
                mark = "*" if profile_name == self.agent.llm.name else " "
                self.print(f" {mark} {profile_name:<12} {profile.model}  {profile.base_url}")
            return
        try:
            llm = self._make_llm(name)
        except ConfigError as e:
            self.print(f"切换失败：{e}", RED)
            return
        await self.agent.llm.close()
        self.agent.llm = llm
        self.print(f"已切换到 {name}（{llm.profile.model}），对话历史保留")
