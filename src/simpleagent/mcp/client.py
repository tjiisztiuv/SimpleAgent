"""MCP 协议会话：连上一个 server，列出它的工具，调用工具。

MCP 在 2026-07-28 版有一次代际变化（规范里叫 legacy / modern）：

- 旧协议（2025-11-25 及更早）：先 initialize 握手协商版本，再发 notifications/initialized，
  之后整条连接就是一个会话；server 也能反过来发请求（ping 等）。
- 新协议（2026-07-28）：没有握手，每个请求在 params._meta 里自带版本和客户端能力，
  server 无状态、不发请求；可选的 server/discover 用来问它支持哪些版本、有哪些能力。

连接时先发 server/discover 探测：回了就走新协议，回别的错误或者不回，就退回 initialize。
规范对「两代都支持的客户端」就是这么要求的。两代的差别只在「怎么开场」和「请求要不要带
_meta」，都收在这个类里，上层只用 list_tools / call_tool。

tools/list 和 tools/call 的格式两代基本一样，所以旧协议这几个版本都能直接用。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Literal

from simpleagent.mcp.transport import (
    METHOD_NOT_FOUND,
    McpError,
    McpTimeout,
    RpcError,
    StdioTransport,
)

# 新的在前：挑版本时从前往后找第一个两边都支持的
MODERN_VERSIONS = ("2026-07-28",)
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
# 探测最多等多久。规范说不回应就当旧 server；等太久，不理未握手请求的旧 server 每次启动都要白等
PROBE_TIMEOUT = 10.0
MAX_PAGES = 100  # tools/list 最多翻这么多页，防止 server 的 cursor 转圈
UNSUPPORTED_PROTOCOL_VERSION = -32022
MISSING_CLIENT_CAPABILITY = -32021

Protocol = Literal["auto", "modern", "legacy"]
Era = Literal["modern", "legacy"]


def _client_version() -> str:
    try:
        return version("simpleagent")
    except PackageNotFoundError:
        return "unknown"


CLIENT_INFO = {"name": "simpleagent", "version": _client_version()}


@dataclass(frozen=True)
class ServerInfo:
    """连上之后知道的东西。name / version 是 server 自报的，只用于显示，不能拿来做判断。"""

    era: Era
    protocol_version: str
    name: str
    version: str
    capabilities: dict[str, Any]
    instructions: str | None = None


@dataclass(frozen=True)
class McpTool:
    """server 提供的一个工具。annotations（readOnlyHint 等）是 server 自己标的，规范说不可信。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    title: str | None = None
    annotations: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CallResult:
    text: str  # 给模型看的文本，见 render_content
    is_error: bool  # server 报告的工具执行错误（isError），模型可以据此改参数重试


# ---------------------------------------------------------------------- 纯函数


def _is_npx(command: str) -> bool:
    return Path(command).name in ("npx", "npx.cmd")


def choose_version(supported: Iterable[str]) -> str | None:
    """两边都支持的最新版本：新协议优先，其次旧协议；没有交集返回 None。"""
    offered = {item for item in supported if isinstance(item, str)}
    for candidate in (*MODERN_VERSIONS, *LEGACY_VERSIONS):
        if candidate in offered:
            return candidate
    return None


def _approx_size(data: Any) -> str:
    """base64 字符串解码后大约多大。"""
    if not isinstance(data, str):
        return "未知大小"
    size = len(data) * 3 // 4 - data[-2:].count("=")
    return f"{size} 字节" if size < 1024 else f"{size / 1024:.1f} KB"


def _render_block(block: Any) -> str:
    if not isinstance(block, dict):
        return f"[无法识别的内容：{str(block)[:100]}]"
    kind = block.get("type")
    if kind == "text":
        return str(block.get("text", ""))
    if kind in ("image", "audio"):
        # Chat Completions 的 tool 消息只能放文本；留个占位，模型才知道工具返回过东西
        label = "图片" if kind == "image" else "音频"
        mime = block.get("mimeType") or "未知类型"
        return f"[{label} {mime}，约 {_approx_size(block.get('data'))}，未展示给模型]"
    if kind == "resource_link":
        uri = block.get("uri", "")
        line = f"[资源链接] {block.get('name') or uri} <{uri}>"
        if block.get("mimeType"):
            line += f"（{block['mimeType']}）"
        if block.get("description"):
            line += f"：{block['description']}"
        return line
    if kind == "resource":
        resource = block.get("resource")
        resource = resource if isinstance(resource, dict) else {}
        uri = resource.get("uri", "")
        if isinstance(resource.get("text"), str):
            return f"[资源 {uri}]\n{resource['text']}"
        mime = resource.get("mimeType") or "二进制"
        return f"[资源 {uri}：{mime}，约 {_approx_size(resource.get('blob'))}，未展示给模型]"
    return f"[未知内容类型 {kind}，已忽略]"


