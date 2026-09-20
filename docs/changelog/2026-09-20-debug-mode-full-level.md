# debug 模式加第四档 full：看清发给 LLM 的具体输入和模型返回了什么

- 日期：2026-09-20
- 对比基线：`820a6b1`（Merge pull request #2 from shfentmall/feat/debug-mode）
- 对应里程碑：M2 之后的可观测性补强

## 功能变化

- **新增**：debug 第四档 `full`（`off / on / verbose / full`）。full 档在 verbose 的基础上，再打出**实际发给模型的请求体正文**（每条消息的 role、字符数、正文预览、工具调用参数）和**模型返回的结构**（思考、正文、工具调用、usage）；verbose 档的语义不变，仍然只打结构不打正文
- **升级**：full 档只打**新增**的消息，跨轮不会把整段历史重打一遍——渲染器记住上次打到第几条，之后只打尾部新增的，前面用「（前 N 条同上次）」带过；`/clear`、换会话、条数变少或用 `/debug` 切换档位时会重新打全量。工具清单同理，签名没变时只报个数，不重复整份列出
- **升级**：工具调用只打名字和参数字段名（如 `glob(pattern,path,limit)`），不打完整 JSON Schema（几千字符且基本不变，要看仍然去 `traces/`）；工具参数如果不是合法 JSON，会原样预览并标 `⚠ 参数不是合法 JSON`——这正是最容易被旧版单行 `shorten()` 预览糊过去的情况
- **升级**：`api_response()` 先把正文流式输出收尾（`inner.end()`）再写 debug 行，修掉了之前流式输出末尾和 `⟨ 200` 挤在同一行的问题
- **修复**：`debug_level()` 原来 `enabled=false` 时直接返回 `off`，和文档里「verbose 隐含 enabled」的说法对不上（`config.example.toml` 里注释掉的示例正好是这种写法，等于白写）。现在改成 `full` 隐含 `verbose`、`verbose` 隐含 `enabled`，行为和文档一致
- **不变（刻意）**：客户端 SSE 帧（`serve/frames.py`）没有改，full 档的正文不会推给前端——一轮请求体可能有几十上百 KB，每步推送会把总线撑爆，客户端 UI 也还没设计怎么承接这块；桌面端要看正文，以后再单独设计按需拉取的接口

## 函数级改动

### `src/simpleagent/events.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `ApiRequest.payload` | 新增 | 实际发出的请求体（含 messages / tools，不含 header、不含 key），挂的是引用，不拷贝也不额外序列化；off 档零成本，中途切到 `/debug full` 也立刻有东西看。只有 full 档会读它 |

### `src/simpleagent/llm/client.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `LLMClient.stream()` | 修改 | 构造 `ApiRequest` 时多传一个 `{**request, **self.profile.extra_body}`，作为 `payload`，和落盘 trace 的内容保持一致（`extra_body` 会被 SDK 合并到顶层） |

### `src/simpleagent/llm/fake.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `FakeLLM.stream()` | 修改 | 同步补上 `ApiRequest` 的 `payload` 参数（`options` 传 `{}`，`payload` 传 `{"model", "messages", "tools"}`），保持和真实客户端字段对齐，测试脚本不用改造型 |

### `src/simpleagent/ui/debug.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `DEBUG_LEVELS` | 修改 | 三档改四档：`("off", "on", "verbose", "full")` |
| `debug_level()` | 修改 | `config.debug.full` → full；`verbose` → verbose；否则看 `enabled`。顺带修了「`enabled=false` 直接短路」的旧 bug |
| `preview_text()` | 新增 | 正文预览：先按 `body_chars` 截字符，再按 `BODY_LINES`（12 行）截行数，截掉了补一行说明原文多长；`limit`/`max_lines` 传 0 表示不截 |
| `pretty_json()` | 新增 | 工具参数：能解析就格式化（短的压成一行，长的缩进展开并走 `preview_text`），解析失败原样预览并返回 `False`（供调用方标 `⚠`） |
| `format_tool_call()` | 新增 | 一个 `tool_call` → 抬头行（工具名 + id）+ 缩进的参数行；参数不合法时抬头加 `⚠ 参数不是合法 JSON` |
| `format_message()` | 新增 | 请求体里一条消息 → 抬头行（序号/role/字符数/行数/tool_calls 个数）+ 缩进的正文和工具调用 |
| `format_tools()` | 新增 | 工具清单 → `名字(参数字段名,...)` 的列表，按 `WRAP_WIDTH`（88）折行 |
| `DebugState` | 新增（dataclass） | 跨轮记住 full 档已经打过的消息条数（`messages`）和工具清单签名（`tools`）；由前端（Repl）持有并在换档、新对话时重建 |
| `DebugRenderer.__init__()` | 修改 | 参数 `verbose: bool` 换成 `level: str`（默认 `"on"`）+ `body_chars: int`（默认 `BODY_CHARS`=600）+ `state: DebugState \| None`；内部据此派生 `self.verbose`（verbose/full 都算）和 `self.full` |
| `DebugRenderer.on_event()` | 修改 | `MessageDone` 不再简单地转给 `inner.end()`：改成先 `inner.end()`，full 档下再调用 `response_body()` 打模型返回的结构 |
| `DebugRenderer.request_body()` | 新增 | full 档专用：按 `DebugState.messages` 只打新增消息（`format_message`），工具清单签名没变时只报个数，变了才用 `format_tools()` 重新列出并更新签名 |
| `DebugRenderer.response_body()` | 新增 | full 档专用：从 `MessageDone.message` 里取思考（`REASONING_KEY`）、正文、`tool_calls`、`event.usage`，分别用 `preview_text()` / `format_tool_call()` 打出来 |
| `DebugRenderer.api_response()` | 修改 | 开头加 `self.inner.end()`，先收尾流式正文再写 debug 行，避免和响应状态行挤在一起 |
| `DebugRenderer.api_request()` | 修改 | full 档不再打 verbose 的 `event.outline` 摘要行（每条消息已经自带字符数），改为额外调用 `request_body()` |

