# MCP：修复 npx 卡住时的误导提示 + 新增按需启动（start = "lazy"）

- 日期：2026-09-22
- 对比基线：`a7b39da`（开发环境与日常环境隔离，发布 v0.2.0）
- 对应里程碑：M5 之后的补强（MCP 客户端）

## 功能变化

- 修复：npx 启动的 MCP server 卡住时提示误导。起因是 `npx -y <包>` 不带版本号时每次启动都要联网查最新版，`registry.npmjs.org` 连不上（比如代理不通）就一直挂着、一行 stderr 都没有；sa 等满 `startup_timeout` 后报「initialize 超过 N 秒没有响应」，却提示「npx 第一次下载，再试一次就好」——这个提示没用。现在超时且 stderr 为空且命令是 npx 时，改为提示卡在联网查 npm 仓库，建议在 args 最前面加 `--prefer-offline`，或者 `npm install -g` 装好后把 command 换成装好的命令；其他超时（非 npx，或 npx 有 stderr 输出）保持原提示不变。
- 新增：MCP server 支持按需启动（`start = "lazy"`，默认仍是 `"eager"`，不影响现有配置）。lazy 的 server 连上一次后，会把它原始的工具清单（过滤、权限之前）缓存到 `<数据目录>/mcp_cache/<名字>.json`；下次 sa 启动时如果缓存还能用（启动参数没变），直接按缓存登记工具、不拉起进程，模型第一次调用它的工具时才真正启动（不计入自动重启次数）。真连上后如果工具和缓存对不上，本次会话仍按已登记的工具走（前缀缓存的约束），缓存更新到下次启动生效。第一次还没有缓存时，照常启动一次再写缓存。`sa mcp list` 用 `force=True` 把懒启动的 server 也真连一遍，顺带刷新缓存。REPL 启动时的「启动 MCP server：……」提示只列真正要等待的 server（standby 的不列）。
- 文档：README、`docs/ARCHITECTURE.md`、`config.example.toml` 补充 `--prefer-offline` 的建议和原因、`start` 配置项说明、按需启动机制、新增的 `mcp_cache/` 数据目录说明。

## 函数级改动

### `src/simpleagent/mcp/client.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `_is_npx(command)` | 新增 | 判断命令文件名是不是 `npx` / `npx.cmd`（纯函数） |
| `McpClient._explain()` | 修改 | 超时 + stderr 为空 + 命令是 npx 时，改为提示卡在联网查 npm 仓库、建议 `--prefer-offline` 或全局安装；其余分支（旧协议探测提示、慢启动提示、只讲新协议提示）不变 |

### `src/simpleagent/mcp/cache.py`（新文件）

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `CachedServer` | 新增 | dataclass，装 `ServerInfo` + 工具清单，`load_cached` 的返回类型 |
| `cache_path(name)` | 新增 | 缓存文件路径 `<数据目录>/mcp_cache/<名字>.json` |
| `fingerprint(config)` | 新增 | 对 command / args / env / env_vars 名（不含值）/ cwd / protocol 取 sha256，作为「是不是同一个 server」的判据 |
| `load_cached(name, config)` | 新增 | 读缓存；文件不存在、JSON 损坏、版本不对、指纹不对都返回 `None`，视为没有缓存 |
| `save_cached(name, config, info, tools)` | 新增 | 写缓存：先写临时文件再 `replace` 改名，写到一半被打断不留半个文件；失败抛 `OSError` |
| `_tool_dict(tool)` | 新增 | `McpTool` → `tools/list` 原始形状，读回来时和 server 返回的走同一个解析函数 `_parse_tool` |

### `src/simpleagent/mcp/manager.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `State` | 修改 | 新增 `standby`：懒启动且有缓存、工具已登记、进程还没起 |
| `McpServer.__init__` | 修改 | 新增 `_cached_tools`（登记时用的缓存工具清单）、`stale`（真实工具和缓存是否对不上）两个字段 |
| `McpServer.standby()` | 新增 | `state == "idle"` 且 `config.start == "lazy"` 且有可用缓存时，按缓存登记工具、状态转 `standby`，不启动进程；没缓存则留在 `idle` |
| `McpServer.start()` | 修改 | 改为调用新拆出来的 `_launch()`，成功后调用 `_remember()` 写缓存；行为对 eager server 不变 |
| `McpServer._launch()` | 新增 | 从原来的 `start()` 里拆出来的「起进程 + 握手 + 列工具」逻辑，成功后状态转 `ready`，失败抛 `McpError`（原因也记在 `self.error`） |
| `McpServer._remember(info, tools)` | 新增 | 只有 `start == "lazy"` 的 server 才写缓存；`save_cached` 失败（`OSError`）记进 `warnings`，不影响本次连接 |
| `McpServer._live_client()` | 修改 | `state == "standby"` 时改走新增的 `_launch_standby()`，不再当作「没连过」直接起进程 |
| `McpServer._launch_standby()` | 新增 | 懒启动 server 第一次被调用时真正启动（`_launch()`），不计入 `restarts`；比较真实工具和 `_cached_tools`，不一样就置 `stale = True`（本次会话工具列表不变），同时调用 `_remember()` 更新缓存 |
| `McpManager.standby()` | 新增 | 对所有启用的 server 调用 `standby()`；单独拎出来是为了在真正启动前知道哪些 server 要等（REPL 用它决定提示里列哪些名字） |
| `McpManager.to_launch` | 新增（属性） | 返回 `state == "idle"`（真的要拉起进程）的 server 列表 |
| `McpManager.start(*, force=False)` | 修改 | 新增 `force` 参数：默认先调 `standby()` 再只启动 `to_launch`；`force=True` 时跳过 `standby()`，全部（含 lazy 的）都启动，给 `sa mcp list` 用 |
| `McpManager.summary()` | 修改 | `standby` 状态显示 `<名字> ◦ N 个工具（按需启动）` |
| `McpManager.describe()` | 修改 | 新增 `standby` 分支（显示按需启动、工具数、schema 大小、「第一次调用时启动」）；`ready` 分支里 `server.stale` 时提示「实际的工具和缓存的不一样：本次会话按缓存的来，下次启动生效」；工具清单 + 警告的输出抽成模块级 `_detail_lines()`，`ready` 和 `standby` 共用 |
| `_detail_lines(server, tools)` | 新增 | `describe()` 里 server 状态行下面的工具清单（免确认/需确认/可并行）和警告，从 `describe()` 方法体里抽出来的模块级函数 |

