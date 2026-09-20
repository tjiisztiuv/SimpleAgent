"""命令行入口：sa。"""

from __future__ import annotations

import argparse
import asyncio
import errno
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from simpleagent.agent.session import SESSION_DIRNAME, Session, SessionStore
from simpleagent.config import ConfigError, home_dir, init_config, load_config
from simpleagent.panel.store import LEVELS
from simpleagent.ui.debug import DEBUG_LEVELS

# --resume 不给值时的哨兵：argparse 用 const 填进来，好区分「没传」和「传了但没给 id」
LATEST = "__latest__"


class CliError(Exception):
    """命令行用法层面的错误：打印一句人话就退出，不像 ConfigError 那样带配置前缀。"""


def package_version() -> str:
    """装好的包的版本号（来自 pyproject.toml）；直接从源码跑、没装过包时返回 unknown。"""
    try:
        return version("simpleagent")
    except PackageNotFoundError:
        return "unknown"


def session_store() -> SessionStore:
    return SessionStore(home_dir() / SESSION_DIRNAME)


def parse_allowed(text: str) -> list[str]:
    """`--allow "bash, write_file"` → `["bash", "write_file"]`。"""
    return [item.strip() for item in text.split(",") if item.strip()]


def resolve_resume(store: SessionStore, value: str | None) -> Session | None:
    """把 --resume [<id>] 解析成一个已存在的会话；None 表示这次不恢复，照常新开。"""
    if value is None:
        return None
    session_id = store.latest() if value in (LATEST, "") else value
    if session_id is None:
        raise CliError("没有可以恢复的会话（还没保存过任何会话）。")
    session = store.load(session_id)
    if session is None:
        raise CliError(f"找不到会话 {session_id}；用 `sa sessions` 列出已有的会话。")
    return session


def inbox_push(title: str, body: str | None, level: str, source: str) -> int:
    """往控制面板投一条消息。直接追加 inbox.jsonl，不需要 sa serve 在跑。

    正文三种给法：`-b 文本`；`-b -` 强制读 stdin；不给 `-b` 而 stdin 是管道时自动读，
    所以例行任务可以直接 `pytest 2>&1 | sa inbox push -t 夜间测试`。
    """
    from simpleagent.panel.store import PanelStore

    title = title.strip()
    if not title:
        raise CliError("标题不能为空：sa inbox push -t <标题>")
    if body == "-" or (body is None and not sys.stdin.isatty()):
        body = sys.stdin.read()
    item = PanelStore().add_message(
        source=source.strip() or "cli", title=title, body=(body or "").rstrip(), level=level
    )
    print(item.id)
    return 0