### `src/simpleagent/ui/repl.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Repl.__init__()` | 修改 | 新增 `self.debug_state = DebugState()`，供 full 档跨轮复用 |
| `Repl.chat()` | 修改 | 构造 `DebugRenderer` 时传 `level=self.debug`、`body_chars=self.config.debug.body_chars`、`state=self.debug_state`；`MessageDone` 处理逻辑调整为：debug 开着就交给 `debug.on_event(event)`（由它负责收尾和 full 档打印），否则走原来的 `renderer.end()` + 打统计行 |
| `Repl._set_debug()` | 修改 | 切换档位时重建 `self.debug_state = DebugState()`，避免换档后 full 档误判「前面已经打过」 |
| `HELP` | 修改 | `/debug` 帮助文案加上 `full` |

### `src/simpleagent/ui/headless.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Headless.run()` | 修改 | 构造 `DebugRenderer` 时传 `level=self.debug`、`body_chars=self.config.debug.body_chars`（不再传旧的 `verbose=` 参数）；`MessageDone` 分支改成 debug 开着就 `debug.on_event(event)`，否则打统计行 |

### `src/simpleagent/config.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `DebugConfig.full` | 新增 | `bool = False`，full 档开关，注释写明会把系统提示、历史消息、工具结果打到 stderr，日志别随手外传 |
| `DebugConfig.body_chars` | 新增 | `int = Field(600, ge=0)`，full 档每段正文的字符上限，0 = 不截断 |

### `src/simpleagent/cli.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `main()` | 修改 | `--debug` 参数的 help 文案从「on / verbose」改成「on / verbose / full」；`choices` 本来就取自 `DEBUG_LEVELS`，不用改 |

### `src/simpleagent/serve/frames.py`

无改动——full 档的正文不进 SSE 帧，理由见上面「功能变化」。

## 配置与依赖

- `~/.simpleagent/config.toml` 的 `[debug]` 段新增两个可选项：`full`（默认 `false`）、`body_chars`（默认 `600`，0 表示不截断）。不写就是关闭，老配置不用改，`config.example.toml` 已同步注释
- 依赖没有变化
- **需要手动处理**：无

## 测试

- `tests/test_debug.py`：新增 10 个用例（`test_full_prints_request_body_and_tools`、`test_full_prints_only_new_messages_next_step`、`test_full_lists_tools_once_until_they_change`、`test_full_truncates_long_content`、`test_full_body_chars_zero_keeps_everything`、`test_full_prints_response_structure`、`test_full_flags_invalid_tool_arguments`、`test_lower_levels_never_print_body[on/verbose]`（参数化 2 个）、`test_repl_full_debug_prints_context_incrementally`），覆盖 full 档的请求体/工具清单打印、跨轮只打新增内容、工具清单签名去重、`body_chars` 截断与 0 表示不截、响应结构打印、非法 JSON 参数标注、低档位不打正文、REPL 里切到 full 后的增量行为；该文件现共 26 个用例（对比基线 `820a6b1` 时是 16 个，已用 `git worktree` 拉出对比确认）
- 全量 `uv run pytest -q`：359 passed
- `uv run ruff check`：All checks passed
- `uv run ruff format --check`：113 files already formatted

## 相关笔记

- 设计取舍与实施记录：`docs/design/debug-mode.md` 第 10 节「追加：`full` 档（看清具体输入和返回）」
