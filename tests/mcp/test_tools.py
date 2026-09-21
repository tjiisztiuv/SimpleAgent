"""MCP 工具接进注册表：子进程环境、工具名、schema 整理、权限对应、端到端调用。

端到端的部分接 `tests/fixtures/mcp/fake_mcp_server.py`，再用 FakeLLM 跑一轮完整的 agent loop。
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from simpleagent.agent.loop import Agent
from simpleagent.agent.session import Session
from simpleagent.config import McpServerConfig
from simpleagent.llm.fake import FakeLLM
from simpleagent.mcp import CallResult, McpClient, McpError, McpTool, StdioTransport
from simpleagent.mcp.tools import (
    BASE_ENV,
    NAME_LIMIT,
    client_from_config,
    normalize_schema,
    server_env,
    tool_name,
    wrap_tools,
)
from simpleagent.permissions import ApprovalDecision, ApprovalRequest, Policy
from simpleagent.tools import ToolContext, ToolRegistry, builtin_tools

FAKE_SERVER = Path(__file__).parent.parent / "fixtures" / "mcp" / "fake_mcp_server.py"
OBJECT = {"type": "object"}


def config(**fields: Any) -> McpServerConfig:
    return McpServerConfig.model_validate({"command": "npx", **fields})


def call(name: str, arguments: dict | str, call_id: str = "c1") -> dict:
    text = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": text}}


class RecordingApprover:
    def __init__(self, allow: bool) -> None:
        self.allow = allow
        self.requests: list[ApprovalRequest] = []

    async def request(self, req: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(req)
        return ApprovalDecision(allow=self.allow)


class NullCaller:
    async def call_tool(self, name: str, arguments: dict, *, timeout: float) -> CallResult:
        return CallResult(f"{name} {arguments}", False)


# ------------------------------------------------------------------ 子进程环境


def test_server_env_is_a_whitelist() -> None:
    environ = {
        "HOME": "/home/u",
        "PATH": "/usr/bin",
        "LANG": "zh_CN.UTF-8",
        "https_proxy": "http://127.0.0.1:7890",
        "DEEPSEEK_API_KEY": "sk-profile-key",
        "AWS_SECRET_ACCESS_KEY": "aws",
        "RANDOM_THING": "x",
    }
    env = server_env(config(env={"LOG_LEVEL": "debug"}), environ=environ, dotenv={})
    assert env == {
        "HOME": "/home/u",
        "PATH": "/usr/bin",
        "LANG": "zh_CN.UTF-8",
        "https_proxy": "http://127.0.0.1:7890",
        "LOG_LEVEL": "debug",
    }
    assert set(env) - {"LOG_LEVEL"} <= set(BASE_ENV)


def test_env_vars_from_environ_then_dotenv() -> None:
    environ = {"GITHUB_TOKEN": "from-env"}
    dotenv = {"GITHUB_TOKEN": "from-dotenv", "NOTION_KEY": "from-dotenv"}
    env = server_env(
        config(env_vars=["GITHUB_TOKEN", "NOTION_KEY"]), environ=environ, dotenv=dotenv
    )
    assert env["GITHUB_TOKEN"] == "from-env"  # 环境变量优先，和 profile 的 api_key_env 一样
    assert env["NOTION_KEY"] == "from-dotenv"


def test_env_vars_read_real_dotenv_file(sa_home: Path) -> None:
    sa_home.mkdir(parents=True, exist_ok=True)
    (sa_home / ".env").write_text("GITHUB_TOKEN=ghp_from_file\n", encoding="utf-8")
    env = server_env(config(env_vars=["GITHUB_TOKEN"]), environ={})
    assert env["GITHUB_TOKEN"] == "ghp_from_file"


def test_missing_env_var_is_reported(sa_home: Path) -> None:
    with pytest.raises(McpError, match=r"找不到环境变量 GITHUB_TOKEN：.*\.env"):
        server_env(config(env_vars=["GITHUB_TOKEN"]), environ={}, dotenv={})


def test_client_from_config_expands_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/Users/someone")
    client = client_from_config(
        "fs", config(command="~/bin/server", args=["~/notes", "--x=~"], cwd="~", protocol="legacy")
    )
    transport = client.transport
    assert transport.command == "/Users/someone/bin/server"
    assert transport.args == ["/Users/someone/notes", "--x=~"]  # 只展开开头的 ~
    assert transport.cwd == Path("/Users/someone")
    assert client.protocol == "legacy"
    assert transport.env is not None and transport.env["HOME"] == "/Users/someone"


# ------------------------------------------------------------------ 工具名与 schema


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("read_text_file", "mcp__fs__read_text_file"),
        ("admin.tools.list", "mcp__fs__admin_tools_list"),
        ("get user/info", "mcp__fs__get_user_info"),
        ("中文", "mcp__fs____"),
    ],
)
def test_tool_name(remote: str, expected: str) -> None:
    assert tool_name("fs", remote) == expected


def test_long_tool_name_is_cut_with_hash() -> None:
    first = tool_name("filesystem", "x" * 100)
    second = tool_name("filesystem", "x" * 99 + "y")
    assert len(first) == NAME_LIMIT and len(second) == NAME_LIMIT
    assert first != second  # 截短后靠哈希区分
    assert first == tool_name("filesystem", "x" * 100)  # 同样的输入同样的名字，缓存才稳


def test_normalize_schema() -> None:
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "properties": {"path": {"type": "string", "title": "Path"}},
        "required": ["path"],
    }
    assert normalize_schema(schema) == {
        "type": "object",
        "properties": {"path": {"type": "string", "title": "Path"}},
        "required": ["path"],
    }
    assert normalize_schema({"type": "object", "additionalProperties": False}) == {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    }
    with pytest.raises(ValueError, match="不是 object"):
        normalize_schema({"type": "string"})


# ------------------------------------------------------------------ wrap_tools


READ = McpTool("read", "读文件", OBJECT, annotations={"readOnlyHint": True})
WRITE = McpTool("write", "写文件", OBJECT, annotations={"destructiveHint": True})
SEND = McpTool("send", "发邮件", OBJECT, annotations={"openWorldHint": True})
PLAIN = McpTool("plain", "", OBJECT, title="普通工具")


def wrapped(cfg: McpServerConfig, *tools: McpTool) -> dict[str, Any]:
    items, _ = wrap_tools("srv", cfg, tools, NullCaller())
    return {item.name: item for item in items}


def test_permissions_follow_annotations() -> None:
    items = wrapped(config(), READ, WRITE, SEND, PLAIN)
    read, write = items["mcp__srv__read"], items["mcp__srv__write"]
    send, plain = items["mcp__srv__send"], items["mcp__srv__plain"]
    assert (read.readonly, read.permission, read.confirm_reason) == (True, "allow", None)
    assert (write.readonly, write.permission) == (False, "ask")
    assert write.confirm_reason == "来自 MCP server srv，server 标注它可能删除或覆盖数据"
    assert (
        send.confirm_reason == "来自 MCP server srv，server 没有标注它是只读的，而且会访问外部系统"
    )
    assert plain.description == "普通工具"  # 没有 description 就用 title


def test_untrusted_annotations_all_ask() -> None:
    items = wrapped(config(trust_annotations=False), READ, SEND)
    read = items["mcp__srv__read"]
    assert (read.readonly, read.permission) == (False, "ask")
    assert read.confirm_reason == "来自 MCP server srv（trust_annotations = false，调用前都要确认）"
    assert "外部系统" not in (items["mcp__srv__send"].confirm_reason or "")


def test_permission_overrides() -> None:
    items = wrapped(config(permissions={"write": "allow", "read": "ask"}), READ, WRITE)
    write, read = items["mcp__srv__write"], items["mcp__srv__read"]
    assert (write.readonly, write.permission) == (False, "allow")  # 免确认，但仍按顺序执行
    assert (read.readonly, read.permission) == (True, "ask")
    assert read.confirm_reason == "来自 MCP server srv，配置要求调用前确认"


def test_filters_and_warnings() -> None:
    broken = McpTool("broken", "", {"type": "array"})
    cfg = config(
        enabled_tools=["read", "write", "broken", "nope"],
        disabled_tools=["write", "gone"],
        permissions={"ghost": "allow"},
    )
    items, warnings = wrap_tools("srv", cfg, [READ, WRITE, SEND, broken], NullCaller())
    assert [item.name for item in items] == ["mcp__srv__read"]
    assert warnings == [
        "srv：配置的 enabled_tools 里有 nope，但 server 没有这个工具",
        "srv：配置的 disabled_tools 里有 gone，但 server 没有这个工具",
        "srv：配置的 permissions 里有 ghost，但 server 没有这个工具",
        "srv：跳过 broken，inputSchema 的 type 是 'array'，不是 object",
    ]


def test_names_colliding_after_cleanup_are_deduplicated() -> None:
    items, _ = wrap_tools(
        "srv", config(), [McpTool("a.b", "", OBJECT), McpTool("a_b", "", OBJECT)], NullCaller()
    )
    first, second = (item.name for item in items)
    assert first == "mcp__srv__a_b"
    assert second.startswith("mcp__srv__a_b_") and second != first


async def test_readonly_mcp_calls_run_in_parallel(tmp_path: Path) -> None:
    class SlowCaller:
        running = 0
        peak = 0

        async def call_tool(self, name: str, arguments: dict, *, timeout: float) -> CallResult:
            SlowCaller.running += 1
            SlowCaller.peak = max(SlowCaller.peak, SlowCaller.running)
            await asyncio.sleep(0.1)
            SlowCaller.running -= 1
            return CallResult("ok", False)

    items, _ = wrap_tools("srv", config(), [READ], SlowCaller())
    registry = ToolRegistry(items)
    ctx = ToolContext(cwd=tmp_path)
    calls = [call("mcp__srv__read", {}, "c1"), call("mcp__srv__read", {}, "c2")]
    batches = [batch async for batch in registry.execute_many(calls, ctx)]
    assert len(batches) == 1 and SlowCaller.peak == 2


# ------------------------------------------------------------------ 接上真的（假）server


@pytest.fixture
async def fake_client() -> AsyncIterator[McpClient]:
    transport = StdioTransport("fake", sys.executable, [str(FAKE_SERVER), "--era", "legacy"])
    async with McpClient(transport) as client:
        yield client


async def registry_for(
    client: McpClient, approver: RecordingApprover, tmp_path: Path, *extra: McpTool
) -> ToolRegistry:
    tools = [*await client.list_tools(), *extra]
    items, _ = wrap_tools("fake", config(), tools, client)
    return ToolRegistry([*builtin_tools(), *items], approver=approver, policy=Policy(tmp_path))


async def test_registry_executes_mcp_tools(fake_client: McpClient, tmp_path: Path) -> None:
    approver = RecordingApprover(allow=True)
    registry = await registry_for(fake_client, approver, tmp_path)
    ctx = ToolContext(cwd=tmp_path)

    echo = await registry.execute(call("mcp__fake__echo", {"text": "hi"}), ctx)
    assert (echo.content, echo.is_error, echo.decision) == ("hi", False, "allow")
    assert approver.requests == []  # 标了只读，不用问

    failed = await registry.execute(call("mcp__fake__fail", {}), ctx)
    assert (failed.content, failed.is_error, failed.decision) == ("错误：出错了", True, "ask")
    assert approver.requests[0].tool_name == "mcp__fake__fail"
    assert approver.requests[0].reason == "来自 MCP server fake，server 没有标注它是只读的"


async def test_registry_denied_mcp_tool(fake_client: McpClient, tmp_path: Path) -> None:
    registry = await registry_for(fake_client, RecordingApprover(allow=False), tmp_path)
    result = await registry.execute(call("mcp__fake__image", {}), ToolContext(cwd=tmp_path))
    assert result.is_error
    assert result.content == "错误：来自 MCP server fake，server 没有标注它是只读的；已被拒绝"


async def test_registry_mcp_errors_become_text(fake_client: McpClient, tmp_path: Path) -> None:
    ghost = McpTool("ghost", "server 其实没有", OBJECT, annotations={"readOnlyHint": True})
    registry = await registry_for(fake_client, RecordingApprover(allow=True), tmp_path, ghost)
    ctx = ToolContext(cwd=tmp_path)
    unknown = await registry.execute(call("mcp__fake__ghost", {}), ctx)
    assert unknown.content == "错误：Unknown tool: ghost（JSON-RPC 错误 -32602）"
    not_object = await registry.execute(call("mcp__fake__echo", "[1, 2]"), ctx)
    assert not_object.content.startswith("错误：参数校验失败")


async def test_agent_loop_uses_mcp_tool(fake_client: McpClient, tmp_path: Path) -> None:
    """完整一轮：模型看到 MCP 原样的 schema，调用 mcp__fake__echo，结果进会话。"""
    registry = await registry_for(fake_client, RecordingApprover(allow=True), tmp_path)
    tool_call = {"id": "call_0", "name": "mcp__fake__echo", "arguments": {"text": "你好"}}
    llm = FakeLLM([{"tool_calls": [tool_call]}, "server 说了你好"])
    agent = Agent(llm, registry, "系统提示", cwd=tmp_path)
    session = Session("s")
    [event async for event in agent.run(session, "让 server 说你好")]

    functions = {item["function"]["name"]: item["function"] for item in llm.requests[0]["tools"]}
    assert "read_file" in functions  # 内置工具还在，MCP 工具排在后面
    assert functions["mcp__fake__echo"]["parameters"] == {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
    tool_message = next(m for m in session.messages if m["role"] == "tool")
    assert tool_message["content"] == "你好"
    assert session.messages[-1]["content"] == "server 说了你好"