### `src/simpleagent/config.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `McpServerConfig.start` | 新增字段 | `Literal["eager", "lazy"] = "eager"`，默认值保持原有的「sa 启动时就拉起来」行为不变 |

### `src/simpleagent/cli.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `list_mcp_servers()` | 修改 | `manager.start()` 改为 `manager.start(force=True)`：懒启动的 server 也真连一遍，顺带刷新它们的工具缓存 |

### `src/simpleagent/ui/repl.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Repl._start_mcp()` | 修改 | 先调用 `self.mcp.standby()` 把有缓存的 lazy server 挑出来（不用等）；「启动 MCP server：……」提示只列 `self.mcp.to_launch` 里真正要等待的 server 名字 |

## 配置与依赖

- 新增配置项 `McpServerConfig.start`（`"eager"` / `"lazy"`，默认 `"eager"`），不改现有配置的行为。
- **需要手动处理**：用户通过 `uv tool install` 日常装的 `sa`（v0.2.0 快照）的配置校验模型还不认识 `start` 这个字段，而配置模型禁止未知键；如果在**日常**配置 `~/.simpleagent/config.toml` 里加 `start = "lazy"`，日常 `sa` 会因为未知配置项报错。要先执行 `uv tool install --reinstall "git+https://github.com/tjiisztiuv/SimpleAgent.git@main"` 升级日常版本，之后才能在日常配置里用 `start = "lazy"`。
- 用户的两份配置已经手动改过：`~/.simpleagent` 和 `~/.simpleagent-dev` 的 filesystem server 的 `args` 都加上了 `--prefer-offline`；开发配置 `~/.simpleagent-dev` 的 filesystem server 额外设成了 `start = "lazy"`（开发版本本身就是这次改动，能识别新字段）。
- 新增数据目录 `mcp_cache/<名字>.json`：lazy 的 server 连上后自动写入，`sa mcp list` 也会刷新。lazy 的 server 在还没有缓存时，第一次启动会照常启动一次（和 eager 一样要等）；可以先跑一遍 `sa mcp list` 预先生成/刷新缓存，之后 sa 启动就不用等它了。

## 测试

- `tests/mcp/test_client.py`：新增 2 个测试——npx 卡住（联网查询超时、无 stderr）时提示带 `--prefer-offline` 且不说「再试一次」；非 npx 的慢启动 server 仍保留原来的「再试一次」提示、不带 `--prefer-offline`。
- `tests/mcp/test_manager.py`：新增 9 个测试，覆盖：lazy 且无缓存时照常启动一次并写缓存；eager 不写缓存；有缓存时 server 待命到第一次调用（`to_launch` 为空，不起新进程），工具照样登记好；并行发起的首次调用只真正启动一个进程；`force=True` 让 lazy 的 server 也启动；启动参数（command/args/env/protocol 等）变了缓存作废，只改过滤 / 权限不作废；缓存文件损坏时当没有缓存处理，照常启动并重新写好；真实工具和缓存对不上时标 `stale`、本次会话工具列表不变、缓存更新到下次生效；懒启动的 server 起不来时，启动阶段不受影响，调用时才抛错，且工具仍然保持登记。
- 测试结果：`uv run pytest -q` 604 passed；`uv run ruff check` 全部通过；`uv run ruff format --check` 142 files already formatted（无需改动）。
- 补充人工验证（未写进自动化测试）：`uv run sa mcp list` 能正常写出缓存；REPL 里 `/mcp` 显示 `filesystem ◦ 13 个工具（按需启动）` 且几乎立即进入交互；`uv run sa run` 里模型第一次调用 `mcp__filesystem__list_allowed_directories` 时才真正启动 server，结果正常。

## 相关笔记

- 无（本次是 M5 之后的功能补强，不是独立里程碑，学习笔记在里程碑收尾时统一写）
