"""debug 输出：把 API 调用和工具调用的过程写到 stderr。

正文仍然由 `ui.repl.Renderer` 写到 stdout，debug 行单独走 stderr，
这样 `sa run --debug 2>debug.log` 能把过程日志单独存一份，正文照样可以管道给别的程序。

不引入 rich：运行时依赖只有 openai 和 pydantic，十几个 ANSI 转义自己写就够了。
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any, TextIO

from simpleagent.config import Config
from simpleagent.events import (
    REASONING_KEY,
    ApiRequest,
    ApiResponse,
    Event,
    MaxStepsReached,
    MessageDone,
    ToolCallStart,
    ToolResult,
    message_outline,
)

if TYPE_CHECKING:
    from simpleagent.ui.repl import Renderer

# debug 四档：关 / API 与工具调用过程 / 再加消息清单和请求开关 / 再加请求与响应的正文
DEBUG_LEVELS = ("off", "on", "verbose", "full")
DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
CYAN, YELLOW, MAGENTA, RED, GREEN = "\033[36m", "\033[33m", "\033[35m", "\033[31m", "\033[32m"

# 判定结果的配色：灰=直接放行，品红=问过人，红=被拦下
DECISION_STYLE = {"allow": DIM, "ask": MAGENTA, "deny": RED}

# 消息 role 的配色，full 档展开正文时用来区分是谁说的
ROLE_STYLE = {"system": MAGENTA, "user": GREEN, "assistant": CYAN, "tool": YELLOW}

PREVIEW_LINES = 5  # 工具结果在 debug 行里预览的行数
ARGUMENT_LIMIT = 160  # 参数预览的字符上限
RULE_WIDTH = 44  # 轮次分隔线的宽度
BODY_LINES = 30  # full 档里每条消息正文的行数上限
BODY_WIDTH = 200  # full 档里正文单行的宽度上限


def supports_color(out: TextIO) -> bool:
    return hasattr(out, "isatty") and out.isatty() and not os.environ.get("NO_COLOR")


def debug_level(config: Config) -> str:
    """配置里的三个开关 → 一档 debug。

    full 是最高的档，单独打开就生效；enabled / verbose 仍按原来的递进关系
    （只开 verbose 不算数，得先开 enabled）。
    """
    if config.debug.full:
        return "full"
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


def prettify_json(raw: str) -> str:
    """工具调用参数的 JSON 串转成缩进后的样子；解析不出结构就原样返回。

    模型给的 arguments 是流式拼起来的原始字符串，可能本来就不是合法 JSON
    （被截断、或掺了别的东西），这种情况原样打出来比吞掉更有用。
    """
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if not isinstance(parsed, (dict, list)):
        return raw
    return json.dumps(parsed, ensure_ascii=False, indent=2)


def clip(text: str, width: int = BODY_WIDTH) -> str:
    """按宽度截断，但保留行首的缩进——正文块里缩进本身是有信息的。"""
    return text if len(text) <= width else text[:width] + "…"


def body_lines(text: str, limit: int = BODY_LINES) -> list[str]:
    """一段正文按行切开：单行按 BODY_WIDTH 截断，总行数超过 limit 时留开头并提示。

    和 `shorten()` 的区别是保留原有的换行和缩进——正文块要能看出原来的结构。
    提示里的「全文见 trace」不是客套：trace 里存的就是同一份内容。
    """
    lines = [clip(line) for line in text.splitlines()]
    if not lines:
        return []
    if len(lines) <= limit:
        return lines
    return [*lines[:limit], f"…还有 {len(lines) - limit} 行（全文见 trace）"]


def content_text(content: Any) -> str:
    """消息的 content 转成可打印文本；不是字符串（多模态之类）就按 JSON 展开。"""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False, indent=2)


def message_body(message: dict[str, Any]) -> list[str]:
    """一条消息的正文行；正文为空、或还带着工具调用时给一句说明。

    tool_calls 是和 content 并列的字段，光看正文会漏掉它——而它往往才是
    这条消息真正的内容（content 为空、只调工具的情况很常见）。
    """
    text = content_text(message.get("content"))
    calls = len(message.get("tool_calls") or [])
    if not text:
        return ["（空）"] if not calls else [f"（空 · 带 {calls} 个工具调用）"]
    lines = body_lines(text)
    if calls:
        lines.append(f"（另有 {calls} 个工具调用）")
    return lines


def call_lines(index: int, call: dict[str, Any]) -> list[str]:
    """一次工具调用的展示行：名字一行，参数按 JSON 缩进跟在下面。"""
    function = call.get("function") or {}
    name = function.get("name") or "?"
    arguments = prettify_json(function.get("arguments") or "")
    if not arguments:
        return [f"{index}. {name}  （无参数）"]
    return [f"{index}. {name}", *[f"   {line}" for line in body_lines(arguments)]]


class DebugRenderer:
    """把 debug 行写到 stderr，正文交给原来的 Renderer。

    工具相关的两类事件由这里接管——debug 行已经包含普通行的全部信息，
    两边都打就重复了。思考内容和正文仍走 Renderer。

    level 决定展开到多细：on 只报摘要，verbose 加消息清单和请求开关，
    full 再把真正发出去的 messages 和模型的返回逐条展开。
    """

    def __init__(self, inner: Renderer, err: TextIO, color: bool, level: str = "on"):
        self.inner = inner
        self.err = err
        self.color = color
        self.level = level
        self.verbose = level in ("verbose", "full")
        self.full = level == "full"

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
        elif isinstance(event, MessageDone):
            self.message_done(event)
        # 步数上限提示由调用方打印；这里只保证正文分段收尾
        elif isinstance(event, MaxStepsReached):
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
        if self.full:
            # full 档展开正文，正文块里已经带了每条的大小，摘要行就不重复了
            self.messages_block(event)
        elif event.outline:
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

    def messages_block(self, event: ApiRequest) -> None:
        """full 档的请求正文：逐条列出真正发出去的消息，末尾跟一行工具名。

        `sent` 是 `prepare_messages` 之后的结果，和 API 收到的一模一样：
        看「模型到底看到了什么」靠这一块，不用去翻 trace。
        """
        self._line("       messages")
        sizes = message_outline(event.sent)
        for index, message in enumerate(event.sent):
            role = message.get("role", "?")
            # outline 和 sent 同源同序，正常一定对得上；对不上时不显示大小，不编一个
            chars = sizes[index]["chars"] if index < len(sizes) else None
            self._write(f"         [{index}] ", DIM)
            self._write(role, ROLE_STYLE.get(role, "") + BOLD)
            self._line("" if chars is None else f" · {fmt_bytes(chars)}")
            for line in message_body(message):
                # 空行不补尾随空格，正文块的竖线本身足够标出层级
                self._line(f"         │ {line}" if line else "         │")
        if event.tool_names:
            self._line(f"       tools  {shorten(' · '.join(event.tool_names), 200)}")

    def message_done(self, event: MessageDone) -> None:
        """full 档的响应正文：模型的返回按 content / reasoning / tool_calls 展开。

        只有成功路径走得到这里；失败和中断在 ApiResponse 那行就结束了
        （本来也没有完整的返回内容可展开）。
        """
        self.inner.end()
        if not self.full:
            return
        message = event.message
        parts: list[tuple[str, list[str]]] = []
        parts.append(("content", body_lines(content_text(message.get("content"))) or ["（空）"]))
        reasoning = message.get(REASONING_KEY) or ""
        if reasoning:
            parts.append((f"reasoning · {fmt_bytes(len(reasoning))}", body_lines(reasoning)))
        calls = message.get("tool_calls") or []
        if calls:
            lines = [line for i, call in enumerate(calls, 1) for line in call_lines(i, call)]
            parts.append((f"tool_calls · {len(calls)} 个", lines))
        self._line("   message")
        for index, (label, lines) in enumerate(parts):
            last = index == len(parts) - 1
            self._write(f"   {'└' if last else '├'} ", DIM)
            self._line(label, BOLD)
            prefix = "     " if last else "   │ "
            for line in lines:
                self._line(f"{prefix}{line}")

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
