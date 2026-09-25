"""命令行入口：sa。"""

from __future__ import annotations

import argparse
import asyncio
import errno
import sys
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from simpleagent.agent.session import SESSION_DIRNAME, Session, SessionStore
from simpleagent.config import (
    ConfigError,
    config_path,
    dev_checkout,
    home_dir,
    init_config,
    load_config,
)
from simpleagent.panel.store import LEVELS
from simpleagent.permissions import Mode, parse_mode
from simpleagent.scheduler import ScheduleError, init_schedules, load_schedules, schedules_path
from simpleagent.ui.debug import DEBUG_LEVELS

# --resume 不给值时的哨兵：argparse 用 const 填进来，好区分「没传」和「传了但没给 id」
LATEST = "__latest__"
# sa serve 默认端口；从源码仓库跑时换一个，开发版和日常版能同时开
SERVE_PORT = 8384
DEV_SERVE_PORT = 8385
WEEKDAYS = "一二三四五六日"


class CliError(Exception):
    """命令行用法层面的错误：打印一句人话就退出，不像 ConfigError 那样带配置前缀。"""


def package_version() -> str:
    """装好的包的版本号（来自 pyproject.toml）；直接从源码跑、没装过包时返回 unknown。"""
    try:
        return version("simpleagent")
    except PackageNotFoundError:
        return "unknown"


def version_text() -> str:
    """`sa --version`：从源码仓库跑时标出仓库和数据目录，一眼分清是开发版还是日常版。"""
    text = f"simpleagent {package_version()}"
    if root := dev_checkout():
        text += f"（开发模式：{root}，数据目录 {home_dir()}）"
    return text


def session_store() -> SessionStore:
    return SessionStore(home_dir() / SESSION_DIRNAME)


def parse_allowed(text: str) -> list[str]:
    """`--allow "bash, write_file"` → `["bash", "write_file"]`。"""
    return [item.strip() for item in text.split(",") if item.strip()]


