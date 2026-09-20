"""事件类型：LLM 客户端和 agent loop 产出的事件流。

前端（REPL / headless / daemon）只消费事件，不关心事件从哪来。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TextDelta:
    text: str


@dataclass
class ReasoningDelta:
    text: str


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0  # 命中前缀缓存的输入 token
    reasoning_tokens: int = 0  # 输出中用于思考的 token

    @classmethod
    def from_dict(cls, usage: dict[str, Any]) -> Usage:
        prompt_details = usage.get("prompt_tokens_details") or {}
        completion_details = usage.get("completion_tokens_details") or {}
        return cls(
            prompt_tokens=usage.get("prompt_tokens") or 0,
            completion_tokens=usage.get("completion_tokens") or 0,
            # OpenAI 标准字段 / DeepSeek 私有字段
            cached_tokens=prompt_details.get("cached_tokens")
            or usage.get("prompt_cache_hit_tokens")
            or 0,
            reasoning_tokens=completion_details.get("reasoning_tokens") or 0,
        )

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            self.cached_tokens + other.cached_tokens,
            self.reasoning_tokens + other.reasoning_tokens,
        )


@dataclass
class ApiRequest:
    """一次 LLM 请求发出前：摘要 + 实际请求体。不含 header、不含 key。

    messages / tools / payload_bytes 用来观察「这次到底发了多少东西」，
    排查上下文膨胀和工具列表变化时最直观；正文在 payload 里，只有 full 档会打出来。
    """

    step: int  # 本轮对话的第几次请求，从 1 开始
    url: str
    model: str
    messages: int  # 请求体里的消息条数
    tools: int  # 携带的工具个数
    payload_bytes: int  # 请求体序列化后的字节数
    # 消息清单：role + 字符数，verbose 档用来找「哪一条把上下文撑大了」
    outline: list[dict[str, Any]] = field(default_factory=list)
    # 非默认的协议开关；extra_body 只放键名，值可能是厂商私有参数
    options: dict[str, Any] = field(default_factory=dict)
    # 实际发出的请求体（含 messages / tools，不含 header、不含 key），full 档用来打正文。
    # 挂的是引用，不拷贝也不额外序列化：off 档零成本，中途 /debug full 也立刻有东西看。
    payload: dict[str, Any] | None = None


@dataclass
class ApiResponse:
    """一次 LLM 请求的终态：成功、失败、中断都会产出。

    失败和 Ctrl+C 也发——这两种情况恰恰是最需要看的，而 MessageDone 只在成功时产出。
    """

    step: int
    status: str  # "ok" | "error" | "cancelled"
    elapsed: float  # 秒
    ttft: float | None = None  # 首个 token 延迟（秒）
    status_code: int | None = None  # HTTP 状态码；连不上时为 None
    error: str | None = None  # "APIConnectionError: ..." 之类
    usage: Usage | None = None
    finish_reason: str | None = None


@dataclass
class MessageDone:
    """一次 LLM 调用结束：拼好的 assistant 消息（OpenAI 格式 dict）。"""

    message: dict[str, Any]
    finish_reason: str | None = None
    usage: Usage | None = None
    ttft: float | None = None  # 首个 token 延迟（秒）
    elapsed: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallStart:
    """模型要求调用一个工具，马上执行。"""

    call_id: str
    name: str
    arguments: str  # 模型给的原始 JSON 字符串，怎么显示由前端决定
    step: int = 0  # 属于第几次请求，用来和 API 行对齐
    permission: str = "allow"  # 工具的默认等级：allow / ask / deny
    readonly: bool = True  # False 时这批调用会串行执行


@dataclass
class ToolResult:
    """一次工具调用的结果；content 就是回给模型的文本。"""

    call_id: str
    name: str
    content: str
    is_error: bool = False
    duration_ms: float = 0.0  # 含权限判定的执行耗时
    decision: str | None = None  # 实际判定结果：allow / ask / deny；参数校验失败时为 None
    truncated: bool = False  # 输出过长被截断并落盘

    def as_message(self) -> dict[str, Any]:
        return {"role": "tool", "tool_call_id": self.call_id, "content": self.content}


@dataclass
class MaxStepsReached:
    """一轮对话请求模型的次数达到上限，loop 停止。"""

    max_steps: int


Event = (
    TextDelta
    | ReasoningDelta
    | ApiRequest
    | ApiResponse
    | MessageDone
    | ToolCallStart
    | ToolResult
    | MaxStepsReached
)
