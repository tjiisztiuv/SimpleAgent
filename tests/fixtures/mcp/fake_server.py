"""测试用的假 MCP server：stdio 上按行收发 JSON-RPC，不懂 MCP 语义，只按方法名做事。

启动参数：
  --banner          启动时先往 stdout 打一行非 JSON、往 stderr 打一行日志（真有 server 这么干）
  --ignore-eof      stdin 关了也不退出（测 SIGTERM 升级）
  --ignore-sigterm  忽略 SIGTERM（测 SIGKILL 升级）
  --spawn-child F   拉起一个 sleep 子进程，pid 写进文件 F（测整个进程组的清理）

每个请求在单独的线程里处理：sleep 不挡后面的请求，响应可以乱序。
只用标准库，测试里用 sys.executable 启动，不依赖 node。
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time

write_lock = threading.Lock()
cancelled: list[dict] = []  # 收到的 notifications/cancelled
waiting: dict[str, list] = {}  # 发给客户端的请求 id → [Event, 客户端的回复]


def send(message: dict) -> None:
    with write_lock:
        sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def ask_client(params: dict) -> dict:
    request_id = f"srv-{time.monotonic_ns()}"
    slot: list = [threading.Event(), None]
    waiting[request_id] = slot
    send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": params["method"],
            "params": params.get("params", {}),
        }
    )
    slot[0].wait(5)
    return {"reply": slot[1]}


def handle(message: dict) -> None:
    method = message["method"]
    params = message.get("params") or {}
    if method == "echo":
        result = params
    elif method == "sleep":
        time.sleep(params["seconds"])
        result = {"slept": params["seconds"]}
    elif method == "fail":
        error = {"code": params.get("code", -32000), "message": params.get("message", "失败")}
        send({"jsonrpc": "2.0", "id": message["id"], "error": error})
        return
    elif method == "big":
        result = {"text": "x" * params["size"]}
    elif method == "crash":
        sys.stderr.write(params.get("stderr", "boom") + "\n")
        sys.stderr.flush()
        os._exit(params.get("code", 3))
    elif method == "notify_me":
        send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"text": "hi"}})
        result = {}
    elif method == "ask_client":
        result = ask_client(params)
    elif method == "stderr_flood":
        for _ in range(params["lines"]):
            sys.stderr.write("y" * 99 + "\n")
        sys.stderr.flush()
        result = {"ok": True}
    elif method == "garbage":
        with write_lock:
            sys.stdout.write("this is not json\n")
            sys.stdout.flush()
        result = {"ok": True}
    elif method == "last_cancelled":
        result = {"cancelled": cancelled}
    else:
        error = {"code": -32601, "message": f"Method not found: {method}"}
        send({"jsonrpc": "2.0", "id": message["id"], "error": error})
        return
    send({"jsonrpc": "2.0", "id": message["id"], "result": result})


def main() -> None:
    args = sys.argv[1:]
    if "--ignore-sigterm" in args:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if "--spawn-child" in args:
        pid_file = args[args.index("--spawn-child") + 1]
        child = subprocess.Popen(
            ["sleep", "60"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with open(pid_file, "w") as f:
            f.write(str(child.pid))
    if "--banner" in args:
        print("fake server starting...", flush=True)
        sys.stderr.write("fake server: listening on stdio\n")
        sys.stderr.flush()
    while line := sys.stdin.readline():
        message = json.loads(line)
        if "method" not in message:  # 客户端对 ask_client 的回复
            slot = waiting.get(message.get("id"))
            if slot is not None:
                slot[1] = message
                slot[0].set()
        elif "id" not in message:  # 通知
            if message["method"] == "notifications/cancelled":
                cancelled.append(message.get("params"))
        else:
            threading.Thread(target=handle, args=(message,), daemon=True).start()
    if "--ignore-eof" in args:
        while True:
            time.sleep(1)


if __name__ == "__main__":
    main()
