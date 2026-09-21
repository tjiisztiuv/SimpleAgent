"""MCP 的 stdio 传输层：启动 server 子进程，在管道上按行收发 JSON-RPC 消息。

这一层只管「消息怎么可靠地到达」，不认识 initialize、tools/call 这些方法——
协议语义在 client.py（M5 第 2 步）。分开之后两边各自好测；以后加 HTTP 传输，
上层照样只用 request / notify / close。

几个容易踩的坑，都在这里处理掉：

- 一根管道上同时有多个请求在路上（注册表会并行执行只读工具），响应可能乱序：
  每个请求带自增 id，登记在 id → Future 表里，由常驻的读循环按 id 交回。
- stderr 必须一直读走：管道缓冲只有几十 KB，写满后 server 会卡在写日志上，看起来像挂了。
- asyncio 的 readline 默认一行最多 64KB，一个大文件的内容就超了：启动时把上限调大。
- 子进程自成进程组：REPL 里按 Ctrl+C 不会顺带打死 server；关闭时能连同 npx 拉起的
  node 一起清掉。和 bash 工具是同一招。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from pathlib import Path
from typing import Any

# 单条消息的上限。asyncio 默认 64KB，server 返回一个大文件就会超；再大就当连接坏了
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
STDERR_KEEP_LINES = 200  # server 的 stderr 只留最后这么多行，报错时附上
STDERR_LINE_CHARS = 500
CLOSE_GRACE_SECONDS = 2.0  # 关 stdin 后等多久再 SIGTERM，SIGTERM 后再等多久 SIGKILL
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603


class McpError(Exception):
    """MCP 调用失败：超时、进程退出、找不到命令……消息写给人和模型看。"""


class RpcError(McpError):
    """server 回了 JSON-RPC error。保留 code：上层要靠它区分「版本不支持」「没有这个方法」。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"{message}（JSON-RPC 错误 {code}）")
        self.code = code
        self.message = message
        self.data = data


class McpTimeout(McpError):
    """请求在时限内没有响应。单独一类：探测时「没回应」和「进程挂了」要分开处理。"""


