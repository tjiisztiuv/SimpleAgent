"""用 httpx2.MockTransport 模拟 SSE 响应，走真实的 openai SDK 解析路径。"""

import json
from pathlib import Path

import httpx2
import openai
import pytest

from simpleagent.config import Profile, Quirks
from simpleagent.events import ApiRequest, MessageDone, ReasoningDelta, TextDelta
from simpleagent.llm.client import LLMClient, is_loopback
from simpleagent.trace import Tracer


def sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks)
    return (body + "data: [DONE]\n\n").encode()


def chunk(delta: dict | None = None, finish_reason: str | None = None, usage: dict | None = None):
    data = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m"}
    data["choices"] = (
        [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish_reason}]
    )
    if usage is not None:
        data["usage"] = usage
    return data


def make_client(
    handler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **profile_kwargs
) -> LLMClient:
    monkeypatch.setenv("SA_FAKE_KEY", "sk-secret-test")
    profile = Profile(
        base_url="http://llm.invalid/v1",
        model="m",
        api_key_env="SA_FAKE_KEY",
        max_retries=0,
        **profile_kwargs,
    )
    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    tracer = Tracer(tmp_path / "traces", "s1", raw_chunks=True)
    return LLMClient("fake", profile, tracer=tracer, http_client=http_client)


async def test_stream_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    captured = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers["authorization"]
        captured["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse(
                chunk({"role": "assistant", "reasoning_content": "想"}),
                chunk({"content": "你好"}),
                chunk({"content": "！"}, "stop"),
                chunk(usage={"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}),
            ),
        )

    client = make_client(
        handler,
        tmp_path,
        monkeypatch,
        extra_body={"thinking": {"type": "enabled"}},
        max_tokens=256,
        quirks=Quirks(reasoning_echo="none"),
    )
    history = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1", "reasoning_content": "旧思考"},
        {"role": "user", "content": "q2"},
    ]
    events = [event async for event in client.stream(history)]
    await client.close()

    # 请求体
    body = captured["body"]
    assert captured["url"] == "http://llm.invalid/v1/chat/completions"
    assert captured["auth"] == "Bearer sk-secret-test"
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["thinking"] == {"type": "enabled"}  # extra_body 合并到顶层
    assert body["max_tokens"] == 256
    assert "reasoning_content" not in body["messages"][1]

    # 事件流：开头是 API 事件，之后才是 SDK 保留的非标准字段 reasoning_content
    request = events[0]
    assert isinstance(request, ApiRequest)
    assert request.url == "http://llm.invalid/v1/chat/completions"
    assert request.messages == 3
    # extra_body 只放键名，值可能是厂商私有参数
    assert request.options["extra_body"] == ["thinking"]
    assert events[1:4] == [ReasoningDelta("想"), TextDelta("你好"), TextDelta("！")]
    done = events[-1]
    assert isinstance(done, MessageDone)
    assert done.message == {"role": "assistant", "content": "你好！", "reasoning_content": "想"}
    assert done.finish_reason == "stop"
    assert done.usage is not None and done.usage.prompt_tokens == 12
    assert done.ttft is not None

    # trace：记录实际请求体和原始 chunk，但不能泄露 key
    trace_file = tmp_path / "traces" / "s1" / "0001.json"
    trace = json.loads(trace_file.read_text())
    assert trace["status"] == "ok"
    assert trace["request"]["thinking"] == {"type": "enabled"}
    assert trace["response"]["message"]["content"] == "你好！"
    assert len(trace["raw_chunks"]) == 4
    assert "sk-secret-test" not in trace_file.read_text()


async def test_stream_usage_quirk_and_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    captured = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["body"] = json.loads(request.content)
        return httpx2.Response(200, content=sse(chunk({"content": "ok"}, "stop")))

    client = make_client(
        handler,
        tmp_path,
        monkeypatch,
        quirks=Quirks(stream_usage=False, parallel_tool_calls=False),
    )
    tools = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}]
    events = [event async for event in client.stream([{"role": "user", "content": "hi"}], tools)]
    await client.close()

    assert "stream_options" not in captured["body"]
    assert captured["body"]["tools"] == tools
    assert captured["body"]["parallel_tool_calls"] is False
    assert isinstance(events[-1], MessageDone) and events[-1].usage is None


async def test_http_error_is_traced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(400, json={"error": {"message": "bad model"}})

    client = make_client(handler, tmp_path, monkeypatch)
    with pytest.raises(openai.BadRequestError):
        async for _ in client.stream([{"role": "user", "content": "hi"}]):
            pass
    await client.close()

    trace = json.loads((tmp_path / "traces" / "s1" / "0001.json").read_text())
    assert trace["status"] == "error"
    assert "BadRequestError" in trace["error"]


async def test_consumer_stopping_early_is_traced_as_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, content=sse(chunk({"content": "一"}), chunk({"content": "二"}, "stop"))
        )

    client = make_client(handler, tmp_path, monkeypatch)
    stream = client.stream([{"role": "user", "content": "hi"}])
    assert isinstance(await anext(stream), ApiRequest)
    assert await anext(stream) == TextDelta("一")
    await stream.aclose()
    await client.close()

    trace = json.loads((tmp_path / "traces" / "s1" / "0001.json").read_text())
    assert trace["status"] == "cancelled"
    assert trace["response"]["message"]["content"] == "一"


def test_is_loopback():
    assert is_loopback("http://localhost:11434/v1")
    assert is_loopback("http://127.0.0.1:8000/v1")
    assert is_loopback("http://[::1]:8000/v1")
    assert not is_loopback("https://api.deepseek.com")
    assert not is_loopback("http://192.168.1.10:11434/v1")


async def test_loopback_profile_ignores_proxy_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    local = LLMClient("local", Profile(base_url="http://localhost:11434/v1", model="m"))
    remote = LLMClient("remote", Profile(base_url="https://api.example.com/v1", model="m"))
    assert local._client._client.trust_env is False
    assert remote._client._client.trust_env is True
    await local.close()
    await remote.close()
