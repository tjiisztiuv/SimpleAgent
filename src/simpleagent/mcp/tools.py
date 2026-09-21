"""把 MCP 接进 SimpleAgent：配置 → server 子进程，MCP 工具 → 注册表里的 Tool。

转成 Tool 之后，agent loop、权限判定、审批器、输出截断全都不用改——模型看到的只是
`tools` 里多了几项，它并不知道 MCP 的存在。

几件要注意的事：

- 工具名加前缀 mcp__<server>__<tool>：filesystem server 自带 read_file / write_file，
  不加前缀就和内置工具撞名；加了前缀，按名字的白名单（sa run --allow）也直接能用。
- schema 原样交给模型，参数只查「是个 JSON 对象」，真正的校验由 server 做（规范要求）。
- 权限来自 server 标的 annotations 加配置。MCP 工具没有 scope，工作目录边界管不到它们：
  server 能碰什么，由 server 自己的配置决定（比如 filesystem server 命令行里给的目录）。
- 子进程只拿到一份环境变量白名单：npx -y 跑的是别人发布的最新版代码，没理由看到
  shell 里 export 的所有密钥。
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from pydantic import RootModel

from simpleagent.config import ENV_FILENAME, McpServerConfig, home_dir, read_env_file
from simpleagent.mcp.client import CallResult, McpClient, McpTool
from simpleagent.mcp.transport import McpError, StdioTransport
from simpleagent.tools.base import Tool, ToolContext, ToolError, ToolFn

# 默认传给 server 的环境变量：能跑起来需要的（PATH、HOME）、中文路径需要的（LANG / LC_*）、
# 联网需要的（代理）。其余一律不传，需要什么在配置里用 env / env_vars 显式加
BASE_ENV = (
    "HOME",
    "LOGNAME",
    "PATH",
    "SHELL",
    "TERM",
    "USER",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    *(f"{name}_proxy" for name in ("http", "https", "all", "no")),
    *(f"{name}_PROXY" for name in ("HTTP", "HTTPS", "ALL", "NO")),
)
# OpenAI 兼容接口对函数名的要求：只能用这些字符，最长 64
NAME_LIMIT = 64
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")


class McpArgs(RootModel[dict[str, Any]]):
    """MCP 工具的参数：只检查是一个 JSON 对象。每个字段对不对由 server 校验。"""


class ToolCaller(Protocol):
    """能调用 MCP 工具的对象。现在是 McpClient；第 4 步换成会自动重启 server 的管理器。"""

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, timeout: float
    ) -> CallResult: ...


# ---------------------------------------------------------------------- 启动


def _expand(text: str) -> str:
    # 只展开开头的 ~。不展开 $VAR：参数会出现在 ps 里，密钥只能走环境变量
    return os.path.expanduser(text) if text.startswith("~") else text


def server_env(
    config: McpServerConfig,
    environ: Mapping[str, str] | None = None,
    dotenv: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """server 子进程的环境变量：白名单 + env + env_vars。env_vars 缺了抛 McpError。"""
    environ = os.environ if environ is None else environ
    env = {name: environ[name] for name in BASE_ENV if name in environ}
    env.update(config.env)
    missing = []
    for name in config.env_vars:
        # 和 profile 的 api_key_env 一个口径：环境变量优先，其次 .env
        if environ.get(name):
            env[name] = environ[name]
            continue
        if dotenv is None:
            dotenv = read_env_file()
        if dotenv.get(name):
            env[name] = dotenv[name]
        else:
            missing.append(name)
    if missing:
        raise McpError(
            f"找不到环境变量 {', '.join(missing)}：先 export，"
            f"或者写到 {home_dir() / ENV_FILENAME}（格式 NAME=值）"
        )
    return env


def client_from_config(name: str, config: McpServerConfig) -> McpClient:
    """按配置造一个还没启动的 McpClient。环境变量缺了在这里就报错，不用等到启动。"""
    transport = StdioTransport(
        name,
        _expand(config.command),
        [_expand(arg) for arg in config.args],
        env=server_env(config),
        cwd=Path(_expand(config.cwd)) if config.cwd else None,
    )
    return McpClient(transport, protocol=config.protocol)


# ---------------------------------------------------------------------- 工具


def tool_name(server: str, remote: str) -> str:
    """MCP 工具在注册表里的名字：mcp__<server>__<tool>，清洗成模型接口认的字符，最长 64。"""
    name = f"mcp__{server}__{_UNSAFE_NAME_CHARS.sub('_', remote)}"
    if len(name) <= NAME_LIMIT:
        return name
    return f"{name[: NAME_LIMIT - 7]}_{_short_hash(remote)}"


def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:6]


def normalize_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """把 MCP 的 inputSchema 整理成各家模型接口都收的样子；不是对象 schema 抛 ValueError。

    只做最少的改动：去掉顶层 $schema（有的兼容实现不认），补上 type 和 properties
    （有的实现拒绝没有 properties 的对象 schema）。其余原样保留。
    """
    kind = schema.get("type", "object")
    if kind != "object":
        raise ValueError(f"inputSchema 的 type 是 {kind!r}，不是 object")
    cleaned = {key: value for key, value in schema.items() if key != "$schema"}
    cleaned["type"] = "object"
    cleaned.setdefault("properties", {})
    return cleaned


def _permission(
    server: str, config: McpServerConfig, tool: McpTool
) -> tuple[bool, str, str | None]:
    """(能否并行, 权限等级, 需要确认时给人看的说明)。"""
    annotations = tool.annotations
    readonly = config.trust_annotations and annotations.get("readOnlyHint") is True
    permission = config.permissions.get(tool.name) or ("allow" if readonly else "ask")
    if permission == "allow":
        return readonly, permission, None
    source = f"来自 MCP server {server}"
    if not config.trust_annotations:
        reason = f"{source}（trust_annotations = false，调用前都要确认）"
    elif readonly:
        reason = f"{source}，配置要求调用前确认"
    elif annotations.get("destructiveHint") is True:
        reason = f"{source}，server 标注它可能删除或覆盖数据"
    else:
        reason = f"{source}，server 没有标注它是只读的"
    if config.trust_annotations and annotations.get("openWorldHint") is True:
        reason += "，而且会访问外部系统"
    return readonly, permission, reason


def _make_fn(caller: ToolCaller, remote: str, timeout: float) -> ToolFn:
    async def run(args: McpArgs, ctx: ToolContext) -> str:
        # ctx.cwd 对 MCP 工具没有意义：server 按它自己的工作目录解析路径
        try:
            result = await caller.call_tool(remote, args.root, timeout=timeout)
        except McpError as e:  # 超时、进程退出、未知工具、需要额外输入……
            raise ToolError(str(e)) from None
        if result.is_error:  # server 报告的执行错误：回给模型，让它自己调整参数
            raise ToolError(result.text)
        return result.text  # 过长由注册表统一截断、落盘

    return run


def wrap_tools(
    server: str,
    config: McpServerConfig,
    tools: Sequence[McpTool],
    caller: ToolCaller,
) -> tuple[list[Tool], list[str]]:
    """把一个 server 的工具转成注册表里的 Tool。返回 (工具, 要给人看的警告)。

    顺序跟 server 给的一样：同样的工具列表得到同样的名字和顺序，前缀缓存才能命中。
    """
    warnings: list[str] = []
    known = {tool.name for tool in tools}
    for field, names in (
        ("enabled_tools", config.enabled_tools or []),
        ("disabled_tools", config.disabled_tools),
        ("permissions", list(config.permissions)),
    ):
        for listed in names:
            if listed not in known:
                warnings.append(f"{server}：配置的 {field} 里有 {listed}，但 server 没有这个工具")

    wrapped: list[Tool] = []
    used: set[str] = set()
    for tool in tools:
        if config.enabled_tools is not None and tool.name not in config.enabled_tools:
            continue
        if tool.name in config.disabled_tools:
            continue
        try:
            parameters = normalize_schema(tool.input_schema)
        except ValueError as e:
            warnings.append(f"{server}：跳过 {tool.name}，{e}")
            continue
        name = tool_name(server, tool.name)
        if name in used:  # 清洗之后撞名（a.b 和 a_b）：后一个带上原名的哈希
            name = f"{name[: NAME_LIMIT - 7]}_{_short_hash(tool.name)}"
        used.add(name)
        readonly, permission, reason = _permission(server, config, tool)
        wrapped.append(
            Tool(
                name=name,
                description=tool.description or tool.title or tool.name,
                args_model=McpArgs,
                fn=_make_fn(caller, tool.name, config.tool_timeout),
                readonly=readonly,
                permission=permission,
                parameters=parameters,
                confirm_reason=reason,
            )
        )
    return wrapped, warnings
