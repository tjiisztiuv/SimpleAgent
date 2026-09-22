"""上下文管理（M6）：预算、清理、摘要压缩、超长兜底、手动压缩、校准与缓存（第 1～5 步）。"""

import asyncio
import json
import os
from pathlib import Path

import httpx2
import openai
import pytest
from pydantic import BaseModel

from simpleagent.agent.context import (
    CLEARED_MARK,
    COMPACT_ACK,
    MESSAGE_OVERHEAD,
    SUMMARY_MARK,
    Calibration,
    ContextAnchor,
    ContextUsage,
    breakdown,
    cleared_result,
    compact_prompt,
    compacted_head,
    estimate_message,
    estimate_messages,
    estimate_text,
    estimate_tools,
    find_cut,
    is_context_overflow,
    last_turn_cut,
    measure,
    output_reserve,
    save_cleared,
    select_clearable,
)
from simpleagent.agent.loop import INTERRUPTED_RESULT, Agent, fill_missing_results
from simpleagent.agent.session import Session, SessionStore
from simpleagent.config import ContextConfig, Profile, Quirks
from simpleagent.events import ContextEdited, ToolCallStart, Usage
from simpleagent.llm.client import prepare_messages
from simpleagent.llm.fake import FakeLLM
from simpleagent.tools import ToolContext, ToolRegistry, builtin_tools, tool

PROFILE = Profile(base_url="http://fake.invalid/v1", model="fake-model")


def api_usage(prompt: int, completion: int = 5) -> dict:
    return {"prompt_tokens": prompt, "completion_tokens": completion}


# ------------------------------------------------------------------ 1. 估算规则


def test_estimate_text_ascii_and_cjk():
    assert estimate_text("") == 0
    assert estimate_text("abc") == 1
    assert estimate_text("abcd") == 2  # ASCII 三个字符一个 token，不足三个也算一个
    assert estimate_text("你好世界") == 4  # 非 ASCII 一个字一个
    assert estimate_text("hi，你好") == 1 + 3  # 全角逗号也是非 ASCII


def test_estimate_message_counts_tool_calls_and_skips_role():
    call = {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
    message = {"role": "assistant", "content": None, "tool_calls": [call]}
    # content 为空时 tool_calls 才是大头，不能漏
    assert estimate_message(message) > MESSAGE_OVERHEAD
    assert estimate_message({"role": "user", "content": ""}) == MESSAGE_OVERHEAD
    assert estimate_message({"role": "tool", "tool_call_id": "c1", "content": "abc"}) == (
        MESSAGE_OVERHEAD + estimate_text("c1") + estimate_text("abc")
    )


def test_estimate_tools():
    assert estimate_tools(None) == 0
    assert estimate_tools([]) == 0
    assert estimate_tools(ToolRegistry(builtin_tools()).schemas()) > 500  # 7 个内置工具的 schema


def test_output_reserve():
    assert output_reserve(PROFILE.model_copy(update={"max_tokens": 4096})) == 4096
    assert output_reserve(PROFILE) == 16_000  # 128k 窗口：取默认的 16k
    assert output_reserve(PROFILE.model_copy(update={"context_window": 32_000})) == 8_000  # 1/4


def test_usage_limit_and_ratio():
    usage = ContextUsage(tokens=56_000, window=128_000, reserve=16_000)
    assert usage.limit == 112_000
    assert usage.ratio == 0.5
    assert ContextUsage(tokens=10, window=100, reserve=200).limit == 1  # 配错了也不除以 0


# ------------------------------------------------------------------ 2. measure


MESSAGES = [
    {"role": "user", "content": "列一下目录"},
    {"role": "assistant", "content": "好的，我看看"},
    {"role": "user", "content": "再看看 src"},
]


def test_measure_without_anchor_estimates_everything():
    tools = ToolRegistry(builtin_tools()).schemas()
    usage = measure(MESSAGES, system_prompt="系统提示", tools=tools, profile=PROFILE)
    assert usage.tokens == estimate_text("系统提示") + estimate_tools(tools) + estimate_messages(
        MESSAGES
    )
    assert (usage.exact, usage.exact_estimate) == (0, 0)
    assert (usage.window, usage.reserve) == (128_000, 16_000)


def test_measure_with_anchor_adds_estimate_of_new_messages():
    anchor = ContextAnchor(prompt_tokens=1000, messages=1, model="fake-model", estimate=1200)
    usage = measure(MESSAGES, system_prompt="系统提示", tools=None, profile=PROFILE, anchor=anchor)
    assert usage.tokens == 1000 + estimate_messages(MESSAGES[1:])
    assert usage.exact == 1000
    assert usage.exact_estimate == 1200  # 同一段发出前按字符估的值，和 1000 一比就是误差


def test_measure_ignores_anchor_from_other_model_or_longer_history():
    full = measure(MESSAGES, system_prompt="s", tools=None, profile=PROFILE)
    for anchor in (
        ContextAnchor(1000, 1, "other-model", 1200),  # 换了 tokenizer
        ContextAnchor(1000, 10, "fake-model", 1200),  # 覆盖的消息比现在还多：历史被改过
    ):
        usage = measure(MESSAGES, system_prompt="s", tools=None, profile=PROFILE, anchor=anchor)
        assert usage == full


def test_measure_follows_reasoning_echo():
    """按实际发出去的样子估：不回传的思考内容不算。"""
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a", "reasoning_content": "想" * 1000},
        {"role": "user", "content": "q2"},
    ]

    def tokens(echo: str) -> int:
        profile = PROFILE.model_copy(update={"quirks": Quirks(reasoning_echo=echo)})
        return measure(messages, system_prompt="", tools=None, profile=profile).tokens

    assert tokens("all") - tokens("none") >= 1000
    assert tokens("current_turn") == tokens("none")  # 思考在上一轮，这一轮不回传


