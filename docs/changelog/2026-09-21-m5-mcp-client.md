# M5 完成：手写 MCP 客户端，REPL / `sa run` / `sa serve` 都能接 MCP server

- 日期：2026-09-21
- 对比基线：`d082743`（Merge pull request #5 from tjiisztiuv/claude/m4-content-implementation-59e2e2）
- 对应里程碑：M5（MCP 客户端，已完成，5 步全部做完）

## 功能变化

- 新增：手写 MCP 客户端（不依赖官方 SDK），分四层：
  - stdio 传输层（`mcp/transport.py`）：子进程、按行收发 JSON-RPC，`id → Future` 支持并发请求，
    超时/取消发 `notifications/cancelled`，stderr 常驻排空防写满卡死，单行消息上限 64MB，
    子进程自成进程组，关闭按「关 stdin → 等 → SIGTERM → 等 → SIGKILL」顺序、连带 `npx` 拉起的
    `node` 一起清掉。
  - 协议会话层（`mcp/client.py`）：应对 MCP 规范 2026-07-28 取消 `initialize` 握手的换代——
    先发 `server/discover` 探测，探测到新协议后每个请求带 `_meta`；探测失败或超时（10s）退回
    旧协议的 `initialize` + `notifications/initialized`；`protocol` 可配 `auto`/`modern`/`legacy`；
    `tools/list` 自动翻页（最多 100 页）；`tools/call` 结果（文本/图片/音频/资源）转成给模型看的
    文本；新协议的 `-32022`（版本不支持）/`-32021`（缺客户端能力）/`input_required` 转成可读错误。
  - 工具接入层（`mcp/tools.py`）：配置 → server 子进程（子进程环境是白名单 `BASE_ENV` + `env` +
    `env_vars`，密钥只能走 `env_vars`）；MCP 工具 → 注册表 `Tool`（工具名加前缀
    `mcp__<server>__<tool>` 避免和内置工具撞名，`parameters` 原样交给模型、参数只做「是个 JSON
    对象」的最宽松校验，权限按 `readOnlyHint` + 配置的 `trust_annotations`/`permissions` 决定，
    需要确认时用 `confirm_reason` 给出针对性说明）。
  - 多 server 管理层（`mcp/manager.py`）：`McpManager`/`McpServer` 并行启动所有 server、单个失败
    互不影响；server 崩溃后不自动盯着重启，等下次调用时再重启（5 分钟内最多 3 次，带锁防并发
    重复重启）；中途崩溃的那次调用不自动重试，把情况说明写进错误交给模型决定；server 的
    `instructions` 截到 2000 字、标明第三方内容后追加进 system prompt。
- 新增：`sa mcp list` 子命令——把每个配置的 server 真的启动一遍，列出状态和工具再关掉，不需要
  API key；有失败时退出码为 1。
- 新增：REPL 新命令 `/mcp`，查看 MCP server 的连接状态、协议代际、工具数、schema 大小、重启次数；
  REPL 启动时并行拉起所有 server，等全部有结果再接受输入，卡住的 server 可以按 Ctrl+C 跳过。
- 升级：`sa run`（headless）启动时拉起 MCP server 并把工具注册进 agent，状态摘要写 stderr（不混进
  `sa run ... > out.txt` 的正文）；单个 server 起不来只警告，不影响退出码。
- 升级：`sa serve` 的 `Runner` 在后台事件循环里持有一个 `McpManager`，所有空间、所有会话共用同一批
  server 子进程；`shutdown()` 时先关 MCP server 再停事件循环；新增 `ApiServer`（继承
  `ThreadingHTTPServer`），HTTP server 关闭时顺带关 `Runner`。
- 升级：`Tool` 新增 `parameters`（现成 JSON Schema，MCP 工具用）和 `confirm_reason`（需要确认时的
  说明）两个字段；注册表在 `Decision.ASK` 且判定器没给理由时，优先用 `tool.confirm_reason`，其次
  才用默认的「工具 X 会改动文件或执行命令」。
