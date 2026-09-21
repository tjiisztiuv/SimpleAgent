"""MCP 协议会话：新旧两代的连接、工具发现与调用、server 发来的消息、结果渲染。

假 server 见 `tests/fixtures/mcp/fake_mcp_server.py`，用 --era 切换它讲哪一代协议。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from simpleagent.mcp import CallResult, McpClient, McpError, RpcError, StdioTransport
from simpleagent.mcp import client as client_module
from simpleagent.mcp.client import choose_version, render_content

FAKE_SERVER = Path(__file__).parent.parent / "fixtures" / "mcp" / "fake_mcp_server.py"


def fake(*flags: str, protocol: Any = "auto") -> McpClient:
    transport = StdioTransport("fake", sys.executable, [str(FAKE_SERVER), *flags])
    return McpClient(transport, protocol=protocol)


def recorded(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ------------------------------------------------------------------ 连接：旧协议


async def test_legacy_server_falls_back_to_initialize(tmp_path: Path) -> None:
    record = tmp_path / "received.jsonl"
    async with fake("--era", "legacy", "--record", str(record)) as client:
        info = client.info
        assert info is not None
        assert (info.era, info.protocol_version) == ("legacy", "2025-11-25")
        assert (info.name, info.version) == ("fake-mcp", "1.2.3")
        assert info.instructions == "测试用的 server，工具都是假的。"
        # 假 server 没收到 notifications/initialized 就会拒绝调工具
        assert await client.call_tool("echo", {"text": "hi"}, timeout=5) == CallResult("hi", False)
    messages = recorded(record)
    methods = [m["method"] for m in messages]
    assert methods[:3] == ["server/discover", "initialize", "notifications/initialized"]
    # 旧协议下，连上之后的请求不带 _meta
    assert "_meta" not in messages[3]["params"]


async def test_legacy_server_negotiates_older_version() -> None:
    async with fake("--era", "legacy", "--answer-version", "2025-06-18") as client:
        assert client.info is not None
        assert client.info.protocol_version == "2025-06-18"


async def test_legacy_server_with_unknown_version_is_rejected() -> None:
    client = fake("--era", "legacy", "--answer-version", "2099-01-01")
    with pytest.raises(McpError, match="server 要用协议版本 2099-01-01"):
        await client.connect(timeout=5)
    assert not client.transport.alive
    assert client.transport.returncode is not None  # 连接失败时子进程已经关掉


async def test_silent_server_falls_back_after_probe_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """握手前的请求一概不回：探测超时后按规范当旧 server 处理。"""
    monkeypatch.setattr(client_module, "PROBE_TIMEOUT", 0.3)
    async with fake("--era", "silent") as client:
        assert client.info is not None
        assert client.info.era == "legacy"


async def test_legacy_protocol_skips_probe(tmp_path: Path) -> None:
    """protocol = "legacy"：碰到未握手请求就崩的 server 也能连上。"""
    record = tmp_path / "received.jsonl"
    flags = ("--era", "legacy", "--crash-on-probe", "--record", str(record))
    async with fake(*flags, protocol="legacy") as client:
        assert client.info is not None
        assert client.info.era == "legacy"
    assert "server/discover" not in [m.get("method") for m in recorded(record)]


async def test_crash_on_probe_reports_stderr_and_hint() -> None:
    client = fake("--era", "legacy", "--crash-on-probe")
    with pytest.raises(McpError) as info:
        await client.connect(timeout=5)
    message = str(info.value)
    assert message.startswith("连接 MCP server fake 失败：意外退出（退出码 4）")
    assert "unexpected method server/discover before initialize" in message
    assert 'protocol = "legacy"' in message


# ------------------------------------------------------------------ 连接：新协议


async def test_modern_server_uses_meta_on_every_request(tmp_path: Path) -> None:
    record = tmp_path / "received.jsonl"
    async with fake("--era", "modern", "--record", str(record)) as client:
        info = client.info
        assert info is not None
        assert (info.era, info.protocol_version, info.name) == ("modern", "2026-07-28", "fake-mcp")
        assert info.capabilities == {"tools": {"listChanged": True}}
        await client.list_tools()
        assert (await client.call_tool("echo", {"text": "hi"}, timeout=5)).text == "hi"
    requests = [m for m in recorded(record) if "id" in m]
    assert [m["method"] for m in requests][0] == "server/discover"
    assert "initialize" not in [m["method"] for m in requests]
    for message in requests:
        meta = message["params"]["_meta"]
        assert meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
        assert meta["io.modelcontextprotocol/clientInfo"]["name"] == "simpleagent"
        assert meta["io.modelcontextprotocol/clientCapabilities"] == {}


async def test_dual_era_server_prefers_modern() -> None:
    async with fake("--era", "dual") as client:
        assert client.info is not None
        assert client.info.era == "modern"


async def test_unsupported_version_with_common_legacy_version_falls_back() -> None:
    """新 server 回 -32022，交集里只有旧版本：退回 initialize。"""
    async with fake("--era", "dual", "--supported", "2025-11-25") as client:
        assert client.info is not None
        assert (client.info.era, client.info.protocol_version) == ("legacy", "2025-11-25")


async def test_no_common_version() -> None:
    client = fake("--era", "modern", "--supported", "2099-01-01")
    with pytest.raises(McpError, match="没有共同的协议版本：server 支持 2099-01-01"):
        await client.connect(timeout=5)


async def test_modern_protocol_rejects_legacy_server() -> None:
    client = fake("--era", "legacy", protocol="modern")
    with pytest.raises(McpError, match="server 不支持新协议"):
        await client.connect(timeout=5)


async def test_slow_modern_server_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """只讲新协议的 server 启动太慢：探测超时退回 initialize 被拒，报错里要说清原因。"""
    monkeypatch.setattr(client_module, "PROBE_TIMEOUT", 0.3)
    client = fake("--era", "modern", "--slow-start", "1")
    with pytest.raises(McpError) as info:
        await client.connect(timeout=5)
    message = str(info.value)
    assert "启动太慢" in message
    assert 'protocol = "modern"' in message


async def test_modern_protocol_waits_for_slow_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "PROBE_TIMEOUT", 0.3)
    async with fake("--era", "modern", "--slow-start", "1", protocol="modern") as client:
        assert client.info is not None
        assert client.info.era == "modern"


async def test_legacy_protocol_against_modern_only_server_hints() -> None:
    client = fake("--era", "modern", protocol="legacy")
    with pytest.raises(McpError) as info:
        await client.connect(timeout=5)
    message = str(info.value)
    assert "JSON-RPC 错误 -32022" in message
    assert "这个 server 只讲新协议" in message


async def test_command_not_found_has_no_probe_hint() -> None:
    client = McpClient(StdioTransport("ghost", "definitely-not-a-command-sa-test"))
    with pytest.raises(McpError) as info:
        await client.connect(timeout=5)
    assert "找不到命令" in str(info.value)
    assert "提示" not in str(info.value)


# ------------------------------------------------------------------ 工具


@pytest.mark.parametrize("era", ["legacy", "modern"])
async def test_list_tools_paginates_and_skips_broken(era: str) -> None:
    async with fake("--era", era) as client:
        tools = await client.list_tools()
        tail = client.transport.stderr_tail()
    assert [tool.name for tool in tools] == [
        "echo",
        "fail",
        "image",
        "structured",
        "needs_input",
        "needs_roots",
        "ping_me",
        "toggle",
        "crash",
    ]
    echo = tools[0]
    assert echo.description == "原样返回 text"
    assert echo.input_schema["required"] == ["text"]
    assert echo.annotations == {"readOnlyHint": True}
    assert "跳过格式不对的工具：broken 的 inputSchema 不是对象" in tail


async def test_server_without_tools_capability(tmp_path: Path) -> None:
    record = tmp_path / "received.jsonl"
    async with fake("--era", "legacy", "--no-tools", "--record", str(record)) as client:
        assert await client.list_tools() == []
    assert "tools/list" not in [m.get("method") for m in recorded(record)]


async def test_tool_execution_error_is_passed_through() -> None:
    async with fake() as client:
        assert await client.call_tool("fail", {}, timeout=5) == CallResult("出错了", True)


async def test_unknown_tool_is_protocol_error() -> None:
    async with fake() as client:
        with pytest.raises(RpcError) as info:
            await client.call_tool("nope", {}, timeout=5)
    assert info.value.code == -32602


async def test_call_before_connect() -> None:
    with pytest.raises(McpError, match="还没有连上"):
        await fake().call_tool("echo", {"text": "hi"}, timeout=5)


async def test_image_and_structured_results() -> None:
    async with fake() as client:
        image = await client.call_tool("image", {}, timeout=5)
        structured = await client.call_tool("structured", {}, timeout=5)
    assert image.text == "[图片 image/png，约 2.0 KB，未展示给模型]"
    assert structured.text == '{\n  "temp": 22.5\n}'


async def test_input_required_becomes_readable_error() -> None:
    async with fake("--era", "modern") as client:
        with pytest.raises(McpError, match=r"需要额外输入（elicitation/create）"):
            await client.call_tool("needs_input", {}, timeout=5)


async def test_missing_client_capability_becomes_readable_error() -> None:
    async with fake("--era", "modern") as client:
        with pytest.raises(McpError, match=r"需要客户端能力 \['roots'\]"):
            await client.call_tool("needs_roots", {}, timeout=5)


# ------------------------------------------------------------------ server 发来的消息


async def test_legacy_server_ping_is_answered() -> None:
    async with fake("--era", "legacy") as client:
        result = await client.call_tool("ping_me", {}, timeout=5)
    assert result.text == "{}"  # 客户端回了空结果


async def test_list_changed_and_log_notifications() -> None:
    async with fake("--era", "legacy") as client:
        assert not client.tools_changed
        await client.call_tool("toggle", {}, timeout=5)
        assert client.tools_changed
        assert "[warning fake] 工具列表变了" in client.transport.stderr_tail()
        await client.list_tools()  # 重新列过之后清掉标记
        assert not client.tools_changed


# ------------------------------------------------------------------ 纯函数


@pytest.mark.parametrize(
    ("supported", "expected"),
    [
        (["2026-07-28", "2025-11-25"], "2026-07-28"),
        (["2025-06-18", "2025-11-25"], "2025-11-25"),
        (["2024-11-05"], "2024-11-05"),
        (["2099-01-01"], None),
        ([], None),
        ([None, 3], None),
    ],
)
def test_choose_version(supported: list[Any], expected: str | None) -> None:
    assert choose_version(supported) == expected


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}, "a\nb"),
        (
            {"content": [{"type": "image", "data": "A" * 4096, "mimeType": "image/png"}]},
            "[图片 image/png，约 3.0 KB，未展示给模型]",
        ),
        (
            {"content": [{"type": "audio", "data": "AAA=", "mimeType": "audio/wav"}]},
            "[音频 audio/wav，约 2 字节，未展示给模型]",
        ),
        (
            {"content": [{"type": "resource", "resource": {"uri": "file:///a.txt", "text": "hi"}}]},
            "[资源 file:///a.txt]\nhi",
        ),
        (
            {
                "content": [
                    {
                        "type": "resource",
                        "resource": {"uri": "file:///a.zip", "blob": "AAAA", "mimeType": "x/zip"},
                    }
                ]
            },
            "[资源 file:///a.zip：x/zip，约 3 字节，未展示给模型]",
        ),
        (
            {
                "content": [
                    {
                        "type": "resource_link",
                        "uri": "file:///m.rs",
                        "name": "main.rs",
                        "mimeType": "text/x-rust",
                        "description": "入口",
                    }
                ]
            },
            "[资源链接] main.rs <file:///m.rs>（text/x-rust）：入口",
        ),
        ({"content": [], "structuredContent": {"t": 1}}, '{\n  "t": 1\n}'),
        # 两份都有时只用 content，不重复
        (
            {"content": [{"type": "text", "text": '{"t": 1}'}], "structuredContent": {"t": 1}},
            '{"t": 1}',
        ),
        ({"content": []}, "(无输出)"),
        ({}, "(无输出)"),
        ({"content": [{"type": "hologram"}]}, "[未知内容类型 hologram，已忽略]"),
    ],
)
def test_render_content(result: dict[str, Any], expected: str) -> None:
    assert render_content(result) == expected
