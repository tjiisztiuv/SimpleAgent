"""多个 MCP server 的管理：并行启动与失败隔离、崩溃后重启、关闭、instructions、状态显示。

假 server 见 `tests/fixtures/mcp/fake_mcp_server.py`，它的 crash 工具会让进程直接退出。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from simpleagent.config import McpServerConfig
from simpleagent.mcp import McpError
from simpleagent.mcp import manager as manager_module
from simpleagent.mcp.manager import MID_CALL_EXIT, PROMPT_HEADER, McpManager

FAKE_SERVER = Path(__file__).parent.parent / "fixtures" / "mcp" / "fake_mcp_server.py"


def fake(*flags: str, **fields: Any) -> McpServerConfig:
    return McpServerConfig(command=sys.executable, args=[str(FAKE_SERVER), *flags], **fields)


def methods(record: Path) -> list[str]:
    lines = record.read_text(encoding="utf-8").splitlines()
    return [json.loads(line).get("method", "") for line in lines]


def tool_calls(record: Path) -> list[str]:
    lines = record.read_text(encoding="utf-8").splitlines()
    messages = [json.loads(line) for line in lines]
    return [m["params"]["name"] for m in messages if m.get("method") == "tools/call"]


# ------------------------------------------------------------------ 启动


async def test_start_isolates_failures(sa_home: Path) -> None:
    manager = McpManager(
        {
            "old": fake("--era", "legacy"),
            "new": fake("--era", "modern"),
            "ghost": McpServerConfig(command="definitely-not-a-command-sa-test"),
            "nokey": fake(env_vars=["SA_TEST_MISSING_VAR"]),
            "off": fake(enabled=False),
        }
    )
    try:
        await manager.start()
        states = {server.name: server.state for server in manager.servers}
        assert states == {
            "old": "ready",
            "new": "ready",
            "ghost": "failed",
            "nokey": "failed",
            "off": "disabled",
        }
        names = [tool.name for tool in manager.tools()]
        assert names[0] == "mcp__old__echo"
        assert names.index("mcp__new__echo") == len(names) // 2  # 按配置顺序：old 全部在前
        assert manager.failed
        summary = manager.summary()
        assert summary.startswith("MCP：old ✓ 9 个工具（")
        assert "ghost ✗ 找不到命令 definitely-not-a-command-sa-test" in summary
        assert "nokey ✗ 找不到环境变量 SA_TEST_MISSING_VAR" in summary
        assert "off" not in summary
    finally:
        await manager.close()
    for server in manager.servers:
        if server.client is not None:
            assert not server.client.transport.alive


async def test_no_servers() -> None:
    manager = McpManager({})
    await manager.start()
    assert manager.tools() == [] and manager.summary() == "" and manager.prompt_section() == ""
    await manager.close()


async def test_cancel_during_start_closes_process() -> None:
    manager = McpManager({"slow": fake("--slow-start", "5")})
    task = asyncio.create_task(manager.start())
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    server = manager.servers[0]
    assert (server.state, server.error) == ("failed", "启动被跳过")
    assert server.client is not None and server.client.transport.returncode is not None
    await manager.close()


# ------------------------------------------------------------------ 崩溃和重启


async def test_crash_mid_call_is_not_retried_then_restarts(tmp_path: Path) -> None:
    record = tmp_path / "received.jsonl"
    manager = McpManager({"fake": fake("--record", str(record))})
    await manager.start()
    server = manager.servers[0]
    try:
        with pytest.raises(McpError) as info:
            await server.call_tool("crash", {}, timeout=5)
        message = str(info.value)
        assert "意外退出（退出码 3）" in message
        assert "fake server: crash requested" in message
        assert message.endswith(MID_CALL_EXIT)
        assert tool_calls(record) == ["crash"]  # 没有自动重试
        assert "server 已退出，下次调用时自动重启" in "\n".join(manager.describe())

        result = await server.call_tool("echo", {"text": "又活了"}, timeout=5)
        assert result.text == "又活了"
        assert server.restarts == 1
        assert methods(record).count("initialize") == 2  # 起了第二个进程
        assert "自动重启过 1 次" in manager.describe()[0]
    finally:
        await manager.close()


async def test_parallel_calls_restart_once(tmp_path: Path) -> None:
    record = tmp_path / "received.jsonl"
    manager = McpManager({"fake": fake("--record", str(record))})
    await manager.start()
    server = manager.servers[0]
    try:
        with pytest.raises(McpError):
            await server.call_tool("crash", {}, timeout=5)
        results = await asyncio.gather(
            server.call_tool("echo", {"text": "a"}, timeout=5),
            server.call_tool("echo", {"text": "b"}, timeout=5),
        )
        assert [r.text for r in results] == ["a", "b"]
        assert server.restarts == 1
        assert methods(record).count("initialize") == 2
    finally:
        await manager.close()


async def test_restart_limit() -> None:
    manager = McpManager({"fake": fake()})
    await manager.start()
    server = manager.servers[0]
    try:
        for _ in range(manager_module.MAX_RESTARTS + 1):  # 最初的进程 + 3 次重启，全都崩掉
            with pytest.raises(McpError, match="中途退出"):
                await server.call_tool("crash", {}, timeout=5)
        with pytest.raises(McpError, match="已经自动重启了 3 次，还是退出，不再自动重启") as info:
            await server.call_tool("echo", {"text": "hi"}, timeout=5)
        assert "fake server: crash requested" in str(info.value)
        assert server.restarts == 3
    finally:
        await manager.close()


async def test_failed_restart_keeps_tools_registered() -> None:
    manager = McpManager({"fake": fake()})
    await manager.start()
    server = manager.servers[0]
    try:
        count = len(manager.tools())
        with pytest.raises(McpError):
            await server.call_tool("crash", {}, timeout=5)
        server.config = server.config.model_copy(update={"command": "definitely-not-a-command"})
        with pytest.raises(McpError, match="找不到命令"):
            await server.call_tool("echo", {"text": "hi"}, timeout=5)
        assert server.state == "failed"
        assert len(manager.tools()) == count  # 工具列表不变，调用时报明确的错误
    finally:
        await manager.close()


async def test_calls_after_close_fail() -> None:
    manager = McpManager({"fake": fake()})
    await manager.start()
    await manager.close()
    with pytest.raises(McpError, match="已关闭"):
        await manager.servers[0].call_tool("echo", {"text": "hi"}, timeout=5)
    assert manager.describe() == ["fake   - 已关闭"]


# ------------------------------------------------------------------ instructions 和显示


async def test_prompt_section(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = McpManager({"old": fake("--era", "legacy"), "ghost": McpServerConfig(command="x-no")})
    await manager.start()
    try:
        section = manager.prompt_section()
        assert section == f"\n\n{PROMPT_HEADER}\n\n## old\n测试用的 server，工具都是假的。"
        monkeypatch.setattr(manager_module, "INSTRUCTIONS_LIMIT", 4)
        assert manager.prompt_section().endswith("## old\n测试用的…（已截断）")
    finally:
        await manager.close()


async def test_describe_with_tools_and_warnings() -> None:
    manager = McpManager(
        {
            "fake": fake(enabled_tools=["echo", "fail", "toggle", "nope"]),
            "ghost": McpServerConfig(command="definitely-not-a-command-sa-test"),
            "off": fake(enabled=False),
        }
    )
    await manager.start()
    try:
        await manager.servers[0].call_tool("toggle", {}, timeout=5)
        lines = manager.describe(tools=True)
    finally:
        await manager.close()
    assert lines[0].startswith(
        "fake    ✓ 旧协议 2025-11-25 · fake-mcp 1.2.3 · 3 个工具，schema 约 "
    )
    assert lines[1] == "    server 说工具列表变了：本次会话不刷新，重启 sa 后生效"
    assert lines[2].split() == ["mcp__fake__echo", "免确认", "·", "可并行"]
    assert lines[3].split() == ["mcp__fake__fail", "需确认"]
    assert lines[5] == "    ! fake：配置的 enabled_tools 里有 nope，但 server 没有这个工具"
    assert lines[6].startswith("ghost   ✗ 连接 MCP server ghost 失败：找不到命令")
    assert lines[-1] == "off     - 已停用（enabled = false）"