def test_breakdown_by_role():
    messages = [
        *MESSAGES,
        {"role": "tool", "tool_call_id": "c1", "content": "a.txt\nb.txt"},
    ]
    parts = breakdown(messages, system_prompt="系统提示", tools=None, profile=PROFILE)
    assert parts["system"] == estimate_text("系统提示")
    assert parts["tools"] == 0
    assert parts["user"] == estimate_messages([MESSAGES[0], MESSAGES[2]])
    assert parts["assistant"] == estimate_message(MESSAGES[1])
    assert parts["tool"] == estimate_message(messages[3])


# ------------------------------------------------------------------ 3. Session 和 Agent


def test_truncate_into_anchored_range_drops_anchor():
    session = Session("s", messages=[dict(m) for m in MESSAGES])
    session.mark_sent(500, 2, "fake-model", 400)
    session.truncate(2)  # 没删到覆盖范围：基准还有效
    assert session.context_anchor == ContextAnchor(500, 2, "fake-model", 400)
    session.truncate(1)
    assert session.context_anchor is None
    assert session.context_calibration == Calibration("fake-model", 1.25)  # 系数不跟着作废


async def test_agent_marks_what_last_request_covered(tmp_path: Path):
    llm = FakeLLM(
        [
            {
                "content": "我看看",
                "tool_calls": [{"id": "c1", "name": "list_dir", "arguments": {}}],
                "usage": api_usage(100),
            },
            {"content": "目录是空的", "usage": api_usage(180)},
        ]
    )
    agent = Agent(llm, ToolRegistry(builtin_tools()), "系统提示", cwd=tmp_path)
    session = Session("s")
    async for _ in agent.run(session, "当前目录有什么？"):
        pass

    # 第二次请求发的是 user + 带 tool_calls 的 assistant + 工具结果
    anchor = session.context_anchor
    assert anchor is not None
    assert (anchor.prompt_tokens, anchor.messages, anchor.model) == (180, 3, "fake-model")
    assert anchor.estimate == request_tokens(llm.requests[1])  # 发出前按字符估的值
    assert len(llm.requests[1]["messages"]) == 1 + 3  # 再加 system
    usage_now = agent.context_usage(session)
    assert usage_now.tokens == 180 + estimate_message(session.messages[3])
    assert usage_now.exact == 180


async def test_agent_without_usage_leaves_no_anchor(tmp_path: Path):
    agent = Agent(FakeLLM(["好"]), ToolRegistry([]), "系统提示", cwd=tmp_path)
    session = Session("s")
    async for _ in agent.run(session, "hi"):
        pass
    assert session.context_anchor is None
    assert agent.context_usage(session).exact == 0


# ------------------------------------------------------------------ 4. 清理旧工具结果（第 2 步）

LONG = "x" * 1200  # 超过 CLEAR_MIN_CHARS，值得清


def assistant(*calls: tuple[str, str]) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": name, "arguments": "{}"}}
            for cid, name in calls
        ],
    }


def tool_msg(call_id: str, content: str = LONG) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


HISTORY = [
    {"role": "user", "content": "q"},
    assistant(("c1", "read_file"), ("c2", "list_dir")),  # 1
    tool_msg("c1"),  # 2
    tool_msg("c2", "ok"),  # 3：太短
    assistant(("c1", "grep")),  # 4：id 跨轮重复
    tool_msg("c1"),  # 5
    assistant(("c3", "bash"), ("c4", "bash"), ("c5", "bash")),  # 6
    tool_msg("c3"),  # 7：模型还没看过
    tool_msg("c4"),  # 8
    tool_msg("c5"),  # 9
]


def test_select_clearable_rules():
    # 最近 1 个（9）不清；7、8 在最后一条 assistant 之后，模型还没看过；3 太短
    assert select_clearable(HISTORY, keep=1) == [(2, "read_file"), (5, "grep")]
    assert select_clearable(HISTORY, keep=5) == [(2, "read_file")]  # 只剩最早的一个
    assert select_clearable(HISTORY, keep=10) == []


def test_select_clearable_skips_already_cleared():
    history = [dict(m) for m in HISTORY]
    history[2] = cleared_result(history[2], "read_file", None)
    assert select_clearable(history, keep=1) == [(5, "grep")]


def test_select_clearable_protects_read_backs():
    """模型把清理文件读回来：说明它要用，不能再清，否则会来回拉锯。"""
    read_back = '{"path": "/home/u/.simpleagent/tool_outputs/cleared-0123456789ab.txt"}'
    history = [
        {"role": "user", "content": "q"},
        assistant(("c1", "read_file")),
        tool_msg("c1"),
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c2",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": read_back},
                }
            ],
        },
        tool_msg("c2"),
        assistant(("c3", "grep")),
        tool_msg("c3"),
        {"role": "assistant", "content": "好了"},
    ]
    assert select_clearable(history, keep=1) == [(2, "read_file")]  # 4 是读回来的，不清


def test_cleared_result_keeps_pairing():
    original = tool_msg("c1")
    with_path = cleared_result(original, "read_file", Path("/tmp/cleared-abc.txt"))
    assert with_path["role"] == "tool"
    assert with_path["tool_call_id"] == "c1"
    assert with_path["content"].startswith(CLEARED_MARK)
    assert "read_file" in with_path["content"]
    assert "1,200 字符" in with_path["content"]
    assert "/tmp/cleared-abc.txt" in with_path["content"]
    assert original["content"] == LONG  # 不改动传入的消息

    without = cleared_result(original, "read_file", None)
    assert "重新调用" in without["content"]


