"""headless 前端：`sa run "..."` 跑一个任务就退出，不读 stdin。

和 REPL 的差别只有两处：

- 审批器换成 `WhitelistApprover`：名单外的写操作一律拒绝，**不再试图问人**。
  没有人在屏幕前的时候，「需要确认」和「拒绝」是同一件事，区别只在要不要把原因告诉模型。
- 渲染更朴素：正文直出，工具调用一行概要，不带颜色也不做分段。

M4 的 `sa daemon` 会把同一个核心换成「按时间表触发」，前台渲染换成写运行日志，
审批仍然走这个白名单。所以这里不写任何和交互强绑定的东西。
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, TextIO

import openai

from simpleagent.agent.loop import Agent
from simpleagent.agent.prompt import build_system_prompt
from simpleagent.agent.session import Session, SessionStore
from simpleagent.config import TOOL_OUTPUT_DIRNAME, Config, Profile, home_dir
from simpleagent.events import (
    ContextEdited,
    MaxStepsReached,
    MessageDone,
    TextDelta,
    ToolCallStart,
    ToolResult,
)
from simpleagent.llm.client import LLM, LLMClient
from simpleagent.mcp.manager import McpManager
from simpleagent.permissions import Policy, WhitelistApprover
from simpleagent.tools import ToolRegistry, builtin_tools
from simpleagent.trace import Tracer, new_session_id
from simpleagent.ui.debug import DebugRenderer, debug_level, supports_color
from simpleagent.ui.repl import Renderer

LLMFactory = Callable[[str, Profile], LLM]

TOOL_PREVIEW_LINES = 3  # 工具结果只露这么几行，完整内容在 trace


def format_usage(done: MessageDone, model: str) -> str:
    parts = [model]
    if done.usage:
        parts.append(f"输入 {done.usage.prompt_tokens:,}")
        parts.append(f"输出 {done.usage.completion_tokens:,}")
    parts.append(f"耗时 {done.elapsed:.2f}s")
    return "[" + " · ".join(parts) + "]"


def _clip(text: Any, limit: int = 300) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


class Headless:
    def __init__(
        self,
        config: Config,
        profile: str | None = None,
        *,
        cwd: Path | None = None,
        allowed_tools: Iterable[str] = (),
        llm_factory: LLMFactory | None = None,
        out: TextIO | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
        err: TextIO | None = None,
        debug: str | None = None,  # off / on / verbose / full；不给就听配置的
    ) -> None:
        self.config = config
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self.err_color = supports_color(self.err)
        self.debug = debug or debug_level(config)
        self.cwd = cwd or Path.cwd()
        self.store = store
        self.session = session or Session(new_session_id())
        name = profile or config.default_profile
        if name not in config.profiles:
            raise ValueError(f"没有名为 '{name}' 的 profile")
        self.tracer = Tracer(
            home_dir() / "traces",
            self.session.id,
            enabled=config.trace.enabled,
            raw_chunks=config.trace.raw_chunks,
        )
        factory = llm_factory or (lambda n, p: LLMClient(n, p, tracer=self.tracer))
        tools = ToolRegistry(
            builtin_tools(),
            max_output_chars=config.tool_output.max_chars,
            max_output_lines=config.tool_output.max_lines,
            approver=WhitelistApprover(allowed_tools),
            policy=Policy(self.cwd),
        )
        self.agent = Agent(
            llm=factory(name, config.profiles[name]),
            tools=tools,
            system_prompt=build_system_prompt(config.system_prompt, cwd=self.cwd),
            cwd=self.cwd,
            max_steps=config.max_steps,
            output_dir=home_dir() / TOOL_OUTPUT_DIRNAME,
            hidden_env=config.api_key_env_names(),
            context=config.context,
        )
        self.mcp = McpManager(config.mcp_servers)
        # 会话由 CLI 恢复时（sa run --resume）已经落过盘了，这里只负责新开的会话
        if store is not None and session is None:
            store.start(self.session, profile=name, cwd=str(self.cwd))

    def _write(self, text: str) -> None:
        self.out.write(text)
        self.out.flush()

    async def run(self, prompt: str) -> int:
        """跑一个任务，返回退出码（0 正常，1 请求失败）。

        MCP server 启动失败只警告，不影响退出码：没有它，很多任务照样能做完。
        """
        await self._start_mcp()
        try:
            return await self._run(prompt)
        finally:
            await self.mcp.close()

    async def _start_mcp(self) -> None:
        if not self.mcp.enabled:
            return
        await self.mcp.start()
        for tool in self.mcp.tools():
            self.agent.tools.register(tool)
        self.agent.system_prompt += self.mcp.prompt_section()
        # 状态走 stderr：`sa run ... > out.txt` 的正文里不混进这些
        self.err.write(self.mcp.summary() + "\n")
        for server in self.mcp.servers:
            if server.state == "failed" and server.error:
                self.err.writelines(f"  {line}\n" for line in server.error.splitlines())
        self.err.flush()

    async def _run(self, prompt: str) -> int:
        model = self.agent.llm.profile.model
        # debug 行写 stderr；正文写 stdout。inner 只用来收尾正文分段和 flush，不参与渲染
        inner = Renderer(self.out, color=False, show_reasoning=False)
        debug = (
            None
            if self.debug == "off"
            else DebugRenderer(
                inner,
                self.err,
                self.err_color,
                level=self.debug,
                body_chars=self.config.debug.body_chars,
            )
        )
        try:
            async for event in self.agent.run(self.session, prompt):
                if isinstance(event, MessageDone):
                    # 统计在 ApiResponse 行里已经打过了；full 档还要打模型返回的结构
                    if debug is not None:
                        debug.on_event(event)
                    else:
                        self._write(f"\n{format_usage(event, model)}\n")
                elif isinstance(event, MaxStepsReached):
                    self._write(f"\n[达到 max_steps={event.max_steps}，本轮停止]\n")
                elif isinstance(event, ContextEdited):
                    self._write(f"\n[{event.summary()}]\n")
                elif debug is not None:
                    debug.on_event(event)
                elif isinstance(event, TextDelta):
                    self._write(event.text)
                elif isinstance(event, ToolCallStart):
                    self._write(f"\n→ {event.name} {_clip(event.arguments)}\n")
                elif isinstance(event, ToolResult):
                    lines = event.content.splitlines() or [""]
                    for line in lines[:TOOL_PREVIEW_LINES]:
                        self._write(f"  {_clip(line)}\n")
                    if len(lines) > TOOL_PREVIEW_LINES:
                        self._write(f"  …（共 {len(lines)} 行）\n")
        except openai.APIError as e:
            self._write(f"\n请求失败：{type(e).__name__}：{e}\n")
            return 1
        return 0