- 升级：`config.py` 新增 `McpServerConfig`（`command`/`args`/`env`/`env_vars`/`cwd`/`enabled`/
  `protocol`/`startup_timeout`/`tool_timeout`/`enabled_tools`/`disabled_tools`/
  `trust_annotations`/`permissions`）和 `Config.mcp_servers: dict[str, McpServerConfig]`；校验
  server 名（只能字母数字 `-` 和单个 `_`，最长 24 字符，因为要拼进工具名）、`env_vars` 只收变量名
  不收值、`env` 里键名像密钥（`KEY`/`TOKEN`/`SECRET`/`PASSWORD` 等段）时报错且不回显值。
- 文档：README 新增「MCP server」一节（配置示例、启动/权限/密钥/崩溃行为说明）；ARCHITECTURE 新增
  「MCP 客户端（M5）」小节并更新代码结构表；ROADMAP 把 M5 标记为已完成，补充实测结论；新增学习
  笔记 `docs/notes/M5-mcp-client.md`（协议换代取舍、stdio 管道踩坑、权限体系融合、真实模型实测、
  踩到的坑、现在的缺口）。

## 函数级改动

### `src/simpleagent/mcp/transport.py`（新文件）

| 函数 / 类 | 说明 |
|---|---|
| `StdioTransport` | 一个 MCP server 子进程及其上的 JSON-RPC 收发；`start()`/`close()` 管生命周期，`request()`/`notify()` 发送，读循环 `_read_stdout()`/`_dispatch()` 按 id 把响应交回等待中的 Future，`_drain_stderr()` 常驻排空 stderr |
| `McpError` / `RpcError` / `McpTimeout` | 三类失败：连接/进程层错误、server 回的 JSON-RPC error（带 code）、请求超时 |

### `src/simpleagent/mcp/client.py`（新文件）

| 函数 / 类 | 说明 |
|---|---|
| `McpClient` | 一个 server 的协议会话；`connect()` 先 `_discover()` 探测新协议，探测失败/超时退回 `_initialize()`；`list_tools()` 翻页拉取工具；`call_tool()` 调用并转文本 |
| `choose_version()` | 从 server 声明支持的版本里挑双方都认的最新版本（新协议优先） |
| `render_content()` / `_render_block()` | `tools/call` 结果的各内容块（text/image/audio/resource\_link/resource）转成给模型看的文本 |
| `_parse_tool()` | `tools/list` 里的一项解析成 `McpTool`，格式不对抛 `ValueError` 由调用方跳过 |

### `src/simpleagent/mcp/tools.py`（新文件）

| 函数 / 类 | 说明 |
|---|---|
| `server_env()` | server 子进程的环境变量：白名单 `BASE_ENV` + 配置的 `env` + `env_vars`（后者缺失时抛 `McpError`，报错里不回显值） |
| `client_from_config()` | 按 `McpServerConfig` 造一个还没启动的 `McpClient` |
| `tool_name()` | MCP 工具名 → `mcp__<server>__<tool>`，清洗非法字符、超长截短加哈希 |
| `normalize_schema()` | MCP `inputSchema` 整理成模型接口都收的样子（去掉 `$schema`，补 `type`/`properties`） |
| `_permission()` | 按 `readOnlyHint`/`destructiveHint`/`openWorldHint` 和配置算出（能否并行, 权限等级, 确认说明） |
| `wrap_tools()` | 一个 server 的 `McpTool` 列表 → 注册表 `Tool` 列表，应用 `enabled_tools`/`disabled_tools`/`permissions` 过滤和覆盖 |

### `src/simpleagent/mcp/manager.py`（新文件）