def mode_arg(text: str) -> Mode:
    """--mode 的取值：英文值和中文名都认，认不出来让 argparse 报错并列出可选值。"""
    try:
        return parse_mode(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


MODE_HELP = "权限模式：只读 read-only / 工作区 workspace / 全放行 full"


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


def init_files() -> int:
    """sa init：缺哪个配置文件就生成哪个；都已存在时报错，和以前一样不覆盖。

    已经有 config.toml 的老用户再跑一次就能拿到 schedules.toml 模板。
    """
    targets = [
        ("配置", config_path(), init_config),
        ("定时任务模板", schedules_path(), init_schedules),
    ]
    if all(path.exists() for _, path, _ in targets):
        raise CliError(f"配置文件都已存在：{config_path()}、{schedules_path()}")
    for label, path, create in targets:
        if path.exists():
            print(f"{label}已存在，跳过：{path}")
        else:
            print(f"已生成{label}：{create()}")
    return 0


def format_fire_time(dt: datetime) -> str:
    return f"{dt:%m-%d} 周{WEEKDAYS[dt.weekday()]} {dt:%H:%M}"


def list_schedules(now: datetime | None = None) -> int:
    """列出定时任务和下次触发时间。有任务加载失败时退出码为 1，方便改完顺手检查。"""
    path = schedules_path()
    if not path.exists():
        print(f"还没有定时任务：先 `sa init` 生成 {path}，照里面的示例写一个。")
        return 0
    config = load_config()
    permissions = config.permissions
    schedules = load_schedules(path, profiles=config.profiles)
    if not schedules.jobs and not schedules.errors:
        print(f"{path} 里还没有任务。")
        return 0
    now = now or datetime.now().astimezone()
    for job in schedules.jobs.values():
        print(f"{job.name}   {job.title}" if job.title else job.name)
        if not job.enabled:
            detail = [job.cron, "已停用"]
        else:
            detail = [job.cron, f"下次 {format_fire_time(job.next_fire(now))}"]
            detail.append(f"权限 {permissions.unattended(job.mode).label}")
            if job.allowed_tools:
                detail.append(f"允许 {', '.join(job.allowed_tools)}")
            if job.profile:
                detail.append(job.profile)
        print(f"    {' · '.join(detail)}")
    for name, reason in schedules.errors.items():
        print(name)
        print(f"    ✗ {reason}")
    return 1 if schedules.errors else 0


def list_mcp_servers() -> int:
    """sa mcp list：真的把每个 server 启动一遍，列出状态和工具再关掉。有失败时退出码为 1。

    用不到模型，所以不需要 API key：改完 [mcp_servers.*] 顺手跑一下就能检查。
    """
    from simpleagent.mcp.manager import McpManager

    config = load_config()
    if not config.mcp_servers:
        print(f"还没有配置 MCP server：在 {config_path()} 里加 [mcp_servers.<名字>]，")
        print("写上 command 和 args。")
        return 0
    manager = McpManager(config.mcp_servers)

    async def check() -> tuple[list[str], bool]:
        # 关闭之前就把结果取出来：关完所有 server 的状态都是 closed 了
        try:
            await manager.start(force=True)  # 懒启动的也真启动一遍，顺带刷新它们的工具缓存
            return manager.describe(tools=True), manager.failed
        finally:
            await manager.close()

    enabled = [server.name for server in manager.enabled]
    if enabled:
        print(f"启动 {'、'.join(enabled)}……", file=sys.stderr)
    lines, failed = asyncio.run(check())
    for line in lines:
        print(line)
    return 1 if failed else 0


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
    parser = argparse.ArgumentParser(
        prog="sa",
        description="SimpleAgent：自用、可学习的本地 agent",
        # 不按终端宽度折行：--version 里带路径，折开了没法复制
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version", version=version_text())
    parser.add_argument(
        "--debug",
        nargs="?",
        const="on",
        default=None,
        choices=DEBUG_LEVELS,
        metavar="LEVEL",
        help="显示 API 与工具调用的过程（on / verbose / full），输出走 stderr",
    )
    parser.add_argument("-m", "--profile", help="模型 profile，默认取配置里的 default_profile")
    parser.add_argument(
        "--mode",
        type=mode_arg,
        default=None,
        metavar="MODE",
        help=f"{MODE_HELP}；默认取配置里的 [permissions].mode（工作区）",
    )
    parser.add_argument(
        "--resume",
        nargs="?",
        const=LATEST,
        default=None,
        metavar="ID",
        help="从已保存的会话继续；不给 id 就接着最近那次",
    )
    commands = parser.add_subparsers(dest="command", metavar="<command>")
    commands.add_parser(
        "init", help="生成默认配置 ~/.simpleagent/config.toml 和定时任务模板 schedules.toml"
    )
    serve = commands.add_parser("serve", help="启动本地 API（HTTP + SSE），供桌面客户端连接")
    serve.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    port = DEV_SERVE_PORT if dev_checkout() else SERVE_PORT
    serve.add_argument("--port", type=int, default=port, help=f"监听端口（默认 {port}）")
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
    run.add_argument(
        "--mode",
        type=mode_arg,
        default=argparse.SUPPRESS,
        metavar="MODE",
        help=f"{MODE_HELP}；默认跟配置走，但不继承「全放行」",
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
    schedule = commands.add_parser("schedule", help="定时任务（~/.simpleagent/schedules.toml）")
    schedule_commands = schedule.add_subparsers(
        dest="schedule_command", metavar="<action>", required=True
    )
    schedule_commands.add_parser("list", help="列出任务和下次触发时间，写错的任务会标出原因")
    mcp = commands.add_parser("mcp", help="MCP server（config.toml 里的 [mcp_servers.*]）")
    mcp_commands = mcp.add_subparsers(dest="mcp_command", metavar="<action>", required=True)
    mcp_commands.add_parser("list", help="启动每个 server，列出状态和工具（不需要 API key）")
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
            return init_files()
        if args.command == "schedule":
            return list_schedules()
        if args.command == "mcp":
            return list_mcp_servers()
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
            if dev_checkout():
                print(f"开发模式：数据目录 {home_dir()}")
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
                mode=args.mode,
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
            mode=args.mode,
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
    except ScheduleError as e:
        print(f"定时任务配置错误：{e}", file=sys.stderr)
        return 1
