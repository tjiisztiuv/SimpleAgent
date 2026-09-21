# M5：手写 MCP 客户端

日期：2026-09-21 · 对应里程碑 M5（MCP 客户端）

M5 之前，模型能用的工具只有进程里写死的 7 个。这一篇记的是把 MCP 接进来时想明白的几件事：
MCP 在 agent 里到底站在哪一层、规范刚换代时客户端该怎么开场、stdio 管道上有哪些坑、
第三方工具怎么融进已有的权限体系，以及 server 进程该归谁管。

代码在 `src/simpleagent/mcp/`，分四层，每层只依赖下一层：

| 文件 | 管什么 | 不管什么 |
|---|---|---|
| `transport.py` | 子进程、按行收发 JSON-RPC、id 配对、超时取消、关闭 | 任何 MCP 方法的含义 |
| `client.py` | 两代协议的开场、`tools/list` 翻页、`tools/call` 结果转文本 | 配置、权限 |
| `tools.py` | 配置 → 子进程环境；MCP 工具 → 注册表里的 `Tool` | 进程什么时候起、崩了怎么办 |
| `manager.py` | 多个 server 并行启动、失败隔离、崩溃后重启、关闭 | 协议细节 |

## 1. MCP 站在哪一层：和 function calling 不是一回事

最容易搞混的一点：**MCP 不替代 function calling，模型根本不知道 MCP 存在**。

```
 LLM  ⇄  function calling  ⇄  Agent（Host）  ⇄  MCP  ⇄  MCP server  ⇄  真实系统
         请求里的 tools 字段                      tools/list             文件 / 日历 / GitHub
         响应里的 tool_calls                      tools/call
```

模型看到的永远是 Chat Completions 请求里的 `tools` 数组。MCP 解决的是另一边：agent 从哪儿
**拿到**工具定义（`tools/list`），以及怎么把调用**转交**出去（`tools/call`）。所以接进来之后，
MCP 工具就是注册表里又多了几个 `Tool`，只是执行函数把参数转发给子进程——agent loop、
权限判定、审批器、输出截断一行都没改。M2 定的「内置工具、MCP 工具……统一注册成 `Tool`」
这条原则，在这里兑现了。

它为什么能成为标准：本质是 LSP 的思路（M 个编辑器 × N 种语言 → M + N），加上工具方自己维护
server、进程隔离让凭据留在 server 里、底层就是 JSON-RPC + stdio 几百行就能实现，再加上
OpenAI / Google / 微软先后跟进形成网络效应。代价也很明显，见第 9 节。

## 2. 规范刚换代：先探测，再决定讲哪一代

动手前查规范，发现 **2026-07-28 版取消了 `initialize` 握手**：

| | 旧协议（2025-11-25 及更早） | 新协议（2026-07-28） |
|---|---|---|
| 开场 | `initialize` 协商版本 + `notifications/initialized` | 没有握手，可选 `server/discover` |
| 版本和能力 | 握手时说一次 | **每个请求**的 `params._meta` 里都带 |
| 状态 | 连接即会话 | 无状态 |
| 结果 | 直接是结果 | 带 `resultType`：`complete` / `input_required` |
| server 反向请求 | 可以（`ping`、`roots/list`…） | 不可以，改成 `input_required` |
| 版本不匹配 | `initialize` 回另一个版本 | 错误码 `-32022`，`data.supported` 列出支持的版本 |

然后实测官方 filesystem server 最新版（2026.8.31）：`server/discover` 回 `-32601`，
`initialize` 协商到 2025-11-25——**生态还停在旧协议**。官方 TS SDK v2 写的 server 默认两代都接，
加 `legacy: 'reject'` 才只讲新协议；官方 TS 客户端默认不探测、直接走旧握手。

所以客户端按规范的建议做成两代都支持（`protocol = "auto" | "modern" | "legacy"`）：

```
server/discover（_meta 里写 2026-07-28）
 ├─ DiscoverResult          → 新协议
 ├─ -32022                  → 从 data.supported 挑两边都支持的最新版本（只剩旧版本就退回）
 ├─ 其他任何 JSON-RPC 错误   → 当旧 server，走 initialize
 └─ 10 秒没回应              → 当旧 server，走 initialize
```

