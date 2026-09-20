"""OpenAI 兼容协议（Chat Completions）的流式客户端。

职责：
- 按 profile 组装请求体（stream_options / extra_body / 思考内容回传策略）
- 把 SSE chunk 拼成完整的 assistant 消息，同时产出增量事件
- 把每次请求/响应（包括失败和中断）写进 trace
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

import httpx2
from openai import AsyncOpenAI, DefaultAsyncHttpxClient

from simpleagent.config import Profile, Quirks
from simpleagent.events import (
    REASONING_KEY,
    ApiRequest,
    ApiResponse,
    Event,
    MessageDone,
    ReasoningDelta,
    TextDelta,
    Usage,
    message_outline,
)
from simpleagent.trace import Tracer


def collect_tool_names(tools: list[dict[str, Any]]) -> list[str]:
    """工具名清单；debug 的 full 档用它显示这一轮带了哪些工具。"""
    return [(tool.get("function") or {}).get("name") or "?" for tool in tools]


class LLM(Protocol):
    name: str
    profile: Profile

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        step: int = 0,  # 本轮对话的第几次请求，只在 debug 输出里用来标号
    ) -> AsyncIterator[Event]: ...

    async def close(self) -> None: ...


class StreamAccumulator:
    """把流式 chunk（dict 形式）拼成完整的 assistant 消息。"""

    def __init__(self, reasoning_field: str | None = REASONING_KEY):
        self.reasoning_field = reasoning_field
        self.text: list[str] = []
        self.reasoning: list[str] = []
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.finish_reason: str | None = None
        self.usage: dict[str, Any] | None = None

    def feed(self, chunk: dict[str, Any]) -> list[Event]:
        events: list[Event] = []
        # 开启 include_usage 后，usage 通常在最后一个 choices 为空的 chunk 里
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or {}
            if self.reasoning_field and (reasoning := delta.get(self.reasoning_field)):
                self.reasoning.append(reasoning)
                events.append(ReasoningDelta(reasoning))
            if content := delta.get("content"):
                self.text.append(content)
                events.append(TextDelta(content))
            for tool_call in delta.get("tool_calls") or []:
                self._merge_tool_call(tool_call)
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
        return events

    def _merge_tool_call(self, delta: dict[str, Any]) -> None:
        # 同一个 tool_call 的多个分片靠 index 关联；个别实现不带 index，视为新的调用
        index = delta.get("index")
        if index is None:
            index = len(self.tool_calls)
        slot = self.tool_calls.setdefault(
            index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
        )
        if delta.get("id"):
            slot["id"] = delta["id"]
        function = delta.get("function") or {}
        # name 只取第一次出现的（有的实现每个分片都重复带 name）；arguments 是真正分片拼接的
        if function.get("name") and not slot["function"]["name"]:
            slot["function"]["name"] = function["name"]
        if function.get("arguments"):
            slot["function"]["arguments"] += function["arguments"]

    def message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": "".join(self.text)}
        if self.reasoning:
            message[REASONING_KEY] = "".join(self.reasoning)
        if self.tool_calls:
            message["tool_calls"] = [self.tool_calls[i] for i in sorted(self.tool_calls)]
            message["content"] = message["content"] or None
        return message


def prepare_messages(messages: list[dict[str, Any]], quirks: Quirks) -> list[dict[str, Any]]:
    """按 reasoning_echo 策略处理历史里的思考内容，返回新的列表（不修改会话历史）。"""
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    prepared = []
    for i, message in enumerate(messages):
        if REASONING_KEY not in message:
            prepared.append(message)
            continue
        message = dict(message)
        reasoning = message.pop(REASONING_KEY)
        echo = quirks.reasoning_echo == "all" or (
            quirks.reasoning_echo == "current_turn" and i > last_user
        )
        if echo and quirks.reasoning_field:
            message[quirks.reasoning_field] = reasoning
        prepared.append(message)
    return prepared


def is_loopback(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class LLMClient:
    def __init__(
        self,
        name: str,
        profile: Profile,
        tracer: Tracer | None = None,
        http_client: httpx2.AsyncClient | None = None,
    ):
        self.name = name
        self.profile = profile
        self.tracer = tracer
        if http_client is None and is_loopback(profile.base_url):
            # 本地服务（如 Ollama）不读 http_proxy 等环境变量，否则本机代理会把请求转走并返回 502
            http_client = DefaultAsyncHttpxClient(trust_env=False)
        self._client = AsyncOpenAI(
            base_url=profile.base_url,
            api_key=profile.api_key(),
            timeout=profile.timeout,
            max_retries=profile.max_retries,
            http_client=http_client,
        )

    def endpoint(self) -> str:
        """实际的请求地址，只用于 debug 显示（不含 key、不含请求体）。"""
        return urljoin(self.profile.base_url.rstrip("/") + "/", "chat/completions")

    def build_request(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        profile, quirks = self.profile, self.profile.quirks
        request: dict[str, Any] = {
            "model": profile.model,
            "messages": prepare_messages(messages, quirks),
            "stream": True,
        }
        if quirks.stream_usage:
            request["stream_options"] = {"include_usage": True}
        if profile.max_tokens is not None:
            request["max_tokens"] = profile.max_tokens
        if profile.temperature is not None:
            request["temperature"] = profile.temperature
        if tools:
            request["tools"] = tools
            if not quirks.parallel_tool_calls:
                request["parallel_tool_calls"] = False
        return request

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        step: int = 0,
    ) -> AsyncIterator[Event]:
        request = self.build_request(messages, tools)
        accumulator = StreamAccumulator(self.profile.quirks.reasoning_field)
        raw_chunks: list[dict[str, Any]] | None = (
            [] if self.tracer and self.tracer.raw_chunks else None
        )
        started_at = datetime.now().isoformat(timespec="seconds")
        start = time.monotonic()
        ttft: float | None = None
        yield ApiRequest(
            step,
            self.endpoint(),
            self.profile.model,
            len(request["messages"]),
            len(tools or []),
            len(json.dumps(request, ensure_ascii=False).encode("utf-8")),
            message_outline(request["messages"]),
            self.request_options(),
            sent=request["messages"],
            tool_names=collect_tool_names(tools or []),
        )

        try:
            stream = await self._client.chat.completions.create(
                **request, extra_body=self.profile.extra_body or None
            )
            try:
                async for chunk in stream:
                    data = chunk.model_dump(exclude_none=True)
                    if raw_chunks is not None:
                        raw_chunks.append(data)
                    for event in accumulator.feed(data):
                        if ttft is None:
                            ttft = time.monotonic() - start
                        yield event
            finally:
                await stream.close()
        except BaseException as e:
            # 失败和中断也要留痕：Ctrl+C（CancelledError）或消费方提前退出（GeneratorExit）
            status = (
                "cancelled"
                if isinstance(e, asyncio.CancelledError | GeneratorExit | KeyboardInterrupt)
                else "error"
            )
            self._trace(request, accumulator, raw_chunks, started_at, start, ttft, status, e)
            # GeneratorExit（消费方提前退出、aclose）期间不能再 yield：async generator
            # 关闭时 yield 会被判成 "ignored GeneratorExit"，把错误甩给调用方。
            # CancelledError 时 yield 是安全的，事件能送到还在迭代的消费方；
            # 至于它会不会被送到，取决于消费方有没有一起被取消，这里不保证。
            if not isinstance(e, GeneratorExit):
                try:
                    yield self._response(step, status, start, ttft, error=e)
                except BaseException:  # noqa: BLE001
                    pass
            raise

        self._trace(request, accumulator, raw_chunks, started_at, start, ttft, "ok")
        yield self._response(step, "ok", start, ttft, accumulator)
        yield MessageDone(
            message=accumulator.message(),
            finish_reason=accumulator.finish_reason,
            usage=Usage.from_dict(accumulator.usage) if accumulator.usage else None,
            ttft=ttft,
            elapsed=time.monotonic() - start,
        )

    def request_options(self) -> dict[str, Any]:
        """影响请求体的非默认开关；debug 时打出来，排查各家差异最快。

        extra_body 只放键名：值可能是厂商私有参数，也可能夹带敏感内容。
        """
        quirks = self.profile.quirks
        options: dict[str, Any] = {
            "stream_usage": quirks.stream_usage,
            "parallel_tool_calls": quirks.parallel_tool_calls,
        }
        if self.profile.extra_body:
            options["extra_body"] = sorted(self.profile.extra_body)
        return options

    def _response(
        self,
        step: int,
        status: str,
        start: float,
        ttft: float | None,
        accumulator: StreamAccumulator | None = None,
        error: BaseException | None = None,
    ) -> ApiResponse:
        return ApiResponse(
            step,
            status,
            time.monotonic() - start,
            ttft,
            status_code=None if error is None else getattr(error, "status_code", None),
            error=None if error is None else f"{type(error).__name__}: {error}",
            usage=(
                Usage.from_dict(accumulator.usage)
                if accumulator is not None and accumulator.usage
                else None
            ),
            finish_reason=None if accumulator is None else accumulator.finish_reason,
        )

    def _trace(
        self,
        request: dict[str, Any],
        accumulator: StreamAccumulator,
        raw_chunks: list[dict[str, Any]] | None,
        started_at: str,
        start: float,
        ttft: float | None,
        status: str,
        error: BaseException | None = None,
    ) -> None:
        if self.tracer is None:
            return
        record: dict[str, Any] = {
            "status": status,
            "profile": self.name,
            "base_url": self.profile.base_url,
            "started_at": started_at,
            "elapsed_s": round(time.monotonic() - start, 3),
            "ttft_s": None if ttft is None else round(ttft, 3),
            # 与实际发送的请求体一致：extra_body 会被 SDK 合并到顶层
            "request": {**request, **self.profile.extra_body},
            "response": {
                "message": accumulator.message(),
                "finish_reason": accumulator.finish_reason,
                "usage": accumulator.usage,
            },
        }
        if error is not None:
            record["error"] = f"{type(error).__name__}: {error}"
        if raw_chunks is not None:
            record["raw_chunks"] = raw_chunks
        self.tracer.record(record)

    async def close(self) -> None:
        await self._client.close()