def render_content(result: Mapping[str, Any]) -> str:
    """tools/call 的结果 → 给模型看的文本。截断不在这里做，交给注册表统一处理。"""
    blocks = result.get("content")
    parts = [_render_block(block) for block in blocks] if isinstance(blocks, list) else []
    parts = [part for part in parts if part]
    structured = result.get("structuredContent")
    if not parts and structured is not None:
        # 规范要求有 structuredContent 时也给一份 JSON 文本；server 没给才自己序列化，
        # 两份都有时只用 content，免得同样的内容占两遍 token
        parts.append(json.dumps(structured, ensure_ascii=False, indent=2))
    return "\n".join(parts) if parts else "(无输出)"


def _parse_tool(raw: Any) -> McpTool:
    """tools/list 里的一项 → McpTool。格式不对抛 ValueError，由调用方跳过这一个。"""
    if not isinstance(raw, dict):
        raise ValueError(f"不是对象：{str(raw)[:100]}")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"缺少 name：{json.dumps(raw, ensure_ascii=False)[:100]}")
    schema = raw.get("inputSchema")
    if not isinstance(schema, dict):
        raise ValueError(f"{name} 的 inputSchema 不是对象")
    description = raw.get("description")
    title = raw.get("title")
    annotations = raw.get("annotations")
    return McpTool(
        name=name,
        description=description if isinstance(description, str) else "",
        input_schema=schema,
        title=title if isinstance(title, str) else None,
        annotations=annotations if isinstance(annotations, dict) else {},
    )


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


# ---------------------------------------------------------------------- 会话