def test_save_cleared_is_deterministic(tmp_path: Path):
    first = save_cleared(tmp_path, "原文")
    assert first is not None and first.read_text(encoding="utf-8") == "原文"
    # 同样的内容得到同一个路径：占位符一字不差，前缀缓存才稳定
    os.utime(first, (0, 0))
    assert save_cleared(tmp_path, "原文") == first
    assert first.stat().st_mtime > 0  # 复用时刷新修改时间，免得被 7 天清理删掉
    assert save_cleared(tmp_path, "别的内容") != first
    assert save_cleared(None, "原文") is None


def test_replace_is_persisted_and_drops_anchor(tmp_path: Path):
    store = SessionStore(tmp_path)
    session = store.start(Session("r"))
    session.add_many(dict(m) for m in HISTORY)
    session.mark_sent(900, 6, "fake-model", 1000)
    lines = store.path("r").read_text().count("\n")

    session.replace({})  # 没有改动：不写记录
    assert store.path("r").read_text().count("\n") == lines
    assert session.context_anchor is not None

    changes = {2: cleared_result(HISTORY[2], "read_file", None)}
    session.replace(changes)
    assert session.messages[2] == changes[2]
    assert session.context_anchor is None  # 改到了上次请求覆盖的范围之内
    assert store.load("r").messages == session.messages  # 重放出同一份历史


def test_context_edited_summary():
    event = ContextEdited("clear", 4000, 1000, 5000, 3)
    assert event.summary() == "清理了 3 个旧工具结果，上下文 4,000 → 1,000 token（80% → 20%）"


class TextArgs(BaseModel):
    text: str = ""


@tool(name="big", description="返回一大段文本")
async def big(args: TextArgs, ctx: ToolContext) -> str:
    return (args.text or "x") * 3000


SMALL = PROFILE.model_copy(update={"context_window": 6000, "max_tokens": 1000})  # 输入上限 5000


def big_calls(n: int) -> list:
    """n 次请求各调一次 big，最后一次回答完成。"""
    script: list = [
        {"tool_calls": [{"id": f"c{i}", "name": "big", "arguments": {"text": str(i)}}]}
        for i in range(n)
    ]
    return [*script, "完成"]


