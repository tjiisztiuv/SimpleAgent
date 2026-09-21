"""MCP 客户端（M5）：让 agent 用上外部 MCP server 提供的工具。

分层：transport.py 管子进程和按行收发 JSON-RPC；client.py 管协议会话（新旧两代的开场、
tools/list、tools/call）；tools.py 把配置变成 server 子进程、把 MCP 工具包装成注册表里的
Tool。多 server 的管理在后续步骤加入。
"""

from simpleagent.mcp.client import CallResult, McpClient, McpTool, ServerInfo
from simpleagent.mcp.tools import client_from_config, wrap_tools
from simpleagent.mcp.transport import McpError, McpTimeout, RpcError, StdioTransport

__all__ = [
    "CallResult",
    "McpClient",
    "McpError",
    "McpTimeout",
    "McpTool",
    "RpcError",
    "ServerInfo",
    "StdioTransport",
    "client_from_config",
    "wrap_tools",
]
