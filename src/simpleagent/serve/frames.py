"""事件 → 帧 的序列化，以及服务端自己发出的补充帧。

核心 loop 产出的 Event（TextDelta / MessageDone / ToolCallStart / ToolResult / ...）在这里
转成总线上的 Frame；另外服务端在「会话状态变化」「出错」「等待审批」「验证完成」时也会发帧，
这些帧由本模块构造（type 分别为 status / error / approval_request / verification）。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from simpleagent.events import (
    ApiRequest,
    ApiResponse,
    ContextEdited,
    Event,
    MaxStepsReached,
    MessageDone,
    ReasoningDelta,
    TextDelta,
    ToolCallStart,
    ToolResult,
)
from simpleagent.serve.bus import Frame


def event_to_frame(event: Event, session_id: str) -> Frame:
    """把核心事件映射成总线帧。类型名见 docs/design/client-ui.md 6.1。"""
    if isinstance(event, TextDelta):
        return Frame(session_id, "text_delta", {"text": event.text})
    if isinstance(event, ReasoningDelta):
        return Frame(session_id, "reasoning_delta", {"text": event.text})
    if isinstance(event, ApiRequest):
        return Frame(
            session_id,
            "api_request",
            {
                "step": event.step,
                "url": event.url,
                "model": event.model,
                "messages": event.messages,
                "tools": event.tools,
                "payload_bytes": event.payload_bytes,
                "options": event.options,
            },
        )
    if isinstance(event, ApiResponse):
        response_payload: dict[str, Any] = {
            "step": event.step,
            "status": event.status,
            "elapsed": round(event.elapsed, 3),
            "ttft": None if event.ttft is None else round(event.ttft, 3),
            "status_code": event.status_code,
            "error": event.error,
            "finish_reason": event.finish_reason,
        }
        if event.usage is not None:
            response_payload["usage"] = {
                "prompt_tokens": event.usage.prompt_tokens,
                "completion_tokens": event.usage.completion_tokens,
                "cached_tokens": event.usage.cached_tokens,
                "reasoning_tokens": event.usage.reasoning_tokens,
            }
        return Frame(session_id, "api_response", response_payload)
    if isinstance(event, MessageDone):
        payload: dict[str, Any] = {
            "message": event.message,
            "finish_reason": event.finish_reason,
        }
        if event.usage is not None:
            payload["usage"] = {
                "prompt_tokens": event.usage.prompt_tokens,
                "completion_tokens": event.usage.completion_tokens,
                "cached_tokens": event.usage.cached_tokens,
                "reasoning_tokens": event.usage.reasoning_tokens,
            }
        return Frame(session_id, "message_done", payload)
    if isinstance(event, ToolCallStart):
        return Frame(
            session_id,
            "tool_call_start",
            {
                "call_id": event.call_id,
                "name": event.name,
                "arguments": event.arguments,
                "step": event.step,
                "permission": event.permission,
                "readonly": event.readonly,
            },
        )
    if isinstance(event, ToolResult):
        return Frame(
            session_id,
            "tool_result",
            {
                "call_id": event.call_id,
                "name": event.name,
                "content": event.content,
                "is_error": event.is_error,
                "duration_ms": round(event.duration_ms, 1),
                "decision": event.decision,
                "truncated": event.truncated,
            },
        )
    if isinstance(event, ContextEdited):
        return Frame(
            session_id,
            "context_edited",
            {
                "kind": event.kind,
                "tokens_before": event.tokens_before,
                "tokens_after": event.tokens_after,
                "limit": event.limit,
                "count": event.count,
                "usage": None if event.usage is None else asdict(event.usage),
                "error": event.error,
                "summary": event.summary(),
            },
        )
    if isinstance(event, MaxStepsReached):
        return Frame(session_id, "max_steps", {"max_steps": event.max_steps})
    return Frame(session_id, "unknown", {"repr": repr(event)})


def status_frame(session_id: str, status: str, extra: dict[str, Any] | None = None) -> Frame:
    payload: dict[str, Any] = {"status": status}
    if extra:
        payload.update(extra)
    return Frame(session_id, "status", payload)


def error_frame(session_id: str, message: str) -> Frame:
    return Frame(session_id, "error", {"message": message})


def approval_request_frame(
    session_id: str, approval_id: str, tool_name: str, arguments: str, reason: str = ""
) -> Frame:
    # reason 是 Policy / 工具给的「为什么要问」：审批卡上要显示，刷新后补发的卡（details()）也带着
    return Frame(
        session_id,
        "approval_request",
        {
            "approval_id": approval_id,
            "tool_name": tool_name,
            "arguments": arguments,
            "reason": reason,
        },
    )


def verification_frame(session_id: str, verification: dict[str, Any]) -> Frame:
    return Frame(session_id, "verification", verification)