async def test_agent_clears_old_results_before_request(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    llm = FakeLLM(big_calls(5), profile=SMALL)
    agent = Agent(
        llm,
        ToolRegistry([big]),
        "系统提示",
        cwd=tmp_path,
        output_dir=tmp_path / "outputs",
        context=ContextConfig(clear_at=0.6, keep_tool_results=2),
    )
    session = store.start(Session("e"))
    events = [e async for e in agent.run(session, "来")]

    edits = [e for e in events if isinstance(e, ContextEdited)]
    assert edits and all(e.kind == "clear" and e.tokens_after < e.tokens_before for e in edits)

    last = llm.requests[-1]["messages"]
    results = [m for m in last if m["role"] == "tool"]
    assert [m["content"].startswith(CLEARED_MARK) for m in results] == [
        True,
        True,
        True,
        False,
        False,
    ]
    assert [m["content"] for m in results[-2:]] == ["3" * 3000, "4" * 3000]  # 最近 2 个保持原样
    # 每个 tool_call 后面都跟着它的结果：配对完整，API 不会拒绝
    for i, message in enumerate(last):
        for j, call in enumerate(message.get("tool_calls") or []):
            assert last[i + 1 + j]["tool_call_id"] == call["id"]
    # 原文落盘，占位符里给了路径
    saved = sorted((tmp_path / "outputs").glob("cleared-*.txt"))
    assert {p.read_text() for p in saved} == {"0" * 3000, "1" * 3000, "2" * 3000}
    assert str(saved[0].parent) in results[0]["content"]
    assert agent.context_usage(session).tokens <= SMALL.context_window - 1000
    # 清理写进了 JSONL，恢复出来的就是清理后的历史
    assert store.load("e").messages == session.messages


@tool(name="medium", description="返回一小段文本")
async def medium(args: TextArgs, ctx: ToolContext) -> str:
    return "y" * 400  # 值得清（超过 CLEAR_MIN_CHARS），但一个只能省下约 90 token


async def test_agent_skips_clearing_when_gain_is_small(tmp_path: Path):
    """上下文大多是对话正文、可清的很少：不为这点地方打破缓存。"""
    calls = [{"tool_calls": [{"id": f"c{i}", "name": "medium", "arguments": {}}]} for i in range(3)]
    llm = FakeLLM([*calls, "完成"], profile=SMALL)
    agent = Agent(
        llm,
        ToolRegistry([medium]),
        "系统提示",
        cwd=tmp_path,
        context=ContextConfig(clear_at=0.3, keep_tool_results=1),
    )
    session = Session("s")
    # 2000 个汉字的问题：一上来就超过 30%，但可清的加起来不到输入上限的 5%（250 token）
    events = [e async for e in agent.run(session, "问" * 2000)]
    assert agent.context_usage(session).ratio > 0.3
    assert not [e for e in events if isinstance(e, ContextEdited)]
    assert not any(str(m.get("content")).startswith(CLEARED_MARK) for m in session.messages)


async def test_clear_at_zero_disables_clearing(tmp_path: Path):
    llm = FakeLLM(big_calls(5), profile=SMALL)
    agent = Agent(
        llm,
        ToolRegistry([big]),
        "系统提示",
        cwd=tmp_path,
        context=ContextConfig(clear_at=0, compact_at=0),
    )
    events = [e async for e in agent.run(Session("s"), "来")]
    assert not [e for e in events if isinstance(e, ContextEdited)]


# ------------------------------------------------------------------ 5. 摘要压缩（第 3 步）


def assert_paired(messages: list[dict]) -> None:
    """每个 tool_call 后面紧跟着它的结果，tool 消息也都有对应的调用：API 才不会拒绝。"""
    calls = 0
    for i, message in enumerate(messages):
        ids = [call["id"] for call in message.get("tool_calls") or []]
        calls += len(ids)
        assert [m.get("tool_call_id") for m in messages[i + 1 : i + 1 + len(ids)]] == ids
    assert sum(m["role"] == "tool" for m in messages) == calls


def qa(n: int, answer_chars: int = 900) -> list[dict]:
    """n 轮纯对话：回答很长，没有工具结果可清。"""
    history = []
    for i in range(n):
        history += [
            {"role": "user", "content": f"问题{i}"},
            {"role": "assistant", "content": "答" * answer_chars},
        ]
    return history


class SummarizingFake(FakeLLM):
    """遇到压缩指令就回一份摘要（summary 也可以是异常或空串），其余请求照脚本走。"""

    def __init__(self, responses: list, summary=None, **kwargs):
        super().__init__(responses, **kwargs)
        default = {
            "content": "摘要：之前聊了几个问题。",
            "usage": {
                "prompt_tokens": 4000,
                "completion_tokens": 20,
                "prompt_cache_hit_tokens": 3900,
            },
        }
        self.summary = default if summary is None else summary  # 空串也是一种要测的回复
        self.summary_requests = 0

    def stream(self, messages, tools=None, step=0, continue_turn=False):
        if is_summary_request({"messages": messages}):
            self.summary_requests += 1
            self.responses.insert(0, self.summary)
        return super().stream(messages, tools, step, continue_turn)

    def main_requests(self) -> list[dict]:
        return [r for r in self.requests if not is_summary_request(r)]


def is_summary_request(request: dict) -> bool:
    """最后一条是压缩指令。只认 user：工具结果里也可能出现这句话（比如读的正是 context.py）。"""
    last = request["messages"][-1]
    return last.get("role") == "user" and str(last.get("content")).startswith("上下文快满了")


def summary_index(llm: FakeLLM) -> int:
    return next(i for i, r in enumerate(llm.requests) if is_summary_request(r))


def request_tokens(request: dict) -> int:
    messages = request["messages"]
    return (
        estimate_text(messages[0]["content"])
        + estimate_tools(request["tools"])
        + estimate_messages(messages[1:])
    )


def test_find_cut_only_at_boundaries():
    history = [
        {"role": "user", "content": "q1"},  # 0
        assistant(("c1", "read_file")),  # 1：紧跟 user，不是切点
        tool_msg("c1"),  # 2
        assistant(("c2", "grep")),  # 3：紧跟工具结果，是切点
        tool_msg("c2"),  # 4
        {"role": "assistant", "content": "答1"},  # 5：切点
        {"role": "user", "content": "q2"},  # 6：切点
        assistant(("c3", "bash")),  # 7：紧跟 user，不是切点
        tool_msg("c3"),  # 8
        {"role": "assistant", "content": "答2"},  # 9：切点
    ]
    assert find_cut(history, 10**6) == 3  # 全都放得下：保留尽量多，只压最前面一段
    suffix = estimate_messages(history[6:])
    assert find_cut(history, suffix) == 6
    assert find_cut(history, suffix - 1) == 9  # 7 不能当切点，下一个是 9
    for keep in range(0, 5000, 50):
        cut = find_cut(history, keep)
        assert cut in (3, 5, 6, 9)
        assert_paired(history[cut:])


def test_find_cut_falls_back_when_last_step_is_huge():
    history = [
        {"role": "user", "content": "q"},
        assistant(("c1", "read_file")),
        tool_msg("c1"),
        assistant(("c2", "read_file")),
        tool_msg("c2", "x" * 30_000),  # 最近一步本身就远超保留量
    ]
    assert find_cut(history, 100) == 3  # 退到最后一个边界，至少把更早的压掉


def test_find_cut_nothing_to_compact():
    assert find_cut([], 100) is None
    assert find_cut([{"role": "user", "content": "q"}], 100) is None
    assert (
        find_cut([{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}], 0)
        is None
    )
    # 开头只有上一次的摘要可压：再压一遍摘要没有意义
    head = compacted_head("旧摘要", 10, "user")
    assert find_cut([*head, *qa(1)], 0) is None
    assert find_cut([*head, *qa(2)], 0) == 4  # 摘要连同第一轮一起压


def test_compacted_head():
    head = compacted_head("要点若干", 12, "user")
    assert head[0]["role"] == "user"
    assert head[0]["content"].startswith(SUMMARY_MARK)
    assert "12 条消息" in head[0]["content"] and "要点若干" in head[0]["content"]
    assert head[1] == {"role": "assistant", "content": COMPACT_ACK}  # 后面是 user：插一条回复
    assert len(compacted_head("要点若干", 12, "assistant")) == 1


def test_compact_prompt():
    prompt = compact_prompt(1200)
    assert "不超过 1200 字" in prompt and "不要调用工具" in prompt
    assert "合并后整体仍不超过这个长度" in prompt  # 滚动压缩时摘要不能越滚越长
    assert "重点：保留所有报错原文" in compact_prompt(1200, "保留所有报错原文")


def test_session_compact_is_persisted(tmp_path: Path):
    store = SessionStore(tmp_path)
    session = store.start(Session("c"))
    session.add_many(qa(3))
    session.mark_sent(900, 6, "fake-model", 1000)
    head = compacted_head("摘要", 4, "user")
    session.compact(4, head)
    assert session.messages == [*head, *qa(3)[4:]]
    assert session.context_anchor is None
    assert store.load("c").messages == session.messages


def test_compact_summary_text():
    usage = Usage(prompt_tokens=4000, completion_tokens=20, cached_tokens=3900)
    event = ContextEdited("compact", 4500, 1500, 5000, 8, usage=usage)
    assert event.summary() == (
        "压缩了前 8 条消息：上下文 4,500 → 1,500 token（90% → 30%）"
        " · 摘要请求 输入 4,000（缓存 3,900）输出 20"
    )
    failed = ContextEdited("compact", 4500, 4500, 5000, error="模型没有给出摘要")
    assert failed.summary() == "摘要压缩失败（模型没有给出摘要），这一轮不再尝试"


async def test_agent_compacts_with_cache_friendly_request(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    session = store.start(Session("c"))
    session.add_many(qa(5))  # 约 4,500 token 的对话正文，没有工具结果可清
    llm = SummarizingFake(["回答"], profile=SMALL)
    agent = Agent(llm, ToolRegistry([big]), "系统提示", cwd=tmp_path)
    before = [*session.messages, {"role": "user", "content": "新问题"}]
    events = [e async for e in agent.run(session, "新问题")]

    edit = next(e for e in events if isinstance(e, ContextEdited))
    assert (edit.kind, edit.error) == ("compact", None)
    cut = edit.count
    summary_request, main_request = llm.requests
    # 摘要请求 = system + 历史前缀 + 压缩指令，工具列表也一样：正好命中上一次请求的前缀缓存
    assert summary_request["messages"][0] == {"role": "system", "content": "系统提示"}
    assert summary_request["messages"][1:-1] == before[:cut]
    assert "压缩成一份摘要" in summary_request["messages"][-1]["content"]
    assert summary_request["tools"] == main_request["tools"]
    # 压缩后：摘要 +（回复）+ 保留的原文
    sent = main_request["messages"][1:]
    assert sent[0]["content"].startswith(SUMMARY_MARK)
    assert "摘要：之前聊了几个问题。" in sent[0]["content"]
    head_len = 2 if before[cut]["role"] == "user" else 1
    assert sent[head_len:] == before[cut:]
    assert sent[-1] == {"role": "user", "content": "新问题"}
    # 事件：压缩前后的量、摘要请求的用量
    assert edit.tokens_after < edit.tokens_before and edit.tokens_after <= edit.limit
    assert edit.usage is not None and edit.usage.cached_tokens == 3900
    assert request_tokens(main_request) <= SMALL.context_window - SMALL.max_tokens
    assert session.requests == 2  # 摘要请求也算
    assert store.load("c").messages == session.messages


async def test_agent_compacts_mid_turn_without_breaking_pairs(tmp_path: Path):
    """daemon 那种一轮几十步：在一轮内部的步骤边界上压，每次请求都配对完整、不超预算。"""
    llm = SummarizingFake(big_calls(8), profile=SMALL)
    agent = Agent(
        llm,
        ToolRegistry([big]),
        "系统提示",
        cwd=tmp_path,
        context=ContextConfig(clear_at=0, compact_at=0.8),  # 只看压缩
    )
    session = Session("s")
    events = [e async for e in agent.run(session, "来")]

    assert llm.summary_requests >= 2
    assert all(
        e.kind == "compact" and e.error is None for e in events if isinstance(e, ContextEdited)
    )
    for request in llm.main_requests():
        assert_paired(request["messages"][1:])
        assert request_tokens(request) <= SMALL.context_window - SMALL.max_tokens
    assert_paired(session.messages)
    assert session.messages[-1]["content"] == "完成"


async def test_clearing_first_then_no_compaction(tmp_path: Path):
    """清理够用就不压缩：脚本里没有摘要回复，真去压缩的话会把工具调用当摘要吃掉。"""
    llm = FakeLLM(big_calls(5), profile=SMALL)
    agent = Agent(
        llm,
        ToolRegistry([big]),
        "系统提示",
        cwd=tmp_path,
        output_dir=tmp_path / "outputs",
        context=ContextConfig(clear_at=0.6, keep_tool_results=1, compact_at=0.8),
    )
    events = [e async for e in agent.run(Session("s"), "来")]
    kinds = {e.kind for e in events if isinstance(e, ContextEdited)}
    assert kinds == {"clear"}


@pytest.mark.parametrize("summary", [RuntimeError("连不上"), ""])
async def test_failed_compaction_is_reported_and_not_retried(tmp_path: Path, summary):
    llm = SummarizingFake(
        [{"tool_calls": [{"id": "c0", "name": "medium", "arguments": {}}]}, "完成"],
        summary=summary,
        profile=SMALL,
    )
    agent = Agent(llm, ToolRegistry([medium]), "系统提示", cwd=tmp_path)
    session = Session("s", messages=qa(5))
    events = [e async for e in agent.run(session, "新问题")]

    failures = [e for e in events if isinstance(e, ContextEdited)]
    assert len(failures) == 1 and failures[0].error
    assert llm.summary_requests == 1  # 第二步还超阈值，但本轮不再试
    assert session.messages[:10] == qa(5)  # 历史没动
    assert session.messages[-1]["content"] == "完成"  # 主请求照常


@tool(name="snail", description="很久才返回")
async def snail(args: TextArgs, ctx: ToolContext) -> str:
    await asyncio.sleep(10)
    return "不会返回"


async def test_cancel_after_mid_turn_compaction_keeps_history_valid(tmp_path: Path):
    """一轮中途压缩后下标整体前移：本轮起点不跟着调整的话，中断时 _repair 找不到悬空的调用。"""
    calls = big_calls(4)[:-1] + [{"tool_calls": [{"id": "s", "name": "snail", "arguments": {}}]}]
    llm = SummarizingFake(calls, profile=SMALL)
    agent = Agent(
        llm,
        ToolRegistry([big, snail]),
        "系统提示",
        cwd=tmp_path,
        context=ContextConfig(clear_at=0, compact_at=0.8),
    )
    session = Session("s", messages=qa(3, answer_chars=10))  # 本轮从第 6 条开始
    seen: list = []

    async def consume() -> None:
        async for event in agent.run(session, "来"):
            seen.append(event)

    task = asyncio.create_task(consume())
    while not any(isinstance(e, ToolCallStart) and e.name == "snail" for e in seen):
        await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert any(isinstance(e, ContextEdited) and e.kind == "compact" for e in seen)
    assert len(session.messages) < 6  # 压缩后历史比本轮起点还短
    assert_paired(session.messages)
    assert session.messages[-1] == {
        "role": "tool",
        "tool_call_id": "s",
        "content": INTERRUPTED_RESULT,
    }


# -------------------------------------------------------------- 6. 超长兜底、/compact（第 4 步）


def api_error(cls, status: int, message: str, body: dict | None = None):
    request = httpx2.Request("POST", "http://fake.invalid/v1/chat/completions")
    return cls(message, response=httpx2.Response(status, request=request), body=body)


def overflow_error() -> openai.BadRequestError:
    return api_error(
        openai.BadRequestError,
        400,
        "This model's maximum context length is 131072 tokens. However, you requested 140000 "
        "tokens (139000 in the messages, 1000 in the completion).",
    )


def test_is_context_overflow():
    assert is_context_overflow(overflow_error())  # OpenAI / DeepSeek 的说法
    qwen = "<400> InternalError.Algo.InvalidParameter: Range of input length should be [1, 129024]"
    assert is_context_overflow(api_error(openai.BadRequestError, 400, qwen))
    by_code = api_error(
        openai.BadRequestError, 400, "too long", body={"code": "context_length_exceeded"}
    )
    assert is_context_overflow(by_code)
    # 别的 400、别的状态码、连不上：都不是
    schema = "Invalid schema for function 'read_file': 'object' is not valid"
    assert not is_context_overflow(api_error(openai.BadRequestError, 400, schema))
    auth = api_error(openai.AuthenticationError, 401, "maximum context length")
    assert not is_context_overflow(auth)
    request = httpx2.Request("POST", "http://fake.invalid/v1")
    assert not is_context_overflow(openai.APIConnectionError(request=request))
    assert not is_context_overflow(ValueError("maximum context length"))


def test_fill_missing_results():
    history = [
        {"role": "user", "content": "q"},
        assistant(("c1", "read_file"), ("c2", "grep")),
        tool_msg("c1", "ok"),  # c2 没有结果
        {"role": "user", "content": "q2"},
        assistant(("c3", "bash")),  # 结尾悬空
    ]
    fixed = fill_missing_results(history)
    assert_paired(fixed)
    assert fixed[3] == {"role": "tool", "tool_call_id": "c2", "content": INTERRUPTED_RESULT}
    assert fixed[-1] == {"role": "tool", "tool_call_id": "c3", "content": INTERRUPTED_RESULT}
    assert len(history) == 5  # 不改动传入的列表
    assert fill_missing_results(fixed) == fixed  # 本来就完整：原样返回


async def test_overflow_error_compacts_and_retries(tmp_path: Path):
    llm = SummarizingFake([overflow_error(), "回答"])  # 128k 窗口：按估算远没满，服务端却说超长
    agent = Agent(llm, ToolRegistry([]), "系统提示", cwd=tmp_path)
    session = Session("s", messages=qa(3))
    events = [e async for e in agent.run(session, "新问题")]

    edits = [e for e in events if isinstance(e, ContextEdited)]
    assert len(edits) == 1 and edits[0].kind == "compact" and edits[0].error is None
    assert llm.summary_requests == 1
    failed, summary, retry = llm.requests
    # 估算不可信：只留最后一轮（这里就是刚问的这句），其余都压掉
    assert retry["messages"][1]["content"].startswith(SUMMARY_MARK)
    assert retry["messages"][3:] == [{"role": "user", "content": "新问题"}]
    assert session.messages[-1]["content"] == "回答"
    assert_paired(session.messages)


async def test_overflow_twice_gives_up_after_one_compaction(tmp_path: Path):
    llm = SummarizingFake([overflow_error(), overflow_error()])
    agent = Agent(llm, ToolRegistry([]), "系统提示", cwd=tmp_path)
    session = Session("s", messages=qa(3))
    with pytest.raises(openai.BadRequestError):
        [e async for e in agent.run(session, "新问题")]
    assert llm.summary_requests == 1  # 每步只兜底一次，不会循环
    # 压缩留下了，这一轮什么都没得到：用户消息照旧撤回
    assert session.messages[0]["content"].startswith(SUMMARY_MARK)
    assert session.messages[-1]["role"] == "assistant"


async def test_overflow_with_nothing_to_compact_raises(tmp_path: Path):
    llm = SummarizingFake([overflow_error()])
    agent = Agent(llm, ToolRegistry([]), "系统提示", cwd=tmp_path)
    session = Session("s")
    with pytest.raises(openai.BadRequestError):
        [e async for e in agent.run(session, "一句很长很长的话")]
    assert llm.summary_requests == 0
    assert session.messages == []


def test_last_turn_cut_keeps_whole_last_turn():
    history = [
        *qa(2),
        {"role": "user", "content": "最后一问"},  # 4
        assistant(("c1", "read_file")),
        tool_msg("c1"),
        {"role": "assistant", "content": "最后的回答"},
    ]
    assert find_cut(history, 0) == 7  # 只留最后一步：问题本身被压掉了
    assert last_turn_cut(history) == 4  # 保留完整的最后一轮
    # 整段只有一轮（daemon 那种）：退回到最后一个边界
    assert last_turn_cut(history[4:]) == 3
    assert last_turn_cut(qa(1)) is None


async def test_compact_keep_recent_zero_keeps_only_last_turn(tmp_path: Path):
    llm = SummarizingFake([])
    agent = Agent(llm, ToolRegistry([]), "系统提示", cwd=tmp_path)
    session = Session("s", messages=qa(4, answer_chars=10))
    # 默认按 1/4 输入上限留：这么短的对话全放得下，只压最前面一轮
    assert (await agent.compact(Session("t", messages=qa(4, answer_chars=10)))).count == 2
    edited = await agent.compact(session, keep_recent=0)
    assert edited is not None and edited.count == 6
    assert session.messages[2:] == qa(4, answer_chars=10)[6:]


# ------------------------------------------------------------------ 7. 校准与缓存（第 5 步）


def test_calibration_factor_is_clamped():
    assert Calibration.of(ContextAnchor(800, 1, "m", 1000)) == Calibration("m", 0.8)
    assert Calibration.of(ContextAnchor(1000, 1, "m", 100)).factor == 2.0  # 上限
    assert Calibration.of(ContextAnchor(100, 1, "m", 1000)).factor == 0.5  # 下限
    assert Calibration.of(ContextAnchor(100, 1, "m", 0)) is None


def test_measure_uses_calibration_without_anchor():
    full = measure(MESSAGES, system_prompt="s", tools=None, profile=PROFILE).tokens
    calibrated = measure(
        MESSAGES,
        system_prompt="s",
        tools=None,
        profile=PROFILE,
        calibration=Calibration("fake-model", 0.8),
    )
    assert calibrated.tokens == round(full * 0.8) and calibrated.factor == 0.8
    other = measure(
        MESSAGES,
        system_prompt="s",
        tools=None,
        profile=PROFILE,
        calibration=Calibration("other-model", 0.8),
    )
    assert other.tokens == full and other.factor is None  # 别的模型的系数不能拿来用


def test_replace_keeps_calibration():
    session = Session("s", messages=[dict(m) for m in HISTORY])
    session.mark_sent(800, 6, "fake-model", 1000)
    session.replace({2: cleared_result(HISTORY[2], "read_file", None)})
    assert session.context_anchor is None
    assert session.context_calibration == Calibration("fake-model", 0.8)
    session.mark_sent(800, 6, "fake-model", 0)  # 估不出来：沿用上一个系数
    assert session.context_calibration == Calibration("fake-model", 0.8)


async def test_edit_numbers_match_context_command(tmp_path: Path):
    """提示行里「之后」的数，和紧接着 /context 看到的是同一个口径。"""
    script = [
        {**step, "usage": api_usage(700 + 800 * i)} if isinstance(step, dict) else step
        for i, step in enumerate(big_calls(5))
    ]
    llm = FakeLLM(script, profile=SMALL)
    agent = Agent(
        llm,
        ToolRegistry([big]),
        "系统提示",
        cwd=tmp_path,
        context=ContextConfig(clear_at=0.6, keep_tool_results=1, compact_at=0),
    )
    session = Session("s")
    seen = 0
    async for event in agent.run(session, "来"):
        if isinstance(event, ContextEdited):
            seen += 1
            assert event.tokens_after == agent.context_usage(session).tokens
            assert agent.context_usage(session).factor is not None  # 用上了校准系数
    assert seen >= 1


async def test_calibration_prevents_premature_compaction(tmp_path: Path):
    """按字符估超过 80%、校准后没到：不压缩（以前清理完紧接着就可能白压一次）。"""

    async def summary_requests(calibration: Calibration | None) -> int:
        llm = SummarizingFake(["回答"], profile=SMALL)
        agent = Agent(llm, ToolRegistry([]), "系统提示", cwd=tmp_path)
        session = Session("s", messages=qa(5))
        session.context_calibration = calibration
        assert agent.context_usage(session).ratio >= (0.8 if calibration is None else 0)
        async for _ in agent.run(session, "新问题"):
            pass
        return llm.summary_requests

    assert await summary_requests(None) == 1
    assert await summary_requests(Calibration("fake-model", 0.7)) == 0


def test_prepare_messages_continue_turn():
    quirks = Quirks(reasoning_echo="current_turn")
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None, "reasoning_content": "想", "tool_calls": []},
        {"role": "tool", "tool_call_id": "c1", "content": "r"},
        {"role": "user", "content": "请压缩"},
    ]
    assert "reasoning_content" not in prepare_messages(messages, quirks)[1]  # 当成新一轮
    assert prepare_messages(messages, quirks, continue_turn=True)[1]["reasoning_content"] == "想"


