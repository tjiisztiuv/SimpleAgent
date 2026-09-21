"""测试用的假 MCP server：按 --era 讲旧协议、新协议，或者两代都讲。只用标准库。

--era legacy        只懂 initialize 握手（默认）。握手前的其他请求回 -32601
                    （和官方 TS SDK v1 一样）；没收到 notifications/initialized
                    就调工具回 -32600，顺带验证客户端发了它
--era modern        只懂新协议：请求缺 _meta 回 -32602，版本不支持回 -32022；initialize 回 -32601
--era dual          两代都接：先收到 initialize 就按旧协议，否则按新协议
--era silent        和 legacy 一样，但握手前的请求一概不回
--answer-version V  旧协议握手时回这个版本（默认：客户端要的认识就回它，不认识回 2025-11-25）
--supported A,B     新协议支持的版本（默认 2026-07-28）
--no-tools          不声明 tools 能力
--crash-on-probe    收到 server/discover 就退出（有些旧 server 碰到未握手的请求会崩）
--slow-start S      先睡 S 秒再开始读 stdin（模拟 npx 第一次下载）
--record FILE       把收到的每条消息追加写进 FILE，测试据此检查客户端发了什么
"""

import argparse
import base64
import json
import os
import sys
import time

LEGACY_KNOWN = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "fake-mcp", "version": "1.2.3"}
INSTRUCTIONS = "测试用的 server，工具都是假的。"

