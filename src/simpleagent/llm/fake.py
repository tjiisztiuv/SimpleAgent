"""FakeLLM：按脚本返回响应的假模型，用于不联网的确定性测试。

脚本里每一项对应一次 stream() 调用，可以是：
- str：纯文本回复
- dict：{"content", "reasoning", "tool_calls": [{"id", "name", "arguments"}], "usage", "delay"}
- Exception：调用时抛出（模拟 API 错误）

响应会被切成 chunk，再经过和真实客户端相同的 StreamAccumulator 拼接。
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from simpleagent.config import Profile
from simpleagent.events import (
    ApiRequest,
    ApiResponse,
    Event,
    MessageDone,
    Usage,
)
from simpleagent.llm.client import REASONING_KEY, StreamAccumulator, message_outline

Script = str | dict[str, Any] | Exception


def _pieces(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def script_to_chunks(response: dict[str, Any], chunk_size: int = 4) -> list[dict[str, Any]]:
    def chunk(delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
        choice = {"index": 0, "delta": delta, "finish_reason": finish_reason}
        return {"choices": [choice]}

    chunks = [chunk({REASONING_KEY: p}) for p in _pieces(response.get("reasoning", ""), chunk_size)]
    chunks += [chunk({"content": p}) for p in _pieces(response.get("content", ""), chunk_size)]
    tool_calls = response.get("tool_calls") or []
    for index, call in enumerate(tool_calls):
        arguments = call.get("arguments", {})
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        head = {"index": index, "id": call.get("id", f"call_{index}"), "type": "function"}
        chunks.append(chunk({"tool_calls": [{**head, "function": {"name": call["name"]}}]}))
        for piece in _pieces(arguments, chunk_size):
            delta = {"index": index, "function": {"arguments": piece}}
            chunks.append(chunk({"tool_calls": [delta]}))
    chunks.append(chunk({}, "tool_calls" if tool_calls else "stop"))
    if "usage" in response:
        chunks.append({"choices": [], "usage": response["usage"]})
    return chunks


class FakeLLM:
    def __init__(
        self,
        responses: list[Script],
        name: str = "fake",
        profile: Profile | None = None,
        chunk_size: int = 4,
    ):
        self.name = name
        self.profile = profile or Profile(base_url="http://fake.invalid/v1", model="fake-model")
        self.responses = list(responses)
        self.chunk_size = chunk_size
        self.requests: list[dict[str, Any]] = []  # 每次调用收到的 messages 和 tools
        self.closed = False

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        step: int = 0,
        continue_turn: bool = False,
    ) -> AsyncIterator[Event]:
        # 收到的是 prepare_messages 之前的历史；continue_turn 记下来，测试里自己按 profile 处理
        self.requests.append(
            {
                "messages": copy.deepcopy(messages),
                "tools": copy.deepcopy(tools),
                "step": step,
                "continue_turn": continue_turn,
            }
        )
        # 和真实客户端产出同样的 API 事件：debug 渲染和事件序列的测试才测得到东西
        start = time.monotonic()
        yield ApiRequest(
            step,
            self.profile.base_url.rstrip("/") + "/chat/completions",
            self.profile.model,
            len(messages),
            len(tools or []),
            len(json.dumps(messages, ensure_ascii=False).encode("utf-8")),
            message_outline(messages),
            {},
            {"model": self.profile.model, "messages": messages, "tools": tools or []},
        )
        if not self.responses:
            raise AssertionError("FakeLLM 的脚本已经用完")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            yield ApiResponse(
                step,
                "error",
                time.monotonic() - start,
                error=f"{type(response).__name__}: {response}",
                status_code=getattr(response, "status_code", None),
            )
            raise response
        if isinstance(response, str):
            response = {"content": response}

        accumulator = StreamAccumulator(REASONING_KEY)
        for chunk in script_to_chunks(response, self.chunk_size):
            await asyncio.sleep(response.get("delay", 0))
            for event in accumulator.feed(chunk):
                yield event
        yield ApiResponse(
            step,
            "ok",
            time.monotonic() - start,
            usage=Usage.from_dict(accumulator.usage) if accumulator.usage else None,
            finish_reason=accumulator.finish_reason,
        )
        yield MessageDone(
            message=accumulator.message(),
            finish_reason=accumulator.finish_reason,
            usage=Usage.from_dict(accumulator.usage) if accumulator.usage else None,
        )

    async def close(self) -> None:
        self.closed = True
