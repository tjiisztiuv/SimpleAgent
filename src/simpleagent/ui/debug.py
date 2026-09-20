"""debug 输出：把 API 调用和工具调用的过程写到 stderr。

正文仍然由 `ui.repl.Renderer` 写到 stdout，debug 行单独走 stderr，
这样 `sa run --debug 2>debug.log` 能把过程日志单独存一份，正文照样可以管道给别的程序。

不引入 rich：运行时依赖只有 openai 和 pydantic，十几个 ANSI 转义自己写就够了。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TextIO

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
from simpleagent.llm.client import REASONING_KEY

if TYPE_CHECKING:
    from simpleagent.ui.repl import Renderer

# debug 的四档：关 / API 与工具调用过程 / 再加消息清单和请求开关 / 再加请求体与响应正文
DEBUG_LEVELS = ("off", "on", "verbose", "full")
DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
CYAN, YELLOW, MAGENTA, RED = "\033[36m", "\033[33m", "\033[35m", "\033[31m"

# 判定结果的配色：灰=直接放行，品红=问过人，红=被拦下
DECISION_STYLE = {"allow": DIM, "ask": MAGENTA, "deny": RED}

PREVIEW_LINES = 5  # 工具结果在 debug 行里预览的行数
ARGUMENT_LIMIT = 160  # 参数预览的字符上限
RULE_WIDTH = 44  # 轮次分隔线的宽度
BODY_CHARS = 600  # full 档里每段正文的字符上限
BODY_LINES = 12  # full 档里每段正文最多打几行
INLINE_JSON = 100  # 工具参数短于这个长度就压成一行，不展开缩进
WRAP_WIDTH = 88  # 工具清单折行的宽度


def supports_color(out: TextIO) -> bool:
    return hasattr(out, "isatty") and out.isatty() and not os.environ.get("NO_COLOR")


def debug_level(config: Config) -> str:
    """配置里的开关 → 一档 debug；full 隐含 verbose，verbose 隐含 enabled。"""
    debug = config.debug
    if debug.full:
        return "full"
    if debug.verbose:
        return "verbose"
    return "on" if debug.enabled else "off"


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


# ---------------------------------------------------------------- full 档的格式化


def preview_text(text: str, limit: int = BODY_CHARS, max_lines: int = BODY_LINES) -> list[str]:
    """正文预览：先按字符上限截，再按行数上限截，截掉了就补一行说明有多少。

    limit / max_lines 给 0 表示不截。单行不再另外裁剪——body_chars 是唯一的闸门，
    超宽的行交给终端折行，这样看到的就是真实发出去的样子。
    """
    if not text:
        return []
    clipped = text if limit <= 0 or len(text) <= limit else text[:limit]
    lines = clipped.splitlines() or [clipped]
    note = ""
    if 0 < max_lines < len(lines):
        lines = lines[:max_lines]
        note = f"…（共 {len(text.splitlines())} 行 {len(text):,} 字）"
    elif len(clipped) < len(text):
        note = f"…（共 {len(text):,} 字）"
    return [*lines, note] if note else lines


def pretty_json(text: str, limit: int = BODY_CHARS) -> tuple[list[str], bool]:
    """工具参数：能解析就（短则压成一行，长则）缩进展开；不能解析原样预览并返回 False。

    返回 False 的那一路正是最值得看的——模型吐了半截 JSON 或夹了别的东西。
    """
    if not text.strip():
        return [], True
    try:
        data = json.loads(text)
    except ValueError:
        return preview_text(text, limit), False
    inline = json.dumps(data, ensure_ascii=False)
    if len(inline) <= INLINE_JSON:
        return [inline], True
    return preview_text(json.dumps(data, ensure_ascii=False, indent=2), limit), True


def format_tool_call(call: dict[str, Any], limit: int = BODY_CHARS) -> list[str]:
    """一个 tool_call → 抬头行（可能带参数）+ 缩进的参数行。"""
    function = call.get("function") or {}
    head = f"⚙ {function.get('name') or '?'}"
    if call.get("id"):
        head += f" #{call['id']}"
    lines, valid = pretty_json(function.get("arguments") or "", limit)
    if not valid:
        return [f"{head}  ⚠ 参数不是合法 JSON", *[f"  {line}" for line in lines]]
    if len(lines) == 1:
        return [f"{head}  {lines[0]}"]
    return [head, *[f"  {line}" for line in lines]]


def format_message(message: dict[str, Any], index: int, limit: int = BODY_CHARS) -> list[str]:
    """请求体里的一条消息 → 抬头行（序号 / role / 规模）+ 缩进的正文和工具调用。"""
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif content:  # 多模态的 content 是数组，原样转成 JSON 看结构
        text = json.dumps(content, ensure_ascii=False)
    else:
        text = ""
    calls = message.get("tool_calls") or []
    head = f"[{index}] {message.get('role', '?')}"
    if message.get("tool_call_id"):
        head += f" #{message['tool_call_id']}"
    if text:
        head += f"  {len(text):,} 字"
        if (line_count := len(text.splitlines())) > 1:
            head += f" · {line_count} 行"
    elif not calls:
        head += "  （空）" if content == "" else "  （content=null）"
    if calls:
        head += f"  tool_calls={len(calls)}"
    lines = [head]
    lines += [f"    {line}" for line in preview_text(text, limit)]
    for call in calls:
        lines += [f"    {line}" for line in format_tool_call(call, limit)]
    return lines


def format_tools(tools: list[dict[str, Any]], width: int = WRAP_WIDTH) -> list[str]:
    """工具清单：名字 + 参数字段名，折行返回。

    完整 JSON Schema 去 traces/ 看——它几千字符且基本不变，这里只要能确认
    「这轮带了哪些工具、签名对不对」。
    """
    parts = []
    for tool in tools:
        function = tool.get("function") or {}
        properties = (function.get("parameters") or {}).get("properties") or {}
        name = function.get("name", "?")
        parts.append(f"{name}({','.join(properties)})" if properties else name)
    lines: list[str] = []
    for part in parts:
        if lines and len(lines[-1]) + len(part) + 3 <= width:
            lines[-1] += f" · {part}"
        else:
            lines.append(part)
    return lines


@dataclass
class DebugState:
    """full 档跨轮记住已经打过什么：整段上下文每轮重打一遍就没法看了。

    渲染器每轮新建（正文的 Renderer 也是），所以这份状态由前端持有、传进来。
    """

    messages: int = 0  # 已经打过的消息条数
    tools: str = ""  # 已经打过的工具清单签名


class DebugRenderer:
    """把 debug 行写到 stderr，正文交给原来的 Renderer。

    工具相关的两类事件由这里接管——debug 行已经包含普通行的全部信息，
    两边都打就重复了。思考内容和正文仍走 Renderer。
    """

    def __init__(
        self,
        inner: Renderer,
        err: TextIO,
        color: bool,
        level: str = "on",
        body_chars: int = BODY_CHARS,
        state: DebugState | None = None,
    ):
        self.inner = inner
        self.err = err
        self.color = color
        self.level = level
        self.verbose = level in ("verbose", "full")
        self.full = level == "full"
        self.body_chars = body_chars
        self.state = state or DebugState()

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
            # 统计行在 ApiResponse 里已经打过；full 档在这里补一段响应结构
            self.inner.end()
            if self.full:
                self.response_body(event)
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
        # full 档每条消息自带字符数，再打一遍摘要就重复了
        if event.outline and not self.full:
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
        if self.full:
            self.request_body(event)

    def request_body(self, event: ApiRequest) -> None:
        """full 档：实际发出去的消息和工具清单。"""
        payload = event.payload or {}
        messages = payload.get("messages") or []
        tools = payload.get("tools") or []
        # 历史是往后追加的，前面那些上一次已经打过了；条数变少（/clear、换会话）就重来
        printed = self.state.messages
        start = printed if 0 < printed <= len(messages) else 0
        self.state.messages = len(messages)
        head = f"请求体  {len(messages)} 条消息"
        if start:
            head += f"（前 {start} 条同上次）"
        self._line(f"  ┌ {head}")
        for index, message in enumerate(messages[start:], start + 1):
            for line in format_message(message, index, self.body_chars):
                self._line(f"  │ {line}")
        if not tools:
            self.state.tools = ""
            self._line("  └ tools  （无）")
            return
        lines = format_tools(tools)
        signature = " ".join(lines)
        # 工具清单基本不变，每轮重打一遍纯属噪音；变了才重新列一次
        if signature == self.state.tools:
            self._line(f"  └ tools  {len(tools)} 个（同上次）")
            return
        self.state.tools = signature
        self._line(f"  └ tools  {lines[0]}  共 {len(tools)} 个")
        for line in lines[1:]:
            self._line(f"           {line}")

    def api_response(self, event: ApiResponse) -> None:
        # 正文是流式写到 stdout 的，先收尾再写 stderr，两个流合起来看才不会挤在一行
        self.inner.end()
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

    def response_body(self, event: MessageDone) -> None:
        """full 档：模型这次到底返回了什么（思考 / 正文 / 工具调用）。"""
        message = event.message or {}
        head = f"响应  {message.get('role', 'assistant')}"
        if event.finish_reason:
            head += f" · finish_reason={event.finish_reason}"
        self._line(f"  ┌ {head}")
        if reasoning := message.get(REASONING_KEY):
            self._line(f"  │ 思考  {len(reasoning):,} 字")
            for line in preview_text(reasoning, self.body_chars):
                self._line(f"  │     {line}")
        content = message.get("content")
        if content:
            self._line(f"  │ 正文  {len(content):,} 字")
            for line in preview_text(content, self.body_chars):
                self._line(f"  │     {line}")
        else:
            self._line(f"  │ 正文  （{'content=null' if content is None else '空'}）")
        calls = message.get("tool_calls") or []
        if calls:
            self._line(f"  │ 工具调用 {len(calls)}")
            for call in calls:
                for line in format_tool_call(call, self.body_chars):
                    self._line(f"  │     {line}")
        usage = event.usage
        if usage is None:
            self._line("  └ usage  （接口没返回）")
            return
        prompt = f"in {usage.prompt_tokens:,}"
        if usage.cached_tokens:
            prompt += f"(缓存 {usage.cached_tokens:,})"
        completion = f"out {usage.completion_tokens:,}"
        if usage.reasoning_tokens:
            completion += f"(思考 {usage.reasoning_tokens:,})"
        self._line(f"  └ usage  {prompt} · {completion}")

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
        # 这里仍然只预览几行：full 档下一轮的请求体会把完整内容作为 tool 消息再打一遍
        for line in lines[:PREVIEW_LINES]:
            self._line(f"        {shorten(line)}", style)
        if len(lines) > PREVIEW_LINES:
            self._line(f"        …（共 {len(lines)} 行）")