async def test_summary_request_keeps_prefix_with_reasoning(tmp_path: Path):
    """current_turn 下，摘要请求里本轮的思考内容照旧回传：和上一次请求的前缀一字不差。
    实测 DeepSeek：去掉思考内容时缓存命中 58%，保留时 99%。"""
    profile = SMALL.model_copy(update={"quirks": Quirks(reasoning_echo="current_turn")})
    script = [
        {**step, "reasoning": "先读一下"} if isinstance(step, dict) else step
        for step in big_calls(6)
    ]
    llm = SummarizingFake(script, profile=profile)
    agent = Agent(
        llm,
        ToolRegistry([big]),
        "系统提示",
        cwd=tmp_path,
        context=ContextConfig(clear_at=0, compact_at=0.8),
    )
    async for _ in agent.run(Session("s"), "来"):
        pass

    k = summary_index(llm)
    assert llm.requests[k]["continue_turn"] is True  # 压到了本轮中间：接着本轮回传
    assert_prefix_matches_previous(llm, k, profile)
    summary = prepare_messages(llm.requests[k]["messages"], profile.quirks, continue_turn=True)
    assert any("reasoning_content" in m for m in summary[:-1])  # 前缀里确实有思考内容


def assert_prefix_matches_previous(llm: FakeLLM, k: int, profile: Profile) -> None:
    """第 k 次请求（摘要请求）去掉最后的指令，和第 k-1 次请求发出去的样子一字不差。"""
    quirks = profile.quirks
    previous = prepare_messages(llm.requests[k - 1]["messages"], quirks)
    summary = prepare_messages(
        llm.requests[k]["messages"], quirks, continue_turn=llm.requests[k]["continue_turn"]
    )
    prefix = summary[:-1]
    shared = min(len(prefix), len(previous))
    assert prefix[:shared] == previous[:shared]


