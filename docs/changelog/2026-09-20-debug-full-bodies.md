# debug 新增 full 档：直接看到发给模型的 messages 正文和模型的返回

- 日期：2026-09-20
- 对比基线：`820a6b1`（Merge pull request #2 from shfentmall/feat/debug-mode）
- 对应里程碑：M2 之后的可观测性补强（延续上一批 debug 模式）

## 功能变化

- **新增**：debug 第四档 `full`。前几档只打结构（`role + 字符数`），这一档把**真正发出去的 messages 正文**逐条展开，并把**模型的返回**按 `content` / `reasoning` / `tool_calls` 三块展开。每条正文最多 30 行、单行 200 字符，超出提示「…还有 N 行（全文见 trace）」
- **新增**：请求块末尾列出这一轮带了哪些工具（只有名字，schema 全文去 trace）
- **升级**：`MessageDone` 现在会转给 debug 渲染器。原来它在 REPL / headless 里被提前消费掉，full 档拿不到返回内容
- **升级**：`ApiRequest` 带上 `sent`（实际发送的消息）和 `tool_names`；两个字段都是列表引用，不复制、不额外序列化，产出成本和之前一样
- **重构**：`REASONING_KEY` 和 `message_outline()` 从 `llm/client.py` 挪到 `events.py`。字段名和消息摘要本来就属于事件层，挪过去之后 UI 层算消息大小不必再导入 openai 客户端
- **不变**：`off` / `on` / `verbose` 三档的输出与改动前逐字节一致（测试有断言守着）

## 函数级改动

### `src/simpleagent/events.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `REASONING_KEY` | 移动 | 从 `llm/client.py` 挪来。会话历史、UI 层都要用的字段名，放在事件层 |
| `message_outline()` | 移动 | 同上。原来在 `llm/client.py`，现在和 `ApiRequest` 放在一起 |
| `ApiRequest.sent` | 新增 | `prepare_messages` 之后的消息列表，也就是 API 实际收到的那份。只有 full 档会展开它 |
| `ApiRequest.tool_names` | 新增 | 这一轮携带的工具名清单 |

### `src/simpleagent/llm/client.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `REASONING_KEY`、`message_outline()` | 删除 | 移到 `events.py`，这里改成导入 |
| `collect_tool_names()` | 新增 | 从工具 schema 里取出名字；schema 缺 `function` 时退化成 `?` |
| `LLMClient.stream()` | 修改 | 构造 `ApiRequest` 时补上 `sent` 和 `tool_names` |

### `src/simpleagent/llm/fake.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `FakeLLM.stream()` | 修改 | 同步补 `sent` / `tool_names`，让 fixture 和真实客户端产出同样的事件 |

### `src/simpleagent/ui/debug.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `DEBUG_LEVELS` | 修改 | 加 `full`；`--debug` 的 choices 也跟着放宽 |
| `ROLE_STYLE`、`GREEN` | 新增 | 消息 role 的配色（system 品红 / user 绿 / assistant 青 / tool 黄） |
| `BODY_LINES`、`BODY_WIDTH` | 新增 | full 档正文的行数（30）和单行宽度（200）上限 |
| `debug_level()` | 修改 | `full` 单独打开就生效；`enabled` / `verbose` 的递进关系不变 |
| `prettify_json()` | 新增 | 工具调用参数的 JSON 美化；流式拼到一半或标量值原样返回，不猜 |
| `clip()` | 新增 | 按宽度截断但保留行首缩进（正文块的缩进本身是信息） |
| `body_lines()` | 新增 | 按行切正文；超过 30 行时留开头并提示全文在 trace |
| `content_text()` | 新增 | content 统一转文本，非字符串（多模态之类）按 JSON 展开 |
| `message_body()` | 新增 | 一条消息的正文行；正文为空、或正文之外还带 tool_calls 时补一句说明 |
| `call_lines()` | 新增 | 一次工具调用的展示行：名字一行，参数 JSON 缩进跟在下面 |
| `DebugRenderer.__init__()` | 修改 | `verbose: bool` → `level: str`，派生 `verbose` / `full` 两个属性 |
| `DebugRenderer.on_event()` | 修改 | `MessageDone` 从「只收尾分段」改成走 `message_done()` |
| `DebugRenderer.messages_block()` | 新增 | 请求正文块：逐条 `[i] role · 大小` + 缩进正文，末尾跟工具名 |
| `DebugRenderer.message_done()` | 新增 | 响应正文块：`content` / `reasoning` / `tool_calls` 三块，树形缩进 |

### `src/simpleagent/ui/repl.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `HELP` | 修改 | `/debug` 的档位说明加上 full |
| `Repl.chat()` | 修改 | 构造 `DebugRenderer` 时传 `level=`；debug 开着时把 `MessageDone` 转给 debug 渲染器（原来被这里吃掉） |

### `src/simpleagent/ui/headless.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Headless.run()` | 修改 | 同上：传 `level=`，并把 `MessageDone` 转给 debug 渲染器 |

### `src/simpleagent/config.py` / `config.example.toml`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `DebugConfig.full` | 新增 | 第四档开关，默认关；单独打开就生效（full 隐含前面两档） |

### `src/simpleagent/cli.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `main()` | 修改 | `--debug` 的 help 文本补上 full；choices 跟着 `DEBUG_LEVELS` 自动放宽 |

## 配置与依赖

- `~/.simpleagent/config.toml` 的 `[debug]` 新增可选 `full`（默认 `false`），老配置不用改
- 依赖没有变化：仍然只有 `openai` + `pydantic`，没引入 rich
- **注意**：full 档的正文写 stderr，里面会有工具读到的文件内容。`sa run --debug full 2>debug.log` 存下来的就是一份完整上下文快照，别随手分享。API key 在 header 不在请求体，所以正文里不会有 key

## 测试

- 修改 `tests/test_debug.py`：新增 4 个用例（full 档展开请求与响应正文、on/verbose 不打正文、`prettify_json` 的退化路径、headless 走完 full 的端到端），`debug_level` 的档位断言补 full，`make_debug` 改为按档位构造；共 20 个用例
- 结果：353 passed
- `ruff format` / `ruff check` 通过

## 相关笔记

- 设计方案与本次扩展：`docs/design/debug-mode.md`
