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
    message_outline,
)
from simpleagent.llm.client import LLMClient
from simpleagent.llm.fake import FakeLLM
from simpleagent.permissions import Policy
from simpleagent.tools import ToolContext, ToolRegistry, builtin_tools, tool
from simpleagent.ui.debug import DebugRenderer, debug_level, prettify_json
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


def make_debug(level: str = "on") -> tuple[DebugRenderer, io.StringIO, io.StringIO]:
    out, err = io.StringIO(), io.StringIO()
    inner = Renderer(out, color=False, show_reasoning=False)
    return DebugRenderer(inner, err, color=False, level=level), out, err


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
    debug, _, err = make_debug(level="verbose")
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


def make_sent_request(step: int = 1) -> ApiRequest:
    """一条带正文的请求：full 档展开的就是 sent 里的内容。"""
    sent: list[dict[str, Any]] = [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "hi"},
        # content 和 tool_calls 同时在：正文之外还要标出带了几个调用
        {
            "role": "assistant",
            "content": "看一下",
            "tool_calls": [
                {
                    "id": "c0",
                    "type": "function",
                    "function": {"name": "list_dir", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "content": "line\n" * 40},
    ]
    return ApiRequest(
        step,
        "https://api.deepseek.com/v1/chat/completions",
        "deepseek-chat",
        len(sent),
        2,
        1024,
        message_outline(sent),
        {"stream_usage": True},
        sent=sent,
        tool_names=["list_dir", "bash"],
    )


def test_full_expands_request_and_response_bodies():
    debug, _, err = make_debug(level="full")
    debug.on_event(make_sent_request())
    debug.on_event(
        MessageDone(
            message={
                "role": "assistant",
                "content": "读完了",
                "reasoning_content": "先看文件",
                "tool_calls": [
                    {
                        "id": "c0",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'},
                    }
                ],
            }
        )
    )
    text = err.getvalue()

    # 请求侧：每条消息一行表头（role + 大小）+ 缩进正文
    assert "[0] system · 4 B" in text
    assert "│ 系统提示" in text
    assert "[1] user · 2 B" in text
    assert "│ 看一下" in text and "（另有 1 个工具调用）" in text
    assert "│ line" in text
    # 超过 BODY_LINES 的正文截断，并指出全文在哪
    assert "…还有 10 行（全文见 trace）" in text
    assert "tools  list_dir · bash" in text
    # full 档不再打 verbose 的摘要行，免得和正文块重复
    assert "msgs  system" not in text

    # 响应侧：content / reasoning / tool_calls 三块都展开
    assert "\n   message\n" in text
    assert "├ content" in text and "读完了" in text
    assert "├ reasoning · 4 B" in text and "先看文件" in text
    assert "└ tool_calls · 1 个" in text
    assert "1. read_file" in text
    assert '"path": "a.txt"' in text


def test_on_and_verbose_never_print_bodies():
    """正文是 full 档独有的：低档位一个字都不能多打。"""
    for level in ("on", "verbose"):
        debug, _, err = make_debug(level=level)
        debug.on_event(make_sent_request())
        debug.on_event(MessageDone(message={"role": "assistant", "content": "读完了"}))
        text = err.getvalue()
        assert "系统提示" not in text
        assert "读完了" not in text
        assert "\n   message\n" not in text
        assert "│ " not in text


def test_prettify_json_only_touches_real_structure():
    assert prettify_json('{"a": 1}') == '{\n  "a": 1\n}'
    # 流式拼到一半的 JSON 和标量原样返回，不猜也不吞
    assert prettify_json('{"a": ') == '{"a": '
    assert prettify_json("42") == "42"
    assert prettify_json("") == ""


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
    )
    blob = json.dumps(
        {"url": event.url, "outline": event.outline, "options": event.options}, ensure_ascii=False
    )
    assert "sk-should-not-leak" not in blob
    assert event.url == "https://api.deepseek.com/v1/chat/completions"


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
    # full 档隐含前面两档
    assert debug_level(Config.model_validate({**base, "debug": {"full": True}})) == "full"
    # verbose / full 单独打开都不算数（得先开 enabled）
    assert debug_level(Config.model_validate({**base, "debug": {"verbose": True}})) == "off"


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


async def test_full_level_expands_bodies_in_headless(config: Config, tmp_path: Path):
    """full 档跑通整条链路：请求正文和模型返回都进 stderr，stdout 只有正文。"""
    out, err = io.StringIO(), io.StringIO()

    def factory(name: str, profile: Profile) -> FakeLLM:
        return FakeLLM([{"content": "答案", "reasoning": "想想"}], name=name, profile=profile)

    frontend = Headless(config, cwd=tmp_path, llm_factory=factory, out=out, err=err, debug="full")
    assert await frontend.run("干活") == 0

    text = err.getvalue()
    assert "messages" in text and "│ 干活" in text  # 请求正文里能看到用户那句话
    assert "\n   message\n" in text and "答案" in text  # 响应正文
    assert "└ reasoning · 2 B" in text  # 没有工具调用时 reasoning 是最后一块
    assert "答案" in out.getvalue()  # stdout 仍是流式正文，debug 行不混进去


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