| 函数 / 类 | 说明 |
|---|---|
| `McpServer` | 一个 server 的句柄和状态机（`disabled`/`idle`/`starting`/`ready`/`failed`/`closed`），也是 `wrap_tools` 要的 `ToolCaller`；`start()` 不抛异常，失败原因记在 `error`；`call_tool()` 挂了会先重启再调用 |
| `McpServer._live_client()` | 拿到活着的 client，server 挂了就重启（带锁防并发重复重启，5 分钟内最多 3 次） |
| `McpManager` | 多个 `McpServer` 的集合；`start()` 并行启动、`close()` 并行关闭、`tools()` 汇总工具、`prompt_section()` 拼 system prompt 附加内容、`summary()`/`describe()` 给状态行 |

### `src/simpleagent/tools/base.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Tool.parameters` | 新增 | 现成 JSON Schema；给了就原样交给模型，不再从 `args_model` 生成 |
| `Tool.confirm_reason` | 新增 | 需要确认时给人看的说明；`None` 时用注册表默认说法 |
| `Tool.schema()` | 修改 | 有 `parameters` 就直接用，否则才从 `args_model.model_json_schema()` 生成 |

### `src/simpleagent/tools/registry.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `ToolRegistry.execute()` | 修改 | `Decision.ASK` 且判定器没给理由时，优先用 `tool.confirm_reason`，其次才是默认的「工具 X 会改动文件或执行命令」 |

### `src/simpleagent/config.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `looks_secret()` | 新增 | 环境变量名按 `_` 切分后是否含 `KEY`/`TOKEN`/`SECRET`/`PASSWORD` 等段（`KEYBOARD` 不算） |
| `McpServerConfig` | 新增 | 一个 MCP server 的配置模型；`env_vars` 校验只能是变量名，`env` 校验键名不能像密钥 |
| `Config.mcp_servers` | 新增 | `dict[str, McpServerConfig]`，按配置顺序保存 |
| `Config._check_mcp_server_names()` | 新增 | 校验 server 名合法字符集和长度上限（24，因为要拼进工具名） |

### `src/simpleagent/cli.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `list_mcp_servers()` | 新增 | `sa mcp list` 的实现：启动所有 server、打印状态和工具、关闭；有失败返回 1 |
| `main()` | 修改 | 新增 `mcp` 子命令（及 `mcp list` 子子命令）的 argparse 注册与分发 |

### `src/simpleagent/serve/runner.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Runner.__init__()` | 修改 | 新增 `self.mcp = McpManager(config.mcp_servers)` |
| `Runner.start()` | 修改 | 在后台事件循环里异步启动 MCP server |
| `Runner._report_mcp()` | 新增 | MCP 启动完打一行状态到 stderr（serve 在后台跑，server 起不来要让人看得到） |
| `Runner.shutdown()` | 修改 | 先在事件循环里关掉 MCP server（等最多 10s），再停事件循环 |
| `Runner._build_agent()` | 修改 | agent 的工具集合并 `self.mcp.tools()`；system prompt 追加 `self.mcp.prompt_section()` |
| `Runner._run_input()` | 修改 | 第一次请求前等 MCP 启动完成，工具列表在请求前就定下来 |

### `src/simpleagent/serve/app.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `ApiServer` | 新增 | 继承 `ThreadingHTTPServer`，`server_close()` 时顺带调用 `Runner.shutdown()` |
| `make_server()` | 修改 | 返回类型从 `ThreadingHTTPServer` 改为 `ApiServer`，把 `app` 挂在 httpd 上 |

### `src/simpleagent/ui/headless.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Headless.__init__()` | 修改 | 新增 `self.mcp = McpManager(config.mcp_servers)` |
| `Headless.run()` | 修改 | 原逻辑移到新的 `_run()`；`run()` 先 `_start_mcp()`，结束时 `finally` 里关闭 MCP |
| `Headless._start_mcp()` | 新增 | 启动 MCP server，把工具注册进 `agent.tools`，追加 system prompt，状态摘要写 stderr |

