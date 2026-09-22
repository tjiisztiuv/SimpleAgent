"""懒启动（start = "lazy"）用的工具清单缓存：<数据目录>/mcp_cache/<名字>.json。

懒启动要解决的矛盾：模型在第一次请求之前就得知道有哪些工具，会话里工具列表也不能变
（前缀缓存），可 server 没启动就拿不到工具列表。办法是记住上次连上时它给的工具清单：
下次启动直接拿缓存登记工具，不拉起进程，等模型第一次调用它的工具时才真正启动。

- 存的是 server 原始的工具清单（过滤、权限之前）：改 enabled_tools / permissions 不用作废缓存。
- 带一个启动参数的指纹，command / args / env / cwd / protocol 变了缓存就作废——
  那可能已经是另一个 server 了。不含 env_vars 的值：那是密钥，也不该落盘。
- 读不出来（没有、损坏、版本不对、指纹不对）一律当没有缓存，照常启动一次再写。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from simpleagent.config import McpServerConfig, home_dir
from simpleagent.mcp.client import McpTool, ServerInfo, _parse_tool

CACHE_DIRNAME = "mcp_cache"
CACHE_VERSION = 1


@dataclass(frozen=True)
class CachedServer:
    info: ServerInfo
    tools: list[McpTool]


def cache_path(name: str) -> Path:
    return home_dir() / CACHE_DIRNAME / f"{name}.json"


def fingerprint(config: McpServerConfig) -> str:
    """决定「连的还是不是同一个 server」的启动参数。"""
    launch = {
        "command": config.command,
        "args": config.args,
        "env": config.env,
        "env_vars": config.env_vars,  # 只有变量名
        "cwd": config.cwd,
        "protocol": config.protocol,
    }
    text = json.dumps(launch, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def load_cached(name: str, config: McpServerConfig) -> CachedServer | None:
    """读缓存；没有或用不了都返回 None。"""
    try:
        data = json.loads(cache_path(name).read_text(encoding="utf-8"))
        if data.get("version") != CACHE_VERSION or data.get("fingerprint") != fingerprint(config):
            return None
        info = ServerInfo(**data["server"])
        tools = [_parse_tool(raw) for raw in data["tools"]]
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
    return CachedServer(info, tools)


def save_cached(name: str, config: McpServerConfig, info: ServerInfo, tools: list[McpTool]) -> None:
    """写缓存：先写临时文件再改名，写到一半被打断也不会留下半个文件。失败抛 OSError。"""
    data = {
        "version": CACHE_VERSION,
        "fingerprint": fingerprint(config),
        "server": dataclasses.asdict(info),
        "tools": [_tool_dict(tool) for tool in tools],
    }
    path = cache_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _tool_dict(tool: McpTool) -> dict[str, Any]:
    """McpTool → tools/list 里的原始形状，读回来时和 server 给的走同一个解析函数。"""
    raw: dict[str, Any] = {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.input_schema,
        "annotations": dict(tool.annotations),
    }
    if tool.title is not None:
        raw["title"] = tool.title
    return raw