几个细节：

- **不能按某个固定错误码判断旧 server**：规范明说旧 server 对未握手请求的回应五花八门
  （`-32601`、`-32602`，或者干脆不回）。只有认得出的新协议错误（`-32022`）才说明它是新 server。
- **不同时发 `discover` 和 `initialize` 抢时间**：对两代都接的 server，收到 `initialize` 就会把
  整个进程切到旧协议，两个一起发就说不准最后跑在哪一代上。
- **探测只等 10 秒而不是整个启动时限**：规范说「不回应就当旧 server」。等得太久，不理未握手请求的
  旧 server 每次启动都白等；代价是只讲新协议、而且第一次 npx 下载特别慢的 server 会失败一次，
  报错里写明原因，提示可以配 `protocol = "modern"`。
- **两代的差别收在两处**：怎么开场，以及请求要不要带 `_meta`。`tools/list` / `tools/call` 的格式
  两代基本一样，`_request()` 里新协议自动补 `_meta`，旧 server 没有 `resultType` 就按 `complete` 处理。

## 3. stdio 管道上的坑

传输层只有「在管道上可靠地收发 JSON-RPC」这一件事，但坑不少：

1. **多个请求共用一根管道，响应会乱序**。注册表会用 `gather` 并行执行只读工具，所以每个请求带
   自增 id，登记在 `id → Future` 表里，由**常驻的读循环**按 id 交回。不能「一次只允许一个请求」，
   否则并行的只读调用只能排队。实测模型在一轮里并行发了 5 个 `list_directory`。
2. **stderr 必须一直读走**。管道缓冲只有几十 KB，写满后 server 会卡在写日志上，看起来像挂了。
   不丢进 DEVNULL（启动失败时就没有线索），也不继承终端（filesystem server 一启动就打两行，会弄乱
   REPL）：用一个 200 行的环形缓冲接住，报错时附上最后几行。
3. **asyncio 的 `readline()` 默认一行最多 64KB**，server 返回一个大文件就超了。启动子进程时传
   `limit=64MB`；真超过就说明分帧不可信，按连接断开处理。
4. **子进程自成进程组**（`start_new_session=True`，和 bash 工具同一招）：REPL 里 Ctrl+C 的 SIGINT
   发给前台进程组，不单独成组就会顺带打死所有 server；`npx` 还会再拉起 `node`，关闭时要 `killpg`
   整组清掉。
5. **超时和中断都要告诉对方**：发 `notifications/cancelled`。Ctrl+C 的路径上同步写一行就重新抛出，
   不能再 `await`。超时之后才到的迟到响应直接丢掉。
6. **按规范的顺序关**：关 stdin（首选，也是唯一跨平台的退出信号）→ 等 → SIGTERM → 等 → SIGKILL。
7. **进程意外退出时，在途请求立刻失败**，报错带退出码和 stderr 末尾，不用等到超时。
8. `json.dumps` 不带 indent 时会把字符串里的换行转义成 `\n`，天然满足「一条消息不能有裸换行」。

## 4. 协议会话里的几个取舍

- **不声明任何客户端能力**（`capabilities: {}`）。不声明 roots 的原因很具体：filesystem server 收到
  roots 会**替换掉**命令行里给的允许目录，配置里写的目录反而不算数。sampling / elicitation 要一个能问
  用户的界面，和工作台的审批一起做。相应地，新协议的 `input_required` 和 `-32021` 转成可读的错误。
- **`notifications/tools/list_changed` 只记标记，不在会话中途刷新工具列表**：工具一变前缀缓存就失效，
  模型刚看到的工具也可能消失。
- **结果转文本**：Chat Completions 的 `tool` 消息只能放文本。图片 / 音频留一个
  `[图片 image/png，约 12.3 KB，未展示给模型]` 占位——要让模型知道有东西，否则它会以为工具什么都没返回；
  `structuredContent` 和文本都有时只用文本（规范要求 server 同时给一份 JSON 文本，放两遍白占 token）。
- **格式不对的工具跳过一个，不拖垮整个 server**；`tools/list` 最多翻 100 页，防 cursor 转圈。

## 5. 融进已有的权限体系