def list_sessions(store: SessionStore, limit: int) -> int:
    rows = store.list(limit=limit)
    if not rows:
        print("还没有保存的会话。")
        return 0
    for info in rows:
        print(f"{info.id}   {info.title or '(无标题)'}")
        detail = [f"{info.messages} 条消息", f"{info.requests} 次请求"]
        if info.profile:
            detail.append(info.profile)
        if info.updated_at:
            detail.append(f"更新于 {info.updated_at}")
        print(f"    {' · '.join(detail)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sa", description="SimpleAgent：自用、可学习的本地 agent")
    parser.add_argument(
        "-V", "--version", action="version", version=f"simpleagent {package_version()}"
    )
    parser.add_argument(
        "--debug",
        nargs="?",
        const="on",
        default=None,
        choices=DEBUG_LEVELS,
        metavar="LEVEL",
        help="显示 API 与工具调用的过程（on / verbose），输出走 stderr",
    )
    parser.add_argument("-m", "--profile", help="模型 profile，默认取配置里的 default_profile")
    parser.add_argument(
        "--resume",
        nargs="?",
        const=LATEST,
        default=None,
        metavar="ID",
        help="从已保存的会话继续；不给 id 就接着最近那次",
    )
    commands = parser.add_subparsers(dest="command", metavar="<command>")
    commands.add_parser("init", help="生成默认配置文件 ~/.simpleagent/config.toml")
    serve = commands.add_parser("serve", help="启动本地 API（HTTP + SSE），供桌面客户端连接")
    serve.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    serve.add_argument("--port", type=int, default=8384, help="监听端口（默认 8384）")
    run = commands.add_parser("run", help="headless：执行一个任务后退出，不与人交互")
    run.add_argument("prompt", help="要做的任务")
    run.add_argument("--cwd", default=None, help="工作目录，默认当前目录")
    run.add_argument(
        "--allow",
        default="",
        metavar="TOOLS",
        help="允许自动执行的工具名，逗号分隔（如 bash,write_file）；没列的一律拒绝",
    )
    run.add_argument(
        "--resume",
        nargs="?",
        const=LATEST,
        default=argparse.SUPPRESS,
        metavar="ID",
        help="接着某个已有会话跑",
    )
    # 用 SUPPRESS 而不是 None：子解析器没传时不要把父级 -m/--profile 的值覆盖掉
    run.add_argument("-m", "--profile", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    run.add_argument(
        "--debug",
        nargs="?",
        const="on",
        default=argparse.SUPPRESS,
        choices=DEBUG_LEVELS,
        metavar="LEVEL",
        help=argparse.SUPPRESS,
    )
    sessions = commands.add_parser("sessions", help="列出已保存的会话")
    sessions.add_argument("--limit", type=int, default=20, help="最多显示几条，默认 20")
    inbox = commands.add_parser("inbox", help="控制面板的消息：脚本 / 例行任务往这里投结论")
    inbox_commands = inbox.add_subparsers(dest="inbox_command", metavar="<action>", required=True)
    push = inbox_commands.add_parser("push", help="投递一条消息（不需要 sa serve 在跑）")
    push.add_argument("-t", "--title", required=True, help="标题，面板列表里显示的那一行")
    push.add_argument(
        "-b",
        "--body",
        default=None,
        help="正文；不给时若有管道输入就从 stdin 读，- 表示强制读 stdin",
    )
    push.add_argument("--level", choices=LEVELS, default="info", help="级别，默认 info")
    push.add_argument(
        "--source", default="cli", help="来源，面板上显示成小标签（如 schedule），默认 cli"
    )
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            print(f"已生成配置：{init_config()}")
            return 0
        if args.command == "serve":
            from simpleagent.serve.app import make_server

            try:
                httpd = make_server(load_config(), host=args.host, port=args.port)
            except OSError as e:
                if e.errno != errno.EADDRINUSE:
                    raise
                print(f"端口 {args.port} 已被占用，启动失败。", file=sys.stderr)
                print("  多半是上一次的 sa serve 还在后台跑——关浏览器不会停掉它。", file=sys.stderr)
                print(f"  查占用者：lsof -nP -iTCP:{args.port} -sTCP:LISTEN", file=sys.stderr)
                print("  确认是自己的进程后 kill <PID>，或者换个端口：", file=sys.stderr)
                print(f"    sa serve --port {args.port + 1}", file=sys.stderr)
                return 1
            print(f"SimpleAgent 本地 API 已启动：http://{args.host}:{args.port}")
            print(f"浏览器打开工作台：http://{args.host}:{args.port}/")
            print("按 Ctrl+C 停止。")
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\n已停止。")
            finally:
                httpd.server_close()  # 立刻把端口还回去，不等进程回收
            return 0
        if args.command == "sessions":
            return list_sessions(session_store(), args.limit)
        if args.command == "inbox":
            return inbox_push(args.title, args.body, args.level, args.source)
        if args.command == "run":
            from simpleagent.ui.headless import Headless

            store = session_store()
            session = resolve_resume(store, getattr(args, "resume", None))
            cwd = Path(args.cwd).expanduser().resolve() if args.cwd else Path.cwd()
            frontend = Headless(
                load_config(),
                profile=args.profile,
                cwd=cwd,
                allowed_tools=parse_allowed(args.allow),
                session=session,
                store=store,
                debug=getattr(args, "debug", None),
            )
            with asyncio.Runner() as runner:
                try:
                    return runner.run(frontend.run(args.prompt))
                finally:
                    runner.run(frontend.agent.llm.close())
        from simpleagent.ui.repl import Repl  # 延迟导入：init 不需要加载 openai

        store = session_store()
        return Repl(
            load_config(),
            profile=args.profile,
            session=resolve_resume(store, args.resume),
            store=store,
            debug=args.debug,
        ).run()
    except CliError as e:
        print(str(e), file=sys.stderr)
        return 1
    except ConfigError as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return 1