OBJECT = {"type": "object"}
TOOLS = [
    {
        "name": "echo",
        "description": "原样返回 text",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {"name": "fail", "description": "返回工具执行错误", "inputSchema": OBJECT},
    {"name": "broken", "description": "缺 inputSchema，客户端应该跳过"},
    {"name": "image", "description": "返回一张图", "inputSchema": OBJECT},
    {"name": "structured", "description": "只返回 structuredContent", "inputSchema": OBJECT},
    {"name": "needs_input", "description": "新协议下要求额外输入", "inputSchema": OBJECT},
    {"name": "needs_roots", "description": "新协议下要求 roots 能力", "inputSchema": OBJECT},
    {"name": "ping_me", "description": "先反向 ping 客户端再回答", "inputSchema": OBJECT},
    {"name": "toggle", "description": "发出 list_changed 和一条日志", "inputSchema": OBJECT},
    {"name": "crash", "description": "调用时进程直接退出（测重启）", "inputSchema": OBJECT},
]
PAGE_SIZE = 2


class Server:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.supported = args.supported.split(",")
        # dual 一开始不定：收到 initialize 就锁定旧协议，否则逐条按新协议处理
        self.mode = None if args.era == "dual" else ("modern" if args.era == "modern" else "legacy")
        self.current = self.mode  # 正在处理的这条消息按哪一代回
        self.initialize_answered = False
        self.initialized = False

    # ---------------------------------------------------------------- 收发

    def send(self, message: dict) -> None:
        sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def read(self) -> dict | None:
        line = sys.stdin.readline()
        if not line:
            return None
        if self.args.record:
            with open(self.args.record, "a", encoding="utf-8") as f:
                f.write(line)
        return json.loads(line)

    def reply(self, message: dict, result: dict) -> None:
        if self.current == "modern":
            result = {
                "resultType": "complete",
                **result,
                "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO},
            }
        self.send({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def error(self, message: dict, code: int, text: str, data: object = None) -> None:
        error: dict = {"code": code, "message": text}
        if data is not None:
            error["data"] = data
        self.send({"jsonrpc": "2.0", "id": message["id"], "error": error})

    def capabilities(self) -> dict:
        return {} if self.args.no_tools else {"tools": {"listChanged": True}}

    # ---------------------------------------------------------------- 主循环

    def run(self) -> None:
        time.sleep(self.args.slow_start)
        while (message := self.read()) is not None:
            method = message.get("method")
            if method is None or "id" not in message:
                if method == "notifications/initialized":
                    self.initialized = True
                continue
            if method == "server/discover" and self.args.crash_on_probe:
                sys.stderr.write("unexpected method server/discover before initialize\n")
                sys.stderr.flush()
                sys.exit(4)
            if self.args.era == "dual" and method == "initialize":
                self.mode = "legacy"
            self.current = self.mode or "modern"
            if self.current == "legacy":
                self.handle_legacy(message)
            else:
                self.handle_modern(message)

    def handle_legacy(self, message: dict) -> None:
        method = message["method"]
        if method == "initialize":
            requested = (message.get("params") or {}).get("protocolVersion")
            answer = self.args.answer_version or (
                requested if requested in LEGACY_KNOWN else LEGACY_KNOWN[0]
            )
            self.initialize_answered = True
            self.reply(
                message,
                {
                    "protocolVersion": answer,
                    "capabilities": self.capabilities(),
                    "serverInfo": SERVER_INFO,
                    "instructions": INSTRUCTIONS,
                },
            )
            return
        if not self.initialize_answered:
            if self.args.era != "silent":
                self.error(message, -32601, "Method not found")
            return
        if not self.initialized:
            self.error(message, -32600, "没收到 notifications/initialized")
            return
        self.handle_common(message)

    def handle_modern(self, message: dict) -> None:
        params = message.get("params") or {}
        meta = params.get("_meta") or {}
        requested = meta.get("io.modelcontextprotocol/protocolVersion")
        if requested is None and message["method"] == "initialize":
            # 和官方 TS SDK v2 的 legacy: 'reject' 一样：旧式开场回 -32022，列出支持的新版本
            data = {"supported": self.supported, "requested": params.get("protocolVersion")}
            self.error(message, -32022, "Unsupported protocol version", data)
            return
        if requested is None or "io.modelcontextprotocol/clientCapabilities" not in meta:
            self.error(message, -32602, "缺少 _meta 里的协议字段")
            return
        if requested not in self.supported:
            data = {"supported": self.supported, "requested": requested}
            self.error(message, -32022, "Unsupported protocol version", data)
            return
        method = message["method"]
        if method == "server/discover":
            self.reply(
                message,
                {
                    "supportedVersions": self.supported,
                    "capabilities": self.capabilities(),
                    "instructions": INSTRUCTIONS,
                },
            )
        elif method == "initialize":
            self.error(message, -32601, "Method not found")
        else:
            self.handle_common(message)

    # ---------------------------------------------------------------- 两代共用

    def handle_common(self, message: dict) -> None:
        method = message["method"]
        params = message.get("params") or {}
        if method == "tools/list":
            start = int(params.get("cursor") or 0)
            result: dict = {"tools": TOOLS[start : start + PAGE_SIZE]}
            if start + PAGE_SIZE < len(TOOLS):
                result["nextCursor"] = str(start + PAGE_SIZE)
            self.reply(message, result)
        elif method == "tools/call":
            self.call_tool(message, params.get("name"), params.get("arguments") or {})
        else:
            self.error(message, -32601, "Method not found")

    def call_tool(self, message: dict, name: str, arguments: dict) -> None:
        def text(value: str, **extra: object) -> None:
            self.reply(message, {"content": [{"type": "text", "text": value}], **extra})

        if name == "echo":
            text(arguments["text"])
        elif name == "fail":
            text("出错了", isError=True)
        elif name == "image":
            data = base64.b64encode(b"\x89PNG" + b"\0" * 2044).decode()
            self.reply(
                message, {"content": [{"type": "image", "data": data, "mimeType": "image/png"}]}
            )
        elif name == "structured":
            self.reply(message, {"content": [], "structuredContent": {"temp": 22.5}})
        elif name == "needs_input" and self.current == "modern":
            request = {"method": "elicitation/create", "params": {"message": "你的名字？"}}
            self.reply(
                message,
                {"resultType": "input_required", "inputRequests": {"login": request}},
            )
        elif name == "needs_roots" and self.current == "modern":
            data = {"requiredCapabilities": ["roots"]}
            self.error(message, -32021, "Missing required client capability", data)
        elif name == "ping_me":
            self.send({"jsonrpc": "2.0", "id": "ping-1", "method": "ping"})
            while (answer := self.read()) is not None and answer.get("id") != "ping-1":
                pass
            text(json.dumps((answer or {}).get("result"), ensure_ascii=False))
        elif name == "toggle":
            self.send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
            log = {"level": "warning", "logger": "fake", "data": "工具列表变了"}
            self.send({"jsonrpc": "2.0", "method": "notifications/message", "params": log})
            text("ok")
        elif name == "crash":
            sys.stderr.write("fake server: crash requested\n")
            sys.stderr.flush()
            os._exit(3)
        else:
            self.error(message, -32602, f"Unknown tool: {name}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--era", choices=["legacy", "modern", "dual", "silent"], default="legacy")
    parser.add_argument("--answer-version")
    parser.add_argument("--supported", default="2026-07-28")
    parser.add_argument("--no-tools", action="store_true")
    parser.add_argument("--crash-on-probe", action="store_true")
    parser.add_argument("--slow-start", type=float, default=0.0)
    parser.add_argument("--record")
    Server(parser.parse_args()).run()


if __name__ == "__main__":
    main()