**命名 `mcp__<server>__<tool>`**：filesystem server 自带 `read_file` / `write_file` / `edit_file`，
和内置工具同名，不加前缀注册表直接报「工具重名」。加了前缀，`sa run --allow mcp__filesystem__write_file`
这种按名字的白名单也直接能用。OpenAI 兼容接口要求函数名 `[A-Za-z0-9_-]{1,64}`，而 MCP 允许 `.`、
最长 128，所以要清洗：不合法字符换 `_`，超长截短加原名哈希，清洗后撞名的也加哈希。真实工具名存在闭包里，
不需要从名字反解析。

**schema 原样交给模型**：`Tool` 新增 `parameters` 字段，有值时 `schema()` 直接用它；参数只由通用的
`McpArgs(RootModel[dict])` 查「是个 JSON 对象」，字段校验交给 server（规范要求 server 必须校验）。
放弃的方案是把 JSON Schema 转成 pydantic 模型（`$ref` / `oneOf` 转换会丢信息，转错了反而拒绝合法参数）。
只做最少的整理：去掉顶层 `$schema`、补 `type: object` 和 `properties`。

**权限来自 annotations + 配置**：

| 情况 | 能否并行 | 权限 |
|---|---|---|
| `readOnlyHint: true` 且 `trust_annotations = true`（默认） | 能 | allow，免确认 |
| 其他 | 不能 | ask |
| 配置里 `permissions = { 工具 = "allow" / "ask" }` | 不变 | 以配置为准 |

规范说 annotations「除非来自可信的 server，否则不可信」——用户亲手写进配置，就是在表达信任；
不信任就 `trust_annotations = false`。确认时的说明也要准确：注册表原来的默认说法是「会改动文件或执行命令」，
放在发邮件的工具上就不对了，所以 `Tool` 加了 `confirm_reason`，MCP 工具按 annotations 自己说明，比如
「来自 MCP server filesystem，server 标注它可能删除或覆盖数据」。

**⚠️ 工作目录边界管不到 MCP 工具**：MCP 工具没有 `scope`，我们不知道它会碰哪些路径。边界由 server
自己负责——给 filesystem server 的目录就是它能碰的全部范围。实测里读 `/etc/hosts` 是 server 自己拒绝的。

**子进程环境用白名单**：只传 `HOME` / `PATH` / `USER` / `LANG` / 代理这些，再加配置里的 `env` 和
`env_vars`。`npx -y` 每次跑的是别人发布的最新版代码，没理由看到 shell 里 export 的所有密钥（官方
Python SDK 也是白名单）。密钥只能写成 `env_vars = ["GITHUB_TOKEN"]`，值从环境变量或 `.env` 取；
`env` 里的键名看起来像密钥就在加载配置时报错，**报错里不回显值**。`args` 不展开 `$VAR`：参数会出现在
`ps` 里。

## 6. server 归谁管：跟着进程走，不跟着会话走

新版规范也说：不要让单个会话或任务决定 stdio 进程的生命周期。于是 REPL 和 `sa run` 各持有一个
`McpManager`，`sa serve` 在 Runner 的后台事件循环里持有一个、所有空间共用（M4 的 daemon 以后照搬）。

- **启动时等所有 server 都有结果再接受第一个问题**。工具列表要在第一次请求前定下来，会话里不再变，
  前缀缓存才能命中。放弃的方案是「后台慢慢起、谁好了就注册谁」。卡住了在 REPL 里按 Ctrl+C 跳过。
- **一个 server 失败只影响它自己**：`McpServer.start()` 不抛异常，原因记在自己身上。
- **崩溃后不另起任务盯着进程，下次调用时再重启**：一启动就崩的 server 不会陷进重启死循环，
  没人用的也不会一直被拉起来。5 分钟内最多重启 3 次；带锁，并发的调用只重启一次。
- **中途崩溃的那次调用不自动重试**：server 可能已经写了一半文件、发出了邮件，再调就重复了。
  告诉模型「可能没执行完，也可能已经生效」，由它决定。
- **启动成功过的 server，工具一直保留**，即使后来崩了、重启也失败了。调用时拿到「重启失败」的明确错误，
  比工具在会话中途突然消失好懂。