REASONING_PROFILE = SMALL.model_copy(update={"quirks": Quirks(reasoning_echo="current_turn")})


def turn_with_reasoning() -> list:
    """一轮：带思考内容地调两次 big，再带思考内容地回答。"""
    steps = big_calls(2)
    return [
        {**steps[0], "reasoning": "想1"},
        {**steps[1], "reasoning": "想2"},
        {"content": "完成", "reasoning": "想3"},
    ]


async def test_summary_prefix_matches_when_new_turn_triggers_compaction(tmp_path: Path):
    """新一轮一开口就要压缩：上一次请求是上一轮的最后一次，上一轮的思考内容当时是回传的。"""
    llm = SummarizingFake([*turn_with_reasoning(), "好"], profile=REASONING_PROFILE)
    agent = Agent(
        llm,
        ToolRegistry([big]),
        "系统提示",
        cwd=tmp_path,
        context=ContextConfig(clear_at=0, compact_at=0.6),
    )
    session = Session("s")
    async for _ in agent.run(session, "来"):
        pass
    assert llm.summary_requests == 0
    async for _ in agent.run(session, "问" * 1500):  # 这一句把占用推过 60%
        pass
    k = summary_index(llm)
    assert llm.summary_requests == 1 and llm.requests[k]["continue_turn"] is True
    assert_prefix_matches_previous(llm, k, REASONING_PROFILE)