### `src/simpleagent/ui/repl.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Repl.__init__()` | 修改 | 新增 `self.mcp = McpManager(config.mcp_servers)`；`HELP` 文案新增 `/mcp` 一行 |
| `Repl.run()` | 修改 | 进入循环前调用 `_start_mcp()`；`finally` 里 `runner.run(self.mcp.close())` |
| `Repl._start_mcp()` | 新增 | 在同一个 asyncio 事件循环里启动 MCP server（Ctrl+C 可跳过），注册工具、追加 system prompt、打印状态摘要 |
| `handle()` 的 `case "mcp"` | 新增 | `/mcp` 命令，调用 `self.mcp.describe()` 打印详情 |

## 配置与依赖

- 没有新增运行时依赖：`pyproject.toml`、`uv.lock` 均无变化。
- MCP 是可选功能：`config.toml` 里不写 `[mcp_servers.*]` 行为和之前完全一样。
- 要用 `@modelcontextprotocol/server-filesystem` 这类 server，本机需要有 `node`/`npx`（不是
  SimpleAgent 的依赖，是 server 自己的运行环境）。
- **需要手动处理**：已经跑过 `sa init` 的老用户，`config.toml` 不会自动补上 MCP 示例；想用的话
  要手动在 `config.toml` 里加一段 `[mcp_servers.<名字>]`（写法见 README「MCP server」一节，或
  `src/simpleagent/config.example.toml` 末尾新增的注释示例）。密钥（如 `env_vars` 里声明的变量）
  放到环境变量或 `~/.simpleagent/.env`，不要写进 `config.toml`。

## 测试

- 新增测试文件：
  - `tests/mcp/test_transport.py`（21 个测试）：传输层收发、并发多路复用、超时与取消、消息分发、
    健壮性（超长行、非 JSON 输出、进程崩溃）、按规范顺序关闭。
  - `tests/mcp/test_client.py`（43 个测试，含参数化展开）：新旧两代协议的连接开场、`tools/list` 翻页、
    `tools/call` 各类内容渲染、server 发来的消息处理、错误转可读文案。
  - `tests/mcp/test_tools.py`（21 个测试，含参数化展开）：子进程环境白名单、工具名清洗、schema 整理、权限对应
    规则，以及接假 server + `FakeLLM` 的端到端 agent loop 调用。
  - `tests/mcp/test_manager.py`（10 个测试）：多 server 并行启动与失败隔离、崩溃后重启（含次数
    上限）、关闭、`instructions` 拼装、状态显示。
  - `tests/mcp/test_cli_mcp.py`（3 个测试）：`sa mcp list` 的输出和退出码。
  - `tests/serve/test_runner_mcp.py`（2 个测试）：`Runner` 的 `McpManager` 在多空间间共享、
    `shutdown()` 时正确关闭。
  - 假 server：`tests/fixtures/mcp/fake_server.py`（只按方法名做事的通用假 server，测传输层）、
    `tests/fixtures/mcp/fake_mcp_server.py`（按 `--era` 模拟新/旧协议及各种边界情况的假 MCP
    server，测协议层和上层）。
- 修改测试文件：`tests/conftest.py`（新增 `fake_mcp` fixture）、`tests/test_config.py`（
  `McpServerConfig` 校验用例）、`tests/test_tool_registry.py`（`parameters`/`confirm_reason`
  用例）、`tests/test_repl.py`（`/mcp` 命令、启动失败提示）、`tests/test_headless.py`（MCP 工具
  走白名单、启动失败只警告）。
- 测试结果：`uv run pytest -q` 517 passed。
- `uv run ruff check`：All checks passed。
- `uv run ruff format --check`：133 files already formatted，无需改动。
- **真实模型验证**（记在 `docs/notes/M5-mcp-client.md` 第 7 节）：用 deepseek-flash + 官方
  `@modelcontextprotocol/server-filesystem`（2026.8.31，只讲旧协议 2025-11-25）实测，模型调用了
  `mcp__filesystem__list_directory`/`search_files`/`write_file`/`read_text_file` 等工具；内置
  `write_file` 因目标在工作目录外被拒后，模型自己改用 `mcp__filesystem__write_file` 写入成功。

## 相关笔记

- `docs/notes/M5-mcp-client.md`
