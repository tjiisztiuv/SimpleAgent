"""上下文管理：预算，以及快满时怎么腾地方。

三级策略：写入时截断（M2 已有，ToolRegistry.trim）→ 清理旧工具结果 → 摘要压缩（本文件后半）。

预算是一切的前提：下一次请求大概会发多少 token。算法是「上次的实际用量 + 之后的估算」：

- 上次请求返回的 usage.prompt_tokens 精确等于那次发出去的 system + 工具 + messages[:n]；
- 那之后新增的消息（assistant 回复、工具结果、新的 user 消息）按字符估。

估算误差只出在上次请求之后新增的那一小段里，每请求一次就校准一次。

没用 tiktoken：多一个依赖，而且各家 tokenizer 不一样，它只对 OpenAI 的模型准。
也不能只看 usage：刚回来一个很大的工具结果、下一次请求还没发出去，恰恰是最危险的时候，
而这时 usage 还不知道它。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from simpleagent.config import Profile
from simpleagent.llm.client import prepare_messages

MESSAGE_OVERHEAD = 4  # 每条消息的 role、分隔符之类的固定开销
DEFAULT_OUTPUT_RESERVE = 16_000  # 没配 max_tokens 时给输出留的余量（再不超过窗口的 1/4）

CLEARED_MARK = "[已清理]"  # 占位符的开头，也用来认出已经清过的结果
CLEARED_FILE_RE = re.compile(r"cleared-[0-9a-f]{12}\.txt")  # 清理时落盘的原文文件名
CLEAR_MIN_CHARS = 300  # 更短的工具结果不值得清：换成占位符省不下什么，信息反而丢了
CLEAR_MIN_GAIN = 0.05  # 能省下的不到输入上限的这个比例，就不为它打破前缀缓存

KEEP_RECENT = 0.25  # 摘要压缩时保留原文的量：约输入上限的 1/4
SUMMARY_MAX_CHARS = 3000  # 摘要字数上限；窗口小时再按输入上限的 1/10 收紧
SUMMARY_MARK = "[对话摘要]"  # 摘要消息的开头
COMPACT_ACK = "好的，我了解了之前的进展。"  # 保留部分以 user 开头时插在摘要后面，保持一问一答交替


def estimate_text(text: str) -> int:
    """按字符估 token：ASCII 约 3 个字符一个，中文等非 ASCII 一个字一个。宁可高估。

    英文正文大约 4 个字符一个 token，代码和 JSON 更碎一些；中文在各家 tokenizer 里
    大多是 0.6～1 个 token 一个字。都往大了取：高估只是早一点压缩，低估会超出窗口。
    """
    ascii_chars = len(text.encode("ascii", "ignore"))  # 比逐字符判断快得多
    return -(-ascii_chars // 3) + (len(text) - ascii_chars)


def estimate_message(message: dict[str, Any]) -> int:
    """一条消息：正文、思考内容、tool_call_id 按文本估，tool_calls 和多模态内容按 JSON 估。"""
    tokens = MESSAGE_OVERHEAD
    for key, value in message.items():
        if key == "role" or value is None:
            continue
        if isinstance(value, str):
            tokens += estimate_text(value)
        else:
            tokens += estimate_text(json.dumps(value, ensure_ascii=False))
    return tokens


def estimate_messages(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_message(message) for message in messages)


def estimate_tools(tools: list[dict[str, Any]] | None) -> int:
    """工具列表按 schema 的 JSON 估；每次请求都带着它，工具一多就是一大块。"""
    return estimate_text(json.dumps(tools, ensure_ascii=False)) if tools else 0


def output_reserve(profile: Profile) -> int:
    """给模型输出留的余量：输入占满窗口的话，模型就没地方回答了。"""
    return profile.max_tokens or min(DEFAULT_OUTPUT_RESERVE, profile.context_window // 4)


CALIBRATION_RANGE = (0.5, 2.0)  # 校准系数的上下限，防止个别怪请求把估算带歪


@dataclass(frozen=True)
class ContextAnchor:
    """上次请求的实际用量：prompt_tokens 精确覆盖了 system + 工具 + messages[:messages]。

    记下模型名：换了模型就换了 tokenizer，这个数不能再拿来用。
    """

    prompt_tokens: int
    messages: int
    model: str
    estimate: int  # 同一段请求发出前按字符估的值，和 prompt_tokens 一比就是估算规则的误差


@dataclass(frozen=True)
class Calibration:
    """实际用量 ÷ 字符估算，来自上次请求。

    清理、压缩会让上次的实际用量作废（它覆盖的历史变了），这时只能全量按字符估，
    在 DeepSeek 上高估约 25%：清理完紧接着判断要不要压缩，用的正是这个偏高的数，
    可能白压一次。系数不随历史改动作废，拿它把全量估算修正回实际的量级。
    """

    model: str
    factor: float

    @classmethod
    def of(cls, anchor: ContextAnchor) -> Calibration | None:
        if anchor.estimate <= 0:
            return None
        low, high = CALIBRATION_RANGE
        return cls(anchor.model, min(max(anchor.prompt_tokens / anchor.estimate, low), high))


@dataclass(frozen=True)
class ContextUsage:
    tokens: int  # 下一次请求预计的输入 token
    window: int  # profile.context_window
    reserve: int  # 给输出留的余量
    exact: int = 0  # tokens 里来自上次实际用量的部分；0 表示全部是估算
    exact_estimate: int = 0  # 同一段按字符估出来是多少，和 exact 一比就是估算规则的误差
    factor: float | None = None  # 没有实际用量可用时，全量估算乘上的校准系数

    @property
    def limit(self) -> int:
        """输入最多能用多少。"""
        return max(self.window - self.reserve, 1)

    @property
    def ratio(self) -> float:
        return self.tokens / self.limit


def measure(
    messages: list[dict[str, Any]],
    *,
    system_prompt: str,
    tools: list[dict[str, Any]] | None,
    profile: Profile,
    anchor: ContextAnchor | None = None,
    calibration: Calibration | None = None,
) -> ContextUsage:
    """估算把这些消息发出去要占多少上下文。

    按实际发出去的样子估：历史里的思考内容会按 reasoning_echo 去掉或保留。
    有上次的实际用量就用它打底；没有的话全量按字符估，有同一个模型的校准系数就乘上。
    """
    sent = prepare_messages(messages, profile.quirks)
    reserve = output_reserve(profile)
    if anchor is not None and anchor.model == profile.model and anchor.messages <= len(sent):
        return ContextUsage(
            anchor.prompt_tokens + estimate_messages(sent[anchor.messages :]),
            profile.context_window,
            reserve,
            exact=anchor.prompt_tokens,
            exact_estimate=anchor.estimate,
        )
    tokens = estimate_text(system_prompt) + estimate_tools(tools) + estimate_messages(sent)
    if calibration is not None and calibration.model == profile.model:
        return ContextUsage(
            round(tokens * calibration.factor),
            profile.context_window,
            reserve,
            factor=calibration.factor,
        )
    return ContextUsage(tokens, profile.context_window, reserve)


def breakdown(
    messages: list[dict[str, Any]],
    *,
    system_prompt: str,
    tools: list[dict[str, Any]] | None,
    profile: Profile,
) -> dict[str, int]:
    """上下文由哪些部分构成（全部按字符估）：system / tools / user / assistant / tool。"""
    parts = {
        "system": estimate_text(system_prompt),
        "tools": estimate_tools(tools),
        "user": 0,
        "assistant": 0,
        "tool": 0,
    }
    for message in prepare_messages(messages, profile.quirks):
        role = str(message.get("role", "?"))
        parts[role] = parts.get(role, 0) + estimate_message(message)
    return parts


# ------------------------------------------------------------------ 第二级：清理旧工具结果
#
# 工具结果是上下文里最占地方的，而模型看过、据此做了下一步之后，原文基本不会再用到。
# 清理只动 tool 消息的 content：消息和 tool_call_id 都在，配对不断；调用了什么、参数是什么
# 也都在 assistant 消息里，模型知道自己做过什么。原文落盘，要用时 read_file 读回来——
# bash、MCP 工具重跑一遍可能有副作用，或者结果已经变了。


def select_clearable(messages: list[dict[str, Any]], keep: int) -> list[tuple[int, str]]:
    """能清理的工具结果：(下标, 工具名)，按出现顺序。

    跳过：最近 keep 个；模型还没看过的（最后一条 assistant 之后的，比如一次并行 5 个调用，
    只按「最近 keep 个」算的话，前两个模型一眼没看就没了）；已经清过的；太短的；
    以及把清理文件读回来的结果——模型主动读回，说明它确实要用，再清就会来回拉锯
    （实测：读回 → 又被清 → 再读回，一轮里转了 8 圈），这种情况留给摘要压缩。
    """
    last_assistant = max(
        (i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=-1
    )
    # tool_call_id → (工具名, 参数)；id 可能跨轮重复，用离得最近的那次调用
    calls: dict[str, tuple[str, str]] = {}
    results: list[tuple[int, str]] = []
    for i, message in enumerate(messages):
        role = message.get("role")
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                calls[call.get("id") or ""] = (
                    function.get("name") or "?",
                    str(function.get("arguments") or ""),
                )
        elif role == "tool":
            name, arguments = calls.get(message.get("tool_call_id") or "", ("工具", ""))
            if not CLEARED_FILE_RE.search(arguments):
                results.append((i, name))
    candidates = results[:-keep] if keep > 0 else results
    picked = []
    for i, name in candidates:
        content = messages[i].get("content")
        if i > last_assistant or not isinstance(content, str):
            continue
        if content.startswith(CLEARED_MARK) or len(content) < CLEAR_MIN_CHARS:
            continue
        picked.append((i, name))
    return picked


def cleared_result(message: dict[str, Any], name: str, saved: Path | None) -> dict[str, Any]:
    """同一条 tool 消息，content 换成占位符；tool_call_id 等字段原样保留。"""
    content = str(message.get("content") or "")
    text = f"{CLEARED_MARK} 这个 {name} 结果较早，为节省上下文已移出（原 {len(content):,} 字符）。"
    if saved is not None:
        text += f"完整内容存在 {saved}，需要时用 read_file 读取。"
    else:
        text += "需要的话重新调用这个工具。"
    return {**message, "content": text}


def save_cleared(output_dir: Path | None, content: str) -> Path | None:
    """原文存到 <output_dir>/cleared-<摘要>.txt，返回路径；没有目录或写失败返回 None。

    文件名只由内容决定、不带时间戳：同样的内容每次得到同一个路径，占位符一字不差，
    前缀缓存才稳定（工作台每轮都会重新清一遍，靠的就是这个）。
    """
    if output_dir is None:
        return None
    digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:12]
    path = output_dir / f"cleared-{digest}.txt"
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        if path.exists():
            os.utime(path)  # 还在用：刷新修改时间，免得被 tool_outputs/ 的 7 天清理删掉
        else:
            path.write_text(content, encoding="utf-8")
    except OSError:
        return None
    return path


# ------------------------------------------------------------------ 第三级：摘要压缩
#
# 清理管不到的时候（上下文主要是对话正文、模型把清理文件读了回来、一轮里干了几十步），
# 只能请模型把早期对话写成摘要，替换原文，最近的部分保留原样。它要多调一次 LLM，
# 之后的整个前缀缓存也全部失效（摘要是新内容），所以排在清理之后、阈值更高。


def _head_len(messages: list[dict[str, Any]]) -> int:
    """历史开头上一次压缩留下的摘要占几条（摘要 + 可能有的那条回复）；没压缩过是 0。"""
    if not messages or not str(messages[0].get("content") or "").startswith(SUMMARY_MARK):
        return 0
    if len(messages) > 1 and messages[1] == {"role": "assistant", "content": COMPACT_ACK}:
        return 2
    return 1


def find_cut(messages: list[dict[str, Any]], keep_tokens: int) -> int | None:
    """切点：messages[:切点] 压成摘要，messages[切点:] 保留原文，大约 keep_tokens。

    切点只落在两种位置：user 消息（轮次边界），或紧跟工具结果的 assistant 消息（一轮内部的
    步骤边界，daemon 一轮几十步也能压）。永远不落在 tool 上，所以 tool_calls 和它的结果要么
    一起压掉、要么一起留下；前一条也不能是 user，后面追加压缩指令时才不会两条 user 挨着。
    最近一步本身就超过 keep_tokens 时，退到最后一个边界，至少把更早的压掉。
    只有上一次的摘要可压时返回 None：再压一遍摘要没有意义。
    """
    start = _head_len(messages) + 1
    boundaries = [
        i
        for i in range(start, len(messages))
        if messages[i].get("role") in ("user", "assistant")
        and messages[i - 1].get("role") != "user"
    ]
    if not boundaries:
        return None
    allowed = set(boundaries)
    kept = 0
    fitting = None
    for i in range(len(messages) - 1, boundaries[0] - 1, -1):
        kept += estimate_message(messages[i])
        if kept > keep_tokens:
            break
        if i in allowed:
            fitting = i
    return fitting if fitting is not None else boundaries[-1]


def last_turn_cut(messages: list[dict[str, Any]]) -> int | None:
    """手动 /compact 的切点：最后一条 user 消息，完整保留最后一轮问答。

    find_cut(messages, 0) 只留最后一步——这一轮里调过工具的话，只剩最后那条回答，
    问题本身被压进了摘要。最后一条 user 不是合法切点（比如整段只有一轮）就退回去用它。
    """
    start = _head_len(messages) + 1
    for i in range(len(messages) - 1, start - 1, -1):
        if messages[i].get("role") == "user":
            if messages[i - 1].get("role") != "user":
                return i
            break
    return find_cut(messages, 0)


def compact_prompt(summary_chars: int, instructions: str | None = None) -> str:
    """追加在历史后面的压缩指令。instructions 是用户手动 /compact 时交代的重点。"""
    text = (
        "上下文快满了。请把上面到目前为止的对话压缩成一份摘要：之后会用它替换上面这些消息，"
        "在此基础上继续工作。\n\n要求：\n"
        "- 用户的目标，以及所有明确提出的要求、约束和偏好，尽量保留原话\n"
        "- 已经完成了什么、得出了哪些结论、做过哪些决定以及原因\n"
        "- 涉及的关键文件路径、函数名、命令、数据和报错原文\n"
        "- 还没做完的事和下一步打算\n"
        "- 工具结果只保留结论和之后还会用到的细节\n"
        f"- 全文不超过 {summary_chars} 字，代码、路径、命令也算在内\n"
        "- 上面如果已有之前的摘要，和新内容合并成一份，合并后整体仍不超过这个长度："
        "越早的内容越概括，只留仍然有用的结论和还没做完的事\n\n"
        "不要调用工具，也不要继续原来的任务，直接输出摘要正文。"
    )
    if instructions:
        text += f"\n\n用户特别交代要保留的重点：{instructions}"
    return text


def compacted_head(summary: str, count: int, next_role: str) -> list[dict[str, Any]]:
    """替换掉前 count 条消息的摘要。

    用 user 消息而不是塞进 system：改 system prompt 连工具定义在内的整个前缀都会失效，
    也违背「system prompt 保持稳定」。保留部分以 user 开头时再加一条固定的 assistant 回复，
    照顾要求 user / assistant 严格交替的服务。
    """
    head: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": f"{SUMMARY_MARK} 以下是之前 {count} 条消息的摘要"
            f"（系统自动生成，原消息已移出上下文）：\n\n{summary}",
        }
    ]
    if next_role == "user":
        head.append({"role": "assistant", "content": COMPACT_ACK})
    return head


# ------------------------------------------------------------------ 兜底：服务端说上下文超长
#
# 预算是估出来的：tokenizer 差得远、服务端悄悄加了 token、context_window 配大了、压缩关了，
# 都可能估低。这时服务端直接回 400，不兜底的话这一轮报错结束，下一轮照样超长，会话就卡死了。
# 注意 Ollama 超过 num_ctx 是悄悄截断而不是报错，本地 profile 只能靠预算。

# 各家说法不一，只能按关键字认。宁少勿泛：「exceeds」之类会把 max_tokens 参数错误也认进来。
# 认错的代价是白压缩一次再照样报错；碰到新说法再补
OVERFLOW_PATTERNS = (
    "context_length_exceeded",  # OpenAI 的 error code
    "maximum context length",  # OpenAI / DeepSeek
    "context window",
    "range of input length",  # 通义千问
    "prompt is too long",
    "input is too long",
    "too many tokens",
    "超长",
    "超过最大",
)


def is_context_overflow(error: BaseException) -> bool:
    """请求是不是因为上下文超长被拒：状态码 400 / 413，且报错里带超长的说法。

    按鸭子类型取 status_code / code / message：loop 不用 import openai，FakeLLM 也能模拟。
    """
    if getattr(error, "status_code", None) not in (400, 413):
        return False
    text = " ".join(
        str(part) for part in (getattr(error, "code", None), getattr(error, "message", None), error)
    ).lower()
    return any(pattern in text for pattern in OVERFLOW_PATTERNS)