async def test_summary_prefix_matches_after_manual_compact(tmp_path: Path):
    """两轮之后 /compact 只留最后一轮：压的是上一轮，上一次请求里它的思考内容本来就去掉了。"""
    llm = SummarizingFake(
        [*turn_with_reasoning(), *turn_with_reasoning()], profile=REASONING_PROFILE
    )
    agent = Agent(llm, ToolRegistry([big]), "系统提示", cwd=tmp_path)
    session = Session("s")
    for text in ("来", "再来"):
        async for _ in agent.run(session, text):
            pass
    edited = await agent.compact(session, cut=last_turn_cut(session.messages))
    assert edited is not None
    assert llm.requests[-1]["continue_turn"] is False
    assert_prefix_matches_previous(llm, len(llm.requests) - 1, REASONING_PROFILE)


async def test_system_prompt_and_tools_stay_stable(tmp_path: Path):
    """前缀缓存的前提：一个会话里所有请求（含摘要请求、跨轮）的 system prompt 和工具列表不变。"""
    llm = SummarizingFake([*big_calls(6), "好"], profile=SMALL)
    agent = Agent(
        llm,
        ToolRegistry([big]),
        "系统提示",
        cwd=tmp_path,
        output_dir=tmp_path / "outputs",
        context=ContextConfig(clear_at=0.6, keep_tool_results=2, compact_at=0.7),
    )
    session = Session("s", messages=qa(2))  # 两轮清不掉的长对话：清理之后还得压缩
    events = [e async for e in agent.run(session, "来")]
    events += [e async for e in agent.run(session, "谢谢")]
    assert {e.kind for e in events if isinstance(e, ContextEdited)} == {"clear", "compact"}
    assert len({json.dumps(r["messages"][0], ensure_ascii=False) for r in llm.requests}) == 1
    assert len({json.dumps(r["tools"], ensure_ascii=False) for r in llm.requests}) == 1