NotificationHandler = Callable[[str, dict[str, Any]], None]
# server 发来的请求（旧协议的 ping 等）：返回 result；抛 RpcError 就回对应的错误
RequestHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class StdioTransport:
    """一个 MCP server 子进程，以及它上面的 JSON-RPC 收发。"""

    def __init__(
        self,
        name: str,
        command: str,
        args: Sequence[str] = (),
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        on_notification: NotificationHandler | None = None,
        on_request: RequestHandler | None = None,
    ) -> None:
        self.name = name  # 配置里的 server 名，只用在报错信息里
        self.command = command
        self.args = list(args)
        # None 表示继承当前进程的环境；该给 server 看哪些变量由上层（第 3 步）决定
        self.env = dict(env) if env is not None else None
        self.cwd = cwd
        self.on_notification = on_notification
        self.on_request = on_request
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0
        self._stderr: deque[str] = deque(maxlen=STDERR_KEEP_LINES)
        self._tasks: set[asyncio.Task[Any]] = set()  # 读循环、stderr 排空、回答 server 的请求
        self._stderr_task: asyncio.Task[None] | None = None
        # 连接不可用的原因（完整的报错信息）；None 表示还能用。只记第一次
        self._failure: str | None = None

    async def __aenter__(self) -> StdioTransport:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------------ 状态

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._failure is None

    @property
    def returncode(self) -> int | None:
        """进程的退出码；还在跑或没启动过是 None。负数表示被信号杀掉（-15 = SIGTERM）。"""
        return None if self._proc is None else self._proc.returncode

    def stderr_tail(self, lines: int = 20) -> str:
        """server 最近的诊断输出：stderr、stdout 上混进来的非 JSON 行、上层记的日志通知。"""
        return "\n".join(list(self._stderr)[-lines:])

    def note(self, text: str) -> None:
        """往诊断缓冲里记一行（比如 server 通过 notifications/message 发来的日志）。"""
        self._keep(text.encode("utf-8"))

    # ------------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        if self._proc is not None:
            raise McpError(f"MCP server {self.name} 已经启动过了")
        if self.cwd is not None and not Path(self.cwd).is_dir():
            raise McpError(f"启动 MCP server {self.name} 失败：工作目录不存在 {self.cwd}")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
                cwd=self.cwd,
                limit=MAX_MESSAGE_BYTES,
                start_new_session=True,  # 自成进程组：见模块说明
            )
        except FileNotFoundError:
            raise McpError(
                f"启动 MCP server {self.name} 失败：找不到命令 {self.command}"
                "（没装，或者不在 PATH 里）"
            ) from None
        except OSError as e:
            raise McpError(f"启动 MCP server {self.name} 失败：{e}") from None
        self._stderr_task = self._spawn(self._drain_stderr())
        self._spawn(self._read_stdout())

    async def close(self, grace: float = CLOSE_GRACE_SECONDS) -> None:
        """按规范的顺序关掉 server：关 stdin → 等 → SIGTERM → 等 → SIGKILL。可以重复调用。"""
        proc = self._proc
        if proc is None:
            return
        # 先占住原因：之后读循环看到 EOF，不会再当成「意外退出」
        self._fail(f"MCP server {self.name} 已关闭")
        if proc.returncode is None:
            assert proc.stdin is not None
            with contextlib.suppress(OSError):  # stdin 可能已经断了
                proc.stdin.close()  # 规范首选的退出信号，也是唯一跨平台的
            if not await self._wait_exit(grace):
                self._kill_group(signal.SIGTERM)
                if not await self._wait_exit(grace):
                    self._kill_group(signal.SIGKILL)
                    await proc.wait()
        # 组长退了，组里可能还剩它拉起的子进程（npx → node）：一并清掉
        self._kill_group(signal.SIGKILL)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _wait_exit(self, timeout: float) -> bool:
        assert self._proc is not None
        try:
            await asyncio.wait_for(self._proc.wait(), timeout)
        except TimeoutError:
            return False
        return True

    def _kill_group(self, sig: int) -> None:
        assert self._proc is not None
        try:
            os.killpg(self._proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass  # 已经全部退出了

    # ------------------------------------------------------------------ 发送

    async def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float
    ) -> dict[str, Any]:
        """发一个请求并等它的响应。失败抛 McpError；server 回了 error 抛 RpcError。"""
        self._check_alive()
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            await self._send(message)
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            self._cancel_remote(request_id, f"超过 {timeout:g}s 没有响应")
            raise McpTimeout(
                f"MCP server {self.name}：{method} 超过 {timeout:g}s 没有响应"
            ) from None
        except asyncio.CancelledError:
            # Ctrl+C / agent.cancel()：告诉 server 别做了。同步写一行，不 await——
            # 取消路径上再挂起，调用方就收不到 CancelledError 了
            self._cancel_remote(request_id, "用户中断")
            raise
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """发一条通知：没有 id，对方不回。"""
        self._check_alive()
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._send(message)

    def _check_alive(self) -> None:
        if self._proc is None:
            raise McpError(f"MCP server {self.name} 还没有启动")
        if self._failure is not None:
            raise McpError(self._failure)

    def _write(self, message: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        # 不带 indent 的 json.dumps 会把字符串里的换行转义成 \n：一条消息保证只占一行
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        self._proc.stdin.write(line.encode("utf-8"))

    async def _send(self, message: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        try:
            self._write(message)
            await self._proc.stdin.drain()
        except OSError as e:  # BrokenPipe / ConnectionReset：server 已经没了
            raise McpError(f"MCP server {self.name} 写入失败：{e}") from None

    def _cancel_remote(self, request_id: int, reason: str) -> None:
        """告诉 server 这个请求不要了（规范要求）。尽力而为：连接已断就算了。"""
        if self._failure is not None:
            return
        with contextlib.suppress(OSError):
            self._write(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": request_id, "reason": reason},
                }
            )

    # ------------------------------------------------------------------ 接收

    async def _read_stdout(self) -> None:
        """常驻的读循环：响应、通知、server 的请求混在同一个流里，统一在这里分发。"""
        reason = await self._read_until_broken()
        if self._failure is not None:
            return  # close() 在关它，不是意外
        # 等 stderr 读完最后几行：崩溃原因通常就在里面
        if self._stderr_task is not None:
            await asyncio.wait({self._stderr_task}, timeout=0.5)
        message = f"MCP server {self.name} {reason}"
        if tail := self.stderr_tail(5):
            message += f"\n最近的 stderr：\n{tail}"
        self._fail(message)

    async def _read_until_broken(self) -> str:
        """一行一行读 stdout 并分发，直到读不下去；返回读不下去的原因。"""
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            try:
                line = await self._proc.stdout.readline()
            except ValueError:  # 单行超过 MAX_MESSAGE_BYTES，asyncio 抛的是 ValueError
                self._kill_group(signal.SIGKILL)
                return f"发来一条超过 {MAX_MESSAGE_BYTES:,} 字节的消息，连接已断开"
            if not line:
                break
            if line.strip():
                self._dispatch(line)
        # stdout 到头基本等于进程退出了；个别 server 只关 stdout 不退出，等一会儿就强杀
        try:
            code = await asyncio.wait_for(self._proc.wait(), CLOSE_GRACE_SECONDS)
        except TimeoutError:
            self._kill_group(signal.SIGKILL)
            return "关闭了 stdout 但没有退出，已强制结束"
        return f"意外退出（退出码 {code}）"

    def _dispatch(self, raw: bytes) -> None:
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            message = None
        if not isinstance(message, dict):
            # 规范不许 server 往 stdout 写别的，但真有 server 打 banner：记下来，不断连接
            self._keep(raw, prefix="[stdout 上的非 JSON 行] ")
            return
        method = message.get("method")
        if isinstance(method, str):
            params = message.get("params")
            params = params if isinstance(params, dict) else {}
            if "id" in message:  # server → 客户端的请求（旧协议的 ping 等）
                self._spawn(self._answer(message["id"], method, params))
            elif self.on_notification is not None:
                try:
                    self.on_notification(method, params)
                except Exception as e:  # noqa: BLE001  回调的 bug 不能拖垮读循环
                    self._stderr.append(f"[sa] 处理通知 {method} 出错：{type(e).__name__}: {e}")
            return
        request_id = message.get("id")
        future = self._pending.get(request_id) if isinstance(request_id, int) else None
        if future is None or future.done():
            return  # 超时或中断之后才到的迟到响应：请求方已经走了，丢掉
        error = message.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            future.set_exception(
                RpcError(
                    code if isinstance(code, int) else 0,
                    str(error.get("message", "")),
                    error.get("data"),
                )
            )
        else:
            result = message.get("result")
            future.set_result(result if isinstance(result, dict) else {})

    async def _answer(self, request_id: Any, method: str, params: dict[str, Any]) -> None:
        """回答 server 发来的请求。只有旧协议的 server 会发（新协议规定 server 不发请求）。"""
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        try:
            if self.on_request is None:
                raise RpcError(METHOD_NOT_FOUND, f"客户端不支持 {method}")
            reply["result"] = await self.on_request(method, params)
        except RpcError as e:
            reply["error"] = {"code": e.code, "message": e.message}
        except Exception as e:  # noqa: BLE001
            reply["error"] = {"code": INTERNAL_ERROR, "message": f"{type(e).__name__}: {e}"}
        if self._failure is None:
            with contextlib.suppress(McpError):
                await self._send(reply)

    async def _drain_stderr(self) -> None:
        """一直把 stderr 读走：没人读的话管道写满，server 会卡在写日志上。"""
        assert self._proc is not None and self._proc.stderr is not None
        buffer = b""
        while chunk := await self._proc.stderr.read(64 * 1024):
            *lines, buffer = (buffer + chunk).split(b"\n")
            for line in lines:
                self._keep(line)
            if len(buffer) > 64 * 1024:  # 一直不换行的输出：截一段存下，免得无限攒
                self._keep(buffer)
                buffer = b""
        if buffer:
            self._keep(buffer)

    def _keep(self, raw: bytes, prefix: str = "") -> None:
        text = raw.decode("utf-8", errors="replace").rstrip()
        if text:
            if len(text) > STDERR_LINE_CHARS:
                text = text[:STDERR_LINE_CHARS] + "…"
            self._stderr.append(prefix + text)

    # ------------------------------------------------------------------ 杂项

    def _fail(self, message: str) -> None:
        """连接不可用了：记下原因，让所有还在等的请求立刻失败，而不是等到超时。"""
        if self._failure is not None:
            return
        self._failure = message
        for future in self._pending.values():
            if not future.done():
                future.set_exception(McpError(message))

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        # 任务要有人持有引用，否则可能被垃圾回收掉
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task