class McpClient:
    """一个 MCP server 的协议会话。connect() 负责启动子进程；连接失败后这个对象就不能再用了。"""

    def __init__(self, transport: StdioTransport, *, protocol: Protocol = "auto") -> None:
        self.transport = transport
        self.protocol = protocol
        self.info: ServerInfo | None = None
        # 收到过 notifications/tools/list_changed。只记标记，不在会话中途刷新工具列表：
        # 工具一变前缀缓存就失效，模型刚看到的工具也可能消失。什么时候刷新由上层决定
        self.tools_changed = False
        self._stage = "start"  # 连接进行到哪一步，出错时给出对应的提示
        self._probe_timed_out = False
        transport.on_request = self._on_server_request
        transport.on_notification = self._on_notification

    @property
    def name(self) -> str:
        return self.transport.name

    async def __aenter__(self) -> McpClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self.transport.close()

    # ------------------------------------------------------------------ 连接

    async def connect(self, timeout: float = 60.0) -> ServerInfo:
        """启动 server 并完成开场，timeout 是整个过程的总时限。

        失败时关掉子进程，抛 McpError，信息里附上 stderr 末尾（启动失败的原因通常在那里）。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        def remaining() -> float:
            return max(deadline - loop.time(), 0.1)

        try:
            self._stage = "start"
            await self.transport.start()
            info = None
            if self.protocol != "legacy":
                self._stage = "discover"
                # auto 档只探测一小会儿，没回应就按规范退回旧协议；modern 档不退回，可以等满
                wait = min(PROBE_TIMEOUT, remaining()) if self.protocol == "auto" else remaining()
                info = await self._discover(wait)
            if info is None:
                self._stage = "initialize"
                info = await self._initialize(remaining())
        except McpError as e:
            await self.transport.close()
            raise McpError(self._explain(e)) from None
        self.info = info
        return info

    def _meta(self, protocol_version: str) -> dict[str, Any]:
        """新协议每个请求都要带的 _meta。"""
        return {
            "io.modelcontextprotocol/protocolVersion": protocol_version,
            "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
            # 不声明 roots / sampling / elicitation：见 _initialize
            "io.modelcontextprotocol/clientCapabilities": {},
        }

    async def _discover(self, timeout: float) -> ServerInfo | None:
        """用 server/discover 探测。返回 None 表示「当旧 server 处理，改走 initialize」。"""
        fallback = self.protocol == "auto"
        protocol_version = MODERN_VERSIONS[0]
        tried: set[str] = set()
        while True:
            tried.add(protocol_version)
            try:
                result = await self.transport.request(
                    "server/discover", {"_meta": self._meta(protocol_version)}, timeout=timeout
                )
            except McpTimeout:
                if not fallback:
                    raise
                self._probe_timed_out = True
                return None
            except RpcError as e:
                # 规范：只有认得出的新协议错误才说明它是新 server；其余错误码五花八门
                # （-32601、-32602……），也可能根本不回，一律当旧 server，不能只认某一个码
                if e.code != UNSUPPORTED_PROTOCOL_VERSION:
                    if fallback:
                        return None
                    raise McpError(f"server 不支持新协议（server/discover 返回 {e}）") from None
                data = _as_dict(e.data)
                supported = data.get("supported")
                supported = supported if isinstance(supported, list) else []
                choice = choose_version(supported)
                if choice is None:
                    raise McpError(self._no_common_version(supported)) from None
                if choice in LEGACY_VERSIONS:
                    if fallback:
                        return None
                    raise McpError(
                        f'server 只支持旧协议（{choice}），而当前配置是 protocol = "modern"'
                    ) from None
                if choice in tried:
                    raise McpError(f"server 拒绝了协议版本 {choice}，却又说支持它") from None
                protocol_version = choice
                continue
            server = _as_dict(
                _as_dict(result.get("_meta")).get("io.modelcontextprotocol/serverInfo")
            )
            return ServerInfo(
                era="modern",
                protocol_version=protocol_version,
                name=str(server.get("name") or "?"),
                version=str(server.get("version") or "?"),
                capabilities=_as_dict(result.get("capabilities")),
                instructions=_as_text(result.get("instructions")),
            )

    async def _initialize(self, timeout: float) -> ServerInfo:
        """旧协议的握手：initialize 协商版本，再发 notifications/initialized。"""
        result = await self.transport.request(
            "initialize",
            {
                "protocolVersion": LEGACY_VERSIONS[0],
                # 不声明 roots：filesystem server 收到 roots 会替换掉命令行里给的允许目录，
                # 配置里写的目录反而不算数。sampling / elicitation 要能问用户的界面，以后再做
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
            timeout=timeout,
        )
        protocol_version = result.get("protocolVersion")
        if protocol_version not in LEGACY_VERSIONS:
            # server 提了一个我们不认识的版本：规范说客户端应当断开
            raise McpError(
                f"server 要用协议版本 {protocol_version}，"
                f"SimpleAgent 的旧协议只支持 {', '.join(LEGACY_VERSIONS)}"
            )
        await self.transport.notify("notifications/initialized")
        server = _as_dict(result.get("serverInfo"))
        return ServerInfo(
            era="legacy",
            protocol_version=protocol_version,
            name=str(server.get("name") or "?"),
            version=str(server.get("version") or "?"),
            capabilities=_as_dict(result.get("capabilities")),
            instructions=_as_text(result.get("instructions")),
        )

    @staticmethod
    def _no_common_version(supported: list[Any]) -> str:
        ours = ", ".join((*MODERN_VERSIONS, *LEGACY_VERSIONS))
        theirs = ", ".join(map(str, supported)) or "（没说）"
        return f"没有共同的协议版本：server 支持 {theirs}；SimpleAgent 支持 {ours}"

    def _explain(self, error: McpError) -> str:
        """把连接失败的原因写成人话，附上 stderr 和针对性的提示。"""
        detail = str(error)
        for prefix in (
            f"启动 MCP server {self.name} 失败：",
            f"MCP server {self.name}：",
            f"MCP server {self.name} ",
        ):
            detail = detail.removeprefix(prefix)
        message = f"连接 MCP server {self.name} 失败：{detail}"
        tail = self.transport.stderr_tail(5)
        if tail and tail not in message:
            message += f"\n最近的 stderr：\n{tail}"
        if isinstance(error, McpTimeout) and not tail and _is_npx(self.transport.command):
            # npx 不带版本号时每次启动都要联网查最新版，连不上 npm 仓库就一直挂着、一行输出都没有；
            # 这时「再试一次」没用
            message += (
                "\n提示：server 一行输出都没有，多半是 npx 卡在联网查 npm 仓库"
                "（每次启动都会查最新版，网络或代理不通就一直挂着）。"
                '在 args 最前面加 "--prefer-offline"，本地有缓存就不联网；'
                "或者 npm install -g 装好，把 command 换成装好的命令。"
            )
        elif (
            self._stage == "discover"
            and self.protocol == "auto"
            and not isinstance(error, RpcError | McpTimeout)
        ):
            message += (
                "\n提示：有些旧 server 收到握手之前的请求就会出错；"
                '在配置里写 protocol = "legacy" 可以跳过探测。'
            )
        elif self._stage == "initialize" and self._probe_timed_out:
            message += (
                f"\n提示：server/discover {PROBE_TIMEOUT:g} 秒内没有回应，已按旧协议重试。"
                "如果是 server 启动太慢（比如 npx 第一次下载），再试一次就好；"
                '确定它只讲新协议的话，在配置里写 protocol = "modern"。'
            )
        elif (
            self._stage == "initialize"
            and isinstance(error, RpcError)
            and error.code == UNSUPPORTED_PROTOCOL_VERSION
        ):
            # 只讲新协议的 server 对 initialize 回 -32022（官方 TS SDK v2 的 legacy: 'reject'）
            message += (
                '\n提示：这个 server 只讲新协议；把配置里的 protocol 改成 "auto" 或 "modern"。'
            )
        return message

    # ------------------------------------------------------------------ 工具

    async def list_tools(self, timeout: float = 60.0) -> list[McpTool]:
        """列出全部工具（自动翻页）。格式不对的工具跳过，原因记进诊断缓冲。"""
        info = self._connected()
        if "tools" not in info.capabilities:
            return []
        self.tools_changed = False
        tools: list[McpTool] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            result = await self._request(
                "tools/list", {"cursor": cursor} if cursor else {}, timeout=timeout
            )
            items = result.get("tools")
            for raw in items if isinstance(items, list) else []:
                try:
                    tools.append(_parse_tool(raw))
                except ValueError as e:
                    self.transport.note(f"[sa] 跳过格式不对的工具：{e}")
            cursor = result.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                return tools
        self.transport.note(f"[sa] tools/list 翻了 {MAX_PAGES} 页还没完，后面的不要了")
        return tools

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, timeout: float
    ) -> CallResult:
        """调用一个工具。协议层面的失败（未知工具、超时、进程退出）抛 McpError。"""
        self._connected()
        result = await self._request(
            "tools/call", {"name": name, "arguments": arguments}, timeout=timeout
        )
        return CallResult(render_content(result), is_error=result.get("isError") is True)

    def _connected(self) -> ServerInfo:
        if self.info is None:
            raise McpError(f"MCP server {self.name} 还没有连上")
        return self.info

    async def _request(
        self, method: str, params: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        """连上之后的请求入口：新协议自动带上 _meta，两代的结果按同一套规则解读。"""
        info = self._connected()
        if info.era == "modern":
            params = {**params, "_meta": self._meta(info.protocol_version)}
        try:
            result = await self.transport.request(method, params, timeout=timeout)
        except RpcError as e:
            if e.code == MISSING_CLIENT_CAPABILITY:
                wanted = _as_dict(e.data).get("requiredCapabilities") or e.data
                raise McpError(f"{method} 需要客户端能力 {wanted}，SimpleAgent 暂不支持") from None
            raise
        # 旧 server 的结果没有 resultType，规范要求按 complete 处理
        kind = result.get("resultType", "complete")
        if kind == "input_required":
            requests = _as_dict(result.get("inputRequests")).values()
            wanted = sorted({str(_as_dict(r).get("method", "?")) for r in requests})
            raise McpError(
                f"{method} 需要额外输入（{'、'.join(wanted) or '未说明'}），"
                "SimpleAgent 暂不支持这种多轮请求"
            )
        if kind != "complete":
            raise McpError(f"{method} 返回了不认识的 resultType：{kind}")
        return result

    # ------------------------------------------------------------------ server 发来的消息

    async def _on_server_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """只有旧协议的 server 会发请求。ping 必须回；其余能力都没声明，一律「不支持」。"""
        if method == "ping":
            return {}
        raise RpcError(METHOD_NOT_FOUND, f"SimpleAgent 不支持 {method}")

    def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "notifications/tools/list_changed":
            self.tools_changed = True
        elif method == "notifications/message":  # server 的日志：和 stderr 记在一起
            level = params.get("level", "info")
            logger = params.get("logger")
            data = params.get("data")
            text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
            self.transport.note(f"[{level}{f' {logger}' if logger else ''}] {text}")
        # notifications/progress 等暂不处理：以后接 ToolContext 的进度上报