async def test_compacts_before_clearing_when_both_needed(tmp_path: Path):
    """清完还得压缩：先在原历史上压（前缀和上一次请求一致，吃得到缓存），不先清理。
    先清的话清理改掉了前缀，被清的结果反正也要压进摘要，白清一次（实测命中从 99% 掉到 90%，
    清的是早期消息时几乎全不中）。"""
    history = [
        {"role": "user", "content": "q1"},
        assistant(("t1", "read_file")),
        tool_msg("t1", "x" * 3000),
        assistant(("t2", "read_file")),
        tool_msg("t2", "y" * 3000),
        {"role": "assistant", "content": "答" * 1500},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "答" * 1500},
    ]
    llm = SummarizingFake(["回答"], profile=SMALL)
    agent = Agent(
        llm,
        ToolRegistry([]),
        "系统提示",
        cwd=tmp_path,
        context=ContextConfig(clear_at=0.6, keep_tool_results=1, compact_at=0.7),
    )
    session = Session("s", messages=[dict(m) for m in history])
    events = [e async for e in agent.run(session, "新问题")]

    assert [e.kind for e in events if isinstance(e, ContextEdited)][0] == "compact"
    summary_request = llm.requests[summary_index(llm)]
    sent = summary_request["messages"][1:-1]
    assert sent[: len(history)] == history  # 原历史，t1 没被换成占位符
    assert not any(str(m.get("content")).startswith(CLEARED_MARK) for m in sent)
