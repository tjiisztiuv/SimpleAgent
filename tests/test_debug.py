"""debug 模式测试：API 事件、工具事件的字段，以及 stderr 上的渲染。

全部用 FakeLLM，不联网。核心要证明两件事：
1. 每一次 API 调用都有始有终（失败和中断也算），工具调用带得上耗时和权限判定；
2. debug 行只写 stderr，正文不受影响，且不含任何凭证。
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any

import httpx2
import openai
import pytest
from pydantic import BaseModel

from simpleagent.agent.loop import Agent
from simpleagent.agent.session import Session
from simpleagent.config import Config, Profile
from simpleagent.events import (
    ApiRequest,
    ApiResponse,
    MessageDone,
    ToolCallStart,
    ToolResult,
    Usage,
)
from simpleagent.llm.client import LLMClient, message_outline
from simpleagent.llm.fake import FakeLLM
from simpleagent.permissions import Policy
from simpleagent.tools import ToolContext, ToolRegistry, builtin_tools, tool
from simpleagent.ui.debug import DebugRenderer, debug_level
from simpleagent.ui.headless import Headless
from simpleagent.ui.repl import Renderer, Repl


class NoArgs(BaseModel):
    pass


@tool(name="slow", description="很久才返回")
async def slow(args: NoArgs, ctx: ToolContext) -> str:
    await asyncio.sleep(10)
    return "不会返回"


@tool(name="needs_approval", description="写操作，默认要问", readonly=False, permission="ask")
async def needs_approval(args: NoArgs, ctx: ToolContext) -> str:
    return "已写入"


@tool(name="huge", description="输出很长")
async def huge(args: NoArgs, ctx: ToolContext) -> str:
    return "x" * 5000


async def run(agent: Agent, session: Session, text: str = "hi") -> list[Any]:
    return [event async for event in agent.run(session, text)]


def make_agent(tmp_path: Path, llm: FakeLLM, registry: ToolRegistry | None = None) -> Agent:
    return Agent(
        llm,
        registry or ToolRegistry(builtin_tools()),
        "系统提示",
        cwd=tmp_path,
    )


# ------------------------------------------------------------ 1. API 事件


async def test_every_request_has_start_and_end(tmp_path: Path):
    llm = FakeLLM(
        [
            {
                "content": "我看看",
                "tool_calls": [{"id": "c0", "name": "list_dir", "arguments": {"depth": 1}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 8},
            },
            "结束了",
        ]
    )
    events = await run(make_agent(tmp_path, llm), Session("s"), "当前目录有什么？")

    requests = [e for e in events if isinstance(e, ApiRequest)]
    responses = [e for e in events if isinstance(e, ApiResponse)]
    assert [e.step for e in requests] == [1, 2]
    assert [e.status for e in responses] == ["ok", "ok"]
    # 每次请求都带 tools，请求体随工具结果变大
    assert requests[0].tools == 7
    assert requests[0].messages == 2  # system + user
    assert requests[1].messages == 4  # + assistant + tool
    assert requests[1].payload_bytes > requests[0].payload_bytes
    assert requests[0].url.endswith("/chat/completions")
    # 消息清单只记 role 和字符数，不带正文
    assert requests[1].outline[-1]["role"] == "tool"
    assert {m["role"] for m in requests[1].outline} == {"system", "user", "assistant", "tool"}
    assert requests[0].options == {}

    # 工具调用标出了属于第几次请求
    start = next(e for e in events if isinstance(e, ToolCallStart))
    assert (start.step, start.name, start.permission, start.readonly) == (
        1,
        "list_dir",
        "allow",
        True,
    )
    # usage 和 finish_reason 在响应行里就能看到，不用等 MessageDone
    assert responses[0].usage is not None and responses[0].usage.prompt_tokens == 20
    assert responses[0].finish_reason == "tool_calls"


async def test_api_error_is_reported_as_error(tmp_path: Path):
    request = httpx2.Request("POST", "http://fake.invalid/v1/chat/completions")
    error = openai.APIStatusError(
        "限流了", response=httpx2.Response(429, request=request), body=None
    )
    llm = FakeLLM([error])
    events: list[Any] = []
    with pytest.raises(openai.APIStatusError):
        async for event in make_agent(tmp_path, llm).run(Session("s"), "hi"):
            events.append(event)

    response = next(e for e in events if isinstance(e, ApiResponse))
    assert (response.status, response.status_code) == ("error", 429)
    assert "APIStatusError" in (response.error or "")
    assert response.usage is None


async def test_cancel_reports_cancelled_or_nothing(tmp_path: Path):
    """取消发生在流式期间时，client 会把这次调用报成 cancelled。

    异步生成器在被取消的那一刻 yield 会再抛一次 CancelledError，事件可能来不及发出，
    所以这里只要求「凡是发出的都标成 cancelled」，绝不标成 ok 或 error。
    """
    llm = FakeLLM([{"content": "y" * 400, "delay": 0.05}])
    agent = make_agent(tmp_path, llm)
    seen: list[Any] = []

    async def consume() -> None:
        async for event in agent.run(Session("s"), "hi"):
            seen.append(event)

    task = asyncio.create_task(consume())
    while not seen:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [e.status for e in seen if isinstance(e, ApiResponse)] in ([], ["cancelled"])


# ------------------------------------------------------------ 2. 工具事件


async def test_tool_result_carries_duration_and_decision(tmp_path: Path):
    llm = FakeLLM([{"tool_calls": [{"id": "c0", "name": "needs_approval"}]}, "被拒了"])
    # policy 生效、没有审批器：ask 一律按拒绝处理
    registry = ToolRegistry([needs_approval], policy=Policy(tmp_path))
    events = await run(make_agent(tmp_path, llm, registry), Session("s"))

    result = next(e for e in events if isinstance(e, ToolResult))
    assert result.is_error
    assert result.decision == "ask"
    assert result.duration_ms >= 0


async def test_allowed_call_records_decision_and_duration(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hi")
    llm = FakeLLM([{"tool_calls": [{"id": "c0", "name": "list_dir"}]}, "好了"])
    events = await run(make_agent(tmp_path, llm), Session("s"))

    result = next(e for e in events if isinstance(e, ToolResult))
    assert result.decision == "allow"
    assert not result.is_error
    assert result.duration_ms >= 0


async def test_truncated_output_is_flagged(tmp_path: Path):
    llm = FakeLLM([{"tool_calls": [{"id": "c0", "name": "huge"}]}, "内容很长"])
    registry = ToolRegistry([huge], max_output_chars=200, max_output_lines=5)
    events = await run(make_agent(tmp_path, llm, registry), Session("s"))

    result = next(e for e in events if isinstance(e, ToolResult))
    assert result.truncated
    assert "截断" in result.content
    assert result.decision == "allow"


async def test_unknown_tool_has_no_decision(tmp_path: Path):
    llm = FakeLLM([{"tool_calls": [{"id": "c0", "name": "not_a_tool"}]}, "没有这个工具"])
    events = await run(make_agent(tmp_path, llm), Session("s"))

    result = next(e for e in events if isinstance(e, ToolResult))
    # 参数校验之前就失败了，谈不上判定
    assert result.decision is None
    start = next(e for e in events if isinstance(e, ToolCallStart))
    # 未知工具按只读、默认等级上报：它只会得到一条错误结果
    assert (start.permission, start.readonly) == ("allow", True)


# ------------------------------------------------------------ 3. 渲染


def make_debug(
    level: str = "on", body_chars: int = 600
) -> tuple[DebugRenderer, io.StringIO, io.StringIO]:
    out, err = io.StringIO(), io.StringIO()
    inner = Renderer(out, color=False, show_reasoning=False)
    renderer = DebugRenderer(inner, err, color=False, level=level, body_chars=body_chars)
    return renderer, out, err


def test_debug_renderer_writes_process_to_stderr():
    debug, out, err = make_debug()
    debug.on_event(
        ApiRequest(
            1,
            "https://api.deepseek.com/v1/chat/completions",
            "deepseek-chat",
            2,
            7,
            14_200,
            [{"role": "system", "chars": 412}, {"role": "user", "chars": 28}],
            {"stream_usage": True, "extra_body": ["thinking"]},
        )
    )
    debug.on_event(
        ApiResponse(
            1,
            "ok",
            1.32,
            0.41,
            usage=Usage(prompt_tokens=3412, completion_tokens=256, cached_tokens=1024),
            finish_reason="tool_calls",
        )
    )
    debug.on_event(ToolCallStart("c0", "grep", '{"pattern": "def run"}', step=1))
    debug.on_event(ToolResult("c0", "grep", "a.py\nb.py\n", duration_ms=12, decision="allow"))

    text = err.getvalue()
    assert "── 轮 1" in text
    assert "POST https://api.deepseek.com/v1/chat/completions" in text
    assert "deepseek-chat · 2 msgs · 7 tools · 13.9 KB" in text
    assert "1.32s · ttft 0.41s · in 3,412(缓存 1,024) · out 256 → tool_calls" in text
    assert "▸ grep" in text and "12ms" in text and "判定 allow" in text
    # verbose 档才有的消息清单和请求开关
    assert "msgs  system" not in text
    assert "extra_body" not in text
    # 正文一行都没被 debug 写进去
    assert out.getvalue() == ""


def test_verbose_adds_outline_without_content():
    debug, _, err = make_debug("verbose")
    debug.on_event(
        ApiRequest(
            2,
            "http://fake.invalid/v1/chat/completions",
            "m",
            3,
            1,
            500,
            [{"role": "system", "chars": 412}, {"role": "user", "chars": 28}],
            {"stream_usage": True, "extra_body": ["thinking"]},
        )
    )
    text = err.getvalue()
    assert "msgs  system 412B · user 28B" in text
    assert "opts  stream_usage=True" in text
    assert "extra_body=thinking" in text
    # 只有结构，没有正文
    assert "412" in text and "系统提示" not in text


def test_failed_and_denied_lines_are_marked():
    debug, _, err = make_debug()
    debug.on_event(ApiResponse(1, "error", 0.5, status_code=429, error="APIStatusError: 限流"))
    debug.on_event(
        ToolResult("c0", "bash", "错误：危险命令", is_error=True, duration_ms=0.2, decision="deny")
    )
    text = err.getvalue()
    assert "⟨ 429" in text
    assert "APIStatusError" in text
    assert "判定 deny" in text


def test_api_request_never_carries_credentials(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LEAKY_KEY", "sk-should-not-leak")
    profile = Profile(base_url="https://api.deepseek.com/v1", model="deepseek-chat")
    profile = profile.model_copy(update={"api_key_env": "LEAKY_KEY"})
    client = LLMClient("p", profile)
    request = client.build_request([{"role": "user", "content": "hi"}], tools=[])
    event = ApiRequest(
        1,
        client.endpoint(),
        profile.model,
        len(request["messages"]),
        0,
        len(json.dumps(request)),
        message_outline(request["messages"]),
        client.request_options(),
        request,
    )
    blob = json.dumps(
        {
            "url": event.url,
            "outline": event.outline,
            "options": event.options,
            "payload": event.payload,
        },
        ensure_ascii=False,
    )
    assert "sk-should-not-leak" not in blob
    assert event.url == "https://api.deepseek.com/v1/chat/completions"
    # payload 是实际请求体：有 messages，没有 header / key
    assert event.payload is not None
    assert event.payload["messages"][0]["content"] == "hi"
    assert "api_key" not in event.payload and "headers" not in event.payload


# ---- full 档：实际发给模型的输入和模型返回的结构

GLOB_TOOL = {
    "type": "function",
    "function": {
        "name": "glob",
        "description": "按模式找文件",
        "parameters": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
        },
    },
}


def api_request_with(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, step: int = 1
) -> ApiRequest:
    return ApiRequest(
        step,
        "http://a.invalid/v1/chat/completions",
        "m",
        len(messages),
        len(tools or []),
        100,
        message_outline(messages),
        {},
        {"model": "m", "messages": messages, "tools": tools or []},
    )


def test_full_prints_request_body_and_tools():
    debug, _, err = make_debug("full")
    messages = [
        {"role": "system", "content": "你是 SimpleAgent"},
        {"role": "user", "content": "看看有多少 py 文件"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c0",
                    "type": "function",
                    "function": {"name": "glob", "arguments": '{"pattern": "**/*.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c0", "content": "a.py\nb.py"},
    ]
    debug.on_event(api_request_with(messages, [GLOB_TOOL]))

    text = err.getvalue()
    assert "请求体  4 条消息" in text
    assert "[1] system" in text and "你是 SimpleAgent" in text
    assert "[2] user" in text and "看看有多少 py 文件" in text
    assert "[3] assistant" in text and "tool_calls=1" in text
    assert '⚙ glob #c0  {"pattern": "**/*.py"}' in text
    assert "[4] tool #c0" in text and "a.py" in text
    # 工具只打名字和参数字段名，不打完整 schema
    assert "tools  glob(pattern,path)" in text
    assert "按模式找文件" not in text
    # full 档每条消息自带字符数，verbose 的摘要行就不重复了
    assert "msgs  system" not in text


def test_full_prints_only_new_messages_next_step():
    debug, _, err = make_debug("full")
    first = [
        {"role": "system", "content": "系统提示原文"},
        {"role": "user", "content": "问题"},
    ]
    debug.on_event(api_request_with(first))
    assert "系统提示原文" in err.getvalue()

    err.truncate(0), err.seek(0)
    second = [
        *first,
        {"role": "assistant", "content": "回答"},
        {"role": "tool", "tool_call_id": "c0", "content": "工具结果"},
    ]
    debug.on_event(api_request_with(second, step=2))
    text = err.getvalue()
    assert "（前 2 条同上次）" in text
    assert "系统提示原文" not in text
    assert "[4] tool #c0" in text and "工具结果" in text

    # /clear 之后条数变少：重新打一遍完整上下文
    err.truncate(0), err.seek(0)
    debug.on_event(api_request_with([{"role": "system", "content": "系统提示原文"}], step=3))
    assert "系统提示原文" in err.getvalue()


def test_full_lists_tools_once_until_they_change():
    debug, _, err = make_debug("full")
    messages = [{"role": "user", "content": "问题"}]
    debug.on_event(api_request_with(messages, [GLOB_TOOL]))
    assert "glob(pattern,path)" in err.getvalue()

    err.truncate(0), err.seek(0)
    debug.on_event(api_request_with(messages, [GLOB_TOOL], step=2))
    assert "tools  1 个（同上次）" in err.getvalue()

    # 工具变了就重新列一遍：换模型 / 改工具集时最想看到这行
    err.truncate(0), err.seek(0)
    bash = {"type": "function", "function": {"name": "bash", "parameters": {"properties": {}}}}
    debug.on_event(api_request_with(messages, [GLOB_TOOL, bash], step=3))
    text = err.getvalue()
    assert "glob(pattern,path) · bash" in text and "共 2 个" in text


def test_full_truncates_long_content():
    debug, _, err = make_debug("full", body_chars=50)
    debug.on_event(api_request_with([{"role": "user", "content": "长" * 500}]))
    text = err.getvalue()
    assert "500 字" in text  # 抬头仍报完整长度
    assert "…（共 500 字）" in text
    assert text.count("长") == 50  # body_chars 是唯一的闸门


def test_full_body_chars_zero_keeps_everything():
    debug, _, err = make_debug("full", body_chars=0)
    debug.on_event(api_request_with([{"role": "user", "content": "长" * 500}]))
    assert err.getvalue().count("长") == 500


def test_full_prints_response_structure():
    debug, _, err = make_debug("full")
    debug.on_event(
        MessageDone(
            message={
                "role": "assistant",
                "content": None,
                "reasoning_content": "先看看目录",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "glob", "arguments": '{"pattern": "src/**/*.py"}'},
                    }
                ],
            },
            finish_reason="tool_calls",
            usage=Usage(
                prompt_tokens=100, completion_tokens=20, cached_tokens=64, reasoning_tokens=8
            ),
        )
    )
    text = err.getvalue()
    assert "响应  assistant · finish_reason=tool_calls" in text
    assert "思考  5 字" in text and "先看看目录" in text
    assert "正文  （content=null）" in text
    assert "工具调用 1" in text and '⚙ glob #c1  {"pattern": "src/**/*.py"}' in text
    assert "usage  in 100(缓存 64) · out 20(思考 8)" in text


def test_full_flags_invalid_tool_arguments():
    debug, _, err = make_debug("full")
    debug.on_event(
        MessageDone(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c2",
                        "type": "function",
                        "function": {"name": "grep", "arguments": '{"pattern": '},
                    }
                ],
            },
            finish_reason="tool_calls",
        )
    )
    text = err.getvalue()
    assert "⚠ 参数不是合法 JSON" in text
    assert '{"pattern":' in text  # 原样打出来，才看得出模型吐了半截


@pytest.mark.parametrize("level", ["on", "verbose"])
def test_lower_levels_never_print_body(level: str):
    debug, _, err = make_debug(level)
    debug.on_event(api_request_with([{"role": "system", "content": "机密的系统提示"}], [GLOB_TOOL]))
    debug.on_event(
        MessageDone(message={"role": "assistant", "content": "模型的回答"}, finish_reason="stop")
    )
    text = err.getvalue()
    assert "机密的系统提示" not in text
    assert "模型的回答" not in text
    assert "请求体" not in text and "响应  assistant" not in text


# ------------------------------------------------------------ 4. 开关


def test_debug_level_from_config():
    base = {
        "default_profile": "a",
        "profiles": {"a": {"base_url": "http://a.invalid/v1", "model": "m"}},
    }
    assert debug_level(Config.model_validate(base)) == "off"
    assert debug_level(Config.model_validate({**base, "debug": {"enabled": True}})) == "on"
    assert (
        debug_level(Config.model_validate({**base, "debug": {"enabled": True, "verbose": True}}))
        == "verbose"
    )
    assert debug_level(Config.model_validate({**base, "debug": {"full": True}})) == "full"
    # full 隐含 verbose，verbose 隐含 enabled：只写一个也算数
    assert debug_level(Config.model_validate({**base, "debug": {"verbose": True}})) == "verbose"


async def test_repl_debug_switch_and_stderr_output(config: Config, tmp_path: Path):
    out, err = io.StringIO(), io.StringIO()
    fake = FakeLLM(["你好"], name="a", profile=config.profiles["a"])

    def factory(name: str, profile: Profile) -> FakeLLM:
        return fake

    repl = Repl(
        config,
        llm_factory=factory,
        out=out,
        err=err,
        input_fn=lambda _: "/exit",
    )
    assert repl.debug == "off"

    await repl.handle("/debug verbose")
    assert repl.debug == "verbose"
    await repl.handle("你好")
    assert "/chat/completions" in err.getvalue()
    assert "你好" in out.getvalue()

    await repl.handle("/debug")  # 不带参数：只显示当前档位
    assert "debug：verbose" in out.getvalue()
    await repl.handle("/debug nonsense")
    assert "用法：/debug" in out.getvalue()


async def test_repl_full_debug_prints_context_incrementally(config: Config, tmp_path: Path):
    out, err = io.StringIO(), io.StringIO()
    fake = FakeLLM(["你好", "再见"], name="a", profile=config.profiles["a"])

    def factory(name: str, profile: Profile) -> FakeLLM:
        return fake

    repl = Repl(
        config,
        llm_factory=factory,
        out=out,
        err=err,
        input_fn=lambda _: "/exit",
        debug="full",
    )
    await repl.handle("第一句")
    text = err.getvalue()
    assert "请求体  2 条消息" in text  # system + user
    assert "第一句" in text

    err.truncate(0), err.seek(0)
    await repl.handle("第二句")
    text = err.getvalue()
    assert "（前 2 条同上次）" in text
    assert "第一句" not in text
    assert "[4] user" in text and "第二句" in text
    # 响应结构也打出来了
    assert "响应  assistant" in text and "正文  2 字" in text


async def test_headless_debug_goes_to_stderr(config: Config, tmp_path: Path):
    out, err = io.StringIO(), io.StringIO()
    factory_calls: list[FakeLLM] = []

    def factory(name: str, profile: Profile) -> FakeLLM:
        fake = FakeLLM(["做完了"], name=name, profile=profile)
        factory_calls.append(fake)
        return fake

    frontend = Headless(config, cwd=tmp_path, llm_factory=factory, out=out, err=err, debug="on")
    assert await frontend.run("干活") == 0

    assert "做完了" in out.getvalue()
    assert "/chat/completions" in err.getvalue()
    # debug 模式下不再重复打统计行
    assert "耗时" not in out.getvalue()


async def test_debug_off_leaves_output_untouched(config: Config, tmp_path: Path):
    out, err = io.StringIO(), io.StringIO()

    def factory(name: str, profile: Profile) -> FakeLLM:
        return FakeLLM(["做完了"], name=name, profile=profile)

    frontend = Headless(config, cwd=tmp_path, llm_factory=factory, out=out, err=err)
    assert await frontend.run("干活") == 0
    assert err.getvalue() == ""
    assert "耗时" in out.getvalue()


# ------------------------------------------------------------ 5. SSE 帧


def test_debug_events_have_frames():
    from simpleagent.serve.frames import event_to_frame

    request = event_to_frame(
        ApiRequest(1, "http://a.invalid/v1/chat/completions", "m", 2, 1, 100), "s1"
    )
    assert request.type == "api_request"
    assert request.payload["payload_bytes"] == 100

    response = event_to_frame(
        ApiResponse(1, "ok", 1.5, 0.4, usage=Usage(prompt_tokens=10, completion_tokens=2)), "s1"
    )
    assert response.type == "api_response"
    assert response.payload["usage"]["prompt_tokens"] == 10

    result = event_to_frame(
        ToolResult("c0", "grep", "x", duration_ms=3.5, decision="allow", truncated=True), "s1"
    )
    assert result.payload["duration_ms"] == 3.5
    assert result.payload["decision"] == "allow"
    assert result.payload["truncated"] is True
