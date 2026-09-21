"""MCP stdio 传输层：收发、并发、超时与取消、分发、健壮性、关闭。

假 server 见 `tests/fixtures/mcp/fake_server.py`，用当前 Python 启动，不联网、不依赖 node。
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from simpleagent.mcp import McpError, RpcError, StdioTransport
from simpleagent.mcp import transport as transport_module

FAKE_SERVER = Path(__file__).parent.parent / "fixtures" / "mcp" / "fake_server.py"


def fake(*flags: str, **kwargs: Any) -> StdioTransport:
    return StdioTransport("fake", sys.executable, [str(FAKE_SERVER), *flags], **kwargs)


# ------------------------------------------------------------------ 收发


async def test_request_roundtrip() -> None:
    async with fake() as t:
        assert await t.request("echo", {"text": "你好\n换行"}, timeout=5) == {"text": "你好\n换行"}


async def test_concurrent_requests_matched_by_id() -> None:
    """慢请求先发、快请求后发：快的先回来，也要各自交回给自己的调用方。"""
    async with fake() as t:
        slow = asyncio.create_task(t.request("sleep", {"seconds": 0.3}, timeout=5))
        await asyncio.sleep(0.05)
        fast = await t.request("echo", {"n": 1}, timeout=5)
        assert not slow.done()
        assert fast == {"n": 1}
        assert await slow == {"slept": 0.3}


async def test_rpc_error_keeps_code() -> None:
    async with fake() as t:
        with pytest.raises(RpcError) as info:
            await t.request("fail", {"code": -32022, "message": "Unsupported"}, timeout=5)
    assert info.value.code == -32022
    assert info.value.message == "Unsupported"


# ------------------------------------------------------------------ 超时和取消


async def test_timeout_notifies_server() -> None:
    async with fake() as t:
        with pytest.raises(McpError, match="超过 0.2s 没有响应"):
            await t.request("sleep", {"seconds": 1}, timeout=0.2)
        result = await t.request("last_cancelled", timeout=5)
        assert result["cancelled"] == [{"requestId": 1, "reason": "超过 0.2s 没有响应"}]
        # 被放弃的请求稍后才回来：迟到的响应要被丢掉，不影响后面的请求
        await asyncio.sleep(1)
        assert await t.request("echo", {"ok": True}, timeout=5) == {"ok": True}


async def test_caller_cancel_notifies_server() -> None:
    async with fake() as t:
        task = asyncio.create_task(t.request("sleep", {"seconds": 5}, timeout=10))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        result = await t.request("last_cancelled", timeout=5)
        assert result["cancelled"] == [{"requestId": 1, "reason": "用户中断"}]


# ------------------------------------------------------------------ 分发


async def test_notification_goes_to_handler() -> None:
    received: list[tuple[str, dict]] = []
    async with fake(on_notification=lambda method, params: received.append((method, params))) as t:
        await t.request("notify_me", timeout=5)
    # server 先发通知再回响应，读循环按顺序处理：请求返回时通知已经分发过了
    assert received == [("notifications/message", {"text": "hi"})]


async def test_server_request_without_handler_gets_method_not_found() -> None:
    async with fake() as t:
        result = await t.request("ask_client", {"method": "roots/list"}, timeout=5)
    assert result["reply"]["error"]["code"] == -32601


async def test_server_request_answered_by_handler() -> None:
    seen: list[str] = []

    async def on_request(method: str, params: dict) -> dict:
        seen.append(method)
        return {}

    async with fake(on_request=on_request) as t:
        result = await t.request("ask_client", {"method": "ping"}, timeout=5)
    assert seen == ["ping"]
    assert result["reply"]["result"] == {}


async def test_non_json_stdout_is_skipped() -> None:
    async with fake("--banner") as t:
        assert await t.request("garbage", timeout=5) == {"ok": True}
        assert await t.request("echo", {"still": "works"}, timeout=5) == {"still": "works"}
        tail = t.stderr_tail()
    assert "fake server starting..." in tail
    assert "this is not json" in tail
    assert "fake server: listening on stdio" in tail


# ------------------------------------------------------------------ 健壮性


async def test_stderr_flood_does_not_block() -> None:
    """2MB 的 stderr：没人读的话 server 会卡在写日志上，这个请求就永远回不来。"""
    async with fake() as t:
        assert await t.request("stderr_flood", {"lines": 20_000}, timeout=10) == {"ok": True}
        assert t.stderr_tail(1) == "y" * 99


async def test_large_response_beyond_default_limit() -> None:
    async with fake() as t:
        result = await t.request("big", {"size": 1_000_000}, timeout=10)
    assert len(result["text"]) == 1_000_000


async def test_oversized_message_breaks_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport_module, "MAX_MESSAGE_BYTES", 4096)
    async with fake() as t:
        with pytest.raises(McpError, match="超过 4,096 字节"):
            await t.request("big", {"size": 10_000}, timeout=5)
        assert not t.alive


async def test_crash_fails_pending_request_with_exit_code_and_stderr() -> None:
    async with fake() as t:
        pending = asyncio.create_task(t.request("sleep", {"seconds": 5}, timeout=10))
        await asyncio.sleep(0.1)
        with pytest.raises(McpError) as info:
            await t.request("crash", {"code": 3, "stderr": "boom: 配置有误"}, timeout=5)
        message = str(info.value)
        assert "意外退出（退出码 3）" in message
        assert "boom: 配置有误" in message
        # 另一个在途的请求也立刻失败，不用等到它自己超时
        with pytest.raises(McpError, match="意外退出"):
            await pending
        # 之后的请求直接失败
        with pytest.raises(McpError, match="意外退出"):
            await t.request("echo", timeout=5)
        assert not t.alive


async def test_command_not_found() -> None:
    t = StdioTransport("ghost", "definitely-not-a-command-sa-test")
    with pytest.raises(McpError, match="找不到命令 definitely-not-a-command-sa-test"):
        await t.start()


async def test_missing_cwd(tmp_path: Path) -> None:
    with pytest.raises(McpError, match="工作目录不存在"):
        await fake(cwd=tmp_path / "nope").start()


async def test_request_before_start() -> None:
    with pytest.raises(McpError, match="还没有启动"):
        await fake().request("echo", timeout=1)


# ------------------------------------------------------------------ 关闭


async def test_close_via_stdin_eof() -> None:
    t = fake()
    await t.start()
    await t.request("echo", timeout=5)
    await t.close()
    assert t.returncode == 0  # server 读到 EOF 自己退出，没动用信号
    with pytest.raises(McpError, match="已关闭"):
        await t.request("echo", timeout=1)
    await t.close()  # 可以重复调用


async def test_close_escalates_to_sigterm() -> None:
    t = fake("--ignore-eof")
    await t.start()
    await t.request("echo", timeout=5)
    await t.close(grace=0.3)
    assert t.returncode == -signal.SIGTERM


async def test_close_escalates_to_sigkill() -> None:
    t = fake("--ignore-eof", "--ignore-sigterm")
    await t.start()
    await t.request("echo", timeout=5)  # 等 server 装好 SIGTERM 处理再关
    await t.close(grace=0.3)
    assert t.returncode == -signal.SIGKILL


async def test_close_fails_in_flight_request() -> None:
    t = fake()
    await t.start()
    pending = asyncio.create_task(t.request("sleep", {"seconds": 5}, timeout=10))
    await asyncio.sleep(0.1)
    await t.close(grace=0.3)
    with pytest.raises(McpError, match="已关闭"):
        await pending


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_close_kills_whole_process_group(tmp_path: Path) -> None:
    """server 拉起的子进程（像 npx → node）也要一起清掉，不能留成孤儿。"""
    pid_file = tmp_path / "child.pid"
    t = fake("--spawn-child", str(pid_file))
    await t.start()
    await t.request("echo", timeout=5)
    child = int(pid_file.read_text())
    assert _alive(child)
    await t.close()
    deadline = time.monotonic() + 3  # 孤儿被 launchd / init 收尸需要一点时间
    while _alive(child) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not _alive(child)