- `McpServer` 本身就是第 3 层要的 `ToolCaller`：注册表里的工具调的是它，不是某个具体的 client，
  所以重启换了 client，工具不用重新注册。

## 7. 真实模型实测（deepseek-flash + filesystem server）

`sa mcp list` 显示 13 个工具（禁用了废弃的 `read_file` 别名），schema 约 7.7k 字符——
**比 7 个内置工具加起来（4.3k）还大**，而且每次请求都要带。

**只读任务**「统计 tmp 下每个子目录各有几个 .md」：请求里 20 个工具，模型**一次 MCP 工具都没调**。
先试 `bash`（`sa run` 没 `--allow`，被拒），然后改用内置的 `list_dir` / `glob`，答对了（8 个）。
内置工具排在前面、名字短、描述也和任务贴合，模型自然优先用它们。

**写任务**「把汇总表写进 tmp/sa_mcp_test.md」，只放行 `mcp__filesystem__write_file`：

1. `bash` 被拒 → 调 `mcp__filesystem__list_allowed_directories` 摸清 server 的范围；
2. **同一轮并行发了 5 个 `mcp__filesystem__list_directory`**（都标了只读）；
3. 内置 `write_file` 因为目标在工作目录之外被**边界直接拒绝** → 模型**自己换成**
   `mcp__filesystem__write_file`，白名单放行，写入成功；
4. 用 MCP 的 `read_text_file` 读回来核对，最后在回复里解释了为什么落地用的是 MCP 工具。

一个 `directory_tree` 就返回了 270 行 / 4.7 KB，请求体从 14 KB 涨到 30 KB。前缀缓存一直命中
（比如 7,511 个输入 token 里 7,296 个命中），这正是「工具列表和 system prompt 在会话里不变」换来的。

## 8. 踩到的坑

- **读循环的 `except ValueError` 包得太宽**：本意是接 `readline()` 单行超长抛的 ValueError，结果
  分发代码里别的 ValueError（比如 server 回了非数字的错误码）也被当成「消息超长」。改成只包住 `readline()`。
- **`sa mcp list` 在关闭之后才看有没有失败**：关完所有 server 的状态都是 `closed`，有失败也返回 0。
  测试抓到的，改成关闭前就把结果取出来。
- **两代都接的假 server 不能「看第一条消息就锁定代际」**：客户端先发新协议的 `discover` 被 `-32022`
  拒绝后再发 `initialize`，假 server 必须能切回旧协议。它模仿的是官方 SDK v2 的行为。
- **报错里名字重复**：「连接 MCP server ghost 失败：启动 MCP server ghost 失败：找不到命令」，
  每一层都加前缀就会这样；外层去掉内层的前缀。
- **重启失败后工具会从 serve 的注册表里消失**：serve 每次输入都重建注册表，最初的 `tools()` 只收
  `ready` 的 server，一个 server 崩了、重启失败，下一轮它的工具就没了。改成启动成功过就一直保留。

## 9. 现在的缺口

- **内置工具和 MCP 工具功能重叠时，模型偏向内置的**。要让某个 server 真正被用上，可能得用
  `enabled_tools` 只暴露它独有的工具，或者在 system prompt 里说明分工。
- **上下文开销**：一个 filesystem server 就多了约 2k token 的工具定义。GitHub 这类几十个工具的 server
  会更夸张。M6 的上下文预算、M7 的 Skills 渐进式披露都和这个有关。
- **没接的能力**：roots / sampling / elicitation（需要能问用户的界面）、`notifications/progress`
  （以后接 `ToolContext.emit` 上报进度）、`list_changed` 后的刷新、`idempotentHint` 工具的自动重试。
- **远程 HTTP 和 OAuth**：按 ROADMAP，到时候引入官方 `mcp` SDK。日历、邮件、IM 大多是这类 server。
- **serve 里共享状态**：旧协议的 server 有自己的内部状态（比如浏览器类 server 共用一个浏览器），
  不同空间会共享它。
- **launchd 下的 PATH 很短**：M4 的 daemon 用 launchd 常驻时，`npx` 可能找不到，要在配置里写绝对路径
  或者用 `env` 补 PATH。
