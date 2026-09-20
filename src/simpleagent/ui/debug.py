"""debug 输出：把 API 调用和工具调用的过程写到 stderr。

正文仍然由 `ui.repl.Renderer` 写到 stdout，debug 行单独走 stderr，
这样 `sa run --debug 2>debug.log` 能把过程日志单独存一份，正文照样可以管道给别的程序。

不引入 rich：运行时依赖只有 openai 和 pydantic，十几个 ANSI 转义自己写就够了。
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, TextIO

from simpleagent.config import Config
from simpleagent.events import (
    ApiRequest,
    ApiResponse,
    Event,
    MaxStepsReached,
    MessageDone,
    ToolCallStart,
    ToolResult,
)

if TYPE_CHECKING:
    from simpleagent.ui.repl import Renderer

# debug 的三档：关 / 显示 API 与工具调用过程 / 再加消息清单和请求开关
DEBUG_LEVELS = ("off", "on", "verbose")
DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
CYAN, YELLOW, MAGENTA, RED = "\033[36m", "\033[33m", "\033[35m", "\033[31m"

# 判定结果的配色：灰=直接放行，品红=问过人，红=被拦下
DECISION_STYLE = {"allow": DIM, "ask": MAGENTA, "deny": RED}

PREVIEW_LINES = 5  # 工具结果在 debug 行里预览的行数
ARGUMENT_LIMIT = 160  # 参数预览的字符上限
RULE_WIDTH = 44  # 轮次分隔线的宽度


def supports_color(out: TextIO) -> bool:
    return hasattr(out, "isatty") and out.isatty() and not os.environ.get("NO_COLOR")


def debug_level(config: Config) -> str:
    """配置里的两个开关 → 一档 debug；verbose 隐含 enabled。"""
    if not config.debug.enabled:
        return "off"
    return "verbose" if config.debug.verbose else "on"


def fmt_bytes(count: int) -> str:
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{count / 1024:.1f} KB"
    return f"{count / 1024 / 1024:.1f} MB"


def fmt_ms(milliseconds: float) -> str:
    return f"{milliseconds:.0f}ms" if milliseconds < 1000 else f"{milliseconds / 1000:.2f}s"


def shorten(text: str, limit: int = ARGUMENT_LIMIT) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


class DebugRenderer:
    """把 debug 行写到 stderr，正文交给原来的 Renderer。

    工具相关的两类事件由这里接管——debug 行已经包含普通行的全部信息，
    两边都打就重复了。思考内容和正文仍走 Renderer。
    """

    def __init__(self, inner: Renderer, err: TextIO, color: bool, verbose: bool = False):
        self.inner = inner
        self.err = err
        self.color = color
        self.verbose = verbose

    # ------------------------------------------------------------------ 输出

    def _write(self, text: str, style: str = "") -> None:
        # stdout 通常是块缓冲，不先冲一遍的话两个流合起来看顺序会乱
        self.inner.out.flush()
        self.err.write(f"{style}{text}{RESET}" if style and self.color else text)
        self.err.flush()

    def _line(self, text: str, style: str = DIM) -> None:
        self._write(text + "\n", style)

    def on_event(self, event: Event) -> None:
        if isinstance(event, ApiRequest):
            self.api_request(event)
        elif isinstance(event, ApiResponse):
            self.api_response(event)
        elif isinstance(event, ToolCallStart):
            self.tool_start(event)
        elif isinstance(event, ToolResult):
            self.tool_result(event)
        # 统计行和步数上限提示由调用方打印；这里只保证正文分段收尾
        elif isinstance(event, MessageDone | MaxStepsReached):
            self.inner.end()
        else:
            self.inner.on_event(event)

    # ------------------------------------------------------------------ API

    def api_request(self, event: ApiRequest) -> None:
        self.inner.end()
        self._line(f"── 轮 {event.step} " + "─" * RULE_WIDTH)
        self._write("⟩ API", CYAN + BOLD)
        self._line(f"    POST {event.url}", CYAN)
        summary = f"{event.model} · {event.messages} msgs · {event.tools} tools"
        self._line(f"       {summary} · {fmt_bytes(event.payload_bytes)}")
        if not self.verbose:
            return
        if event.outline:
            roles = " · ".join(f"{m['role']} {m['chars']:,}B" for m in event.outline)
            self._line(f"       msgs  {shorten(roles, 200)}")
        if event.options:
            extra = event.options.get("extra_body")
            options = dict(event.options)
            # extra_body 只放键名，显示成 extra_body=enable_thinking,... 太吵，这里标一下就行
            if extra:
                options["extra_body"] = ",".join(extra) if isinstance(extra, list) else extra
            items = " · ".join(f"{key}={value}" for key, value in options.items())
            self._line(f"       opts  {shorten(items, 200)}")

    def api_response(self, event: ApiResponse) -> None:
        ok = event.status == "ok"
        self._write(f"⟨ {event.status_code or event.status}", (CYAN if ok else RED) + BOLD)
        parts = [f"{event.elapsed:.2f}s"]
        if event.ttft is not None:
            parts.append(f"ttft {event.ttft:.2f}s")
        if event.usage is not None:
            prompt = f"in {event.usage.prompt_tokens:,}"
            if event.usage.cached_tokens:
                prompt += f"(缓存 {event.usage.cached_tokens:,})"
            parts += [prompt, f"out {event.usage.completion_tokens:,}"]
        line = " · ".join(parts)
        if event.finish_reason:
            line += f" → {event.finish_reason}"
        self._line("   " + line, DIM if ok else RED)
        if event.error:
            self._line(f"       {shorten(event.error, 200)}", RED)

    # ------------------------------------------------------------------ 工具

    def tool_start(self, event: ToolCallStart) -> None:
        self.inner.end()
        self._write(f"▸ {event.name}", YELLOW + BOLD)
        self._write(f"  {shorten(event.arguments)}")
        self._write(f"  轮 {event.step} · {event.permission}")
        if not event.readonly:
            self._write(" · 写操作", MAGENTA)
        self._line("")

    def tool_result(self, event: ToolResult) -> None:
        lines = event.content.splitlines() or [""]
        if event.decision is not None:
            self._write(f"        判定 {event.decision}", DECISION_STYLE.get(event.decision, DIM))
        self._write(f" · {fmt_ms(event.duration_ms)}")
        if event.truncated:
            self._write(" · 已截断落盘", MAGENTA)
        self._line(f" · {len(lines)} 行 {fmt_bytes(len(event.content))}")
        style = RED if event.is_error else DIM
        for line in lines[:PREVIEW_LINES]:
            self._line(f"        {shorten(line)}", style)
        if len(lines) > PREVIEW_LINES:
            self._line(f"        …（共 {len(lines)} 行）")
