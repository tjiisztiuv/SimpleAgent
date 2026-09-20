# 新增 debug 模式：交互时看清 API 调用与工具调用的过程

- 日期：2026-09-20
- 对比基线：`fe02700`（Merge pull request #1 from shfentmall/claude/space-session-md-render-45e170）
- 对应里程碑：M2 之后的可观测性补强

## 功能变化

- **新增**：debug 模式，三档 `off / on / verbose`。on 档显示每次 API 请求（URL、model、消息条数、工具个数、请求体大小）和响应（状态码、耗时、ttft、token、停在哪个 finish_reason），以及每个工具调用（名字、参数、权限等级、是否写操作、判定结果、耗时、结果行数与大小）；verbose 档再加每轮的消息清单（role + 字符数）和请求开关
- **新增**：工具结果现在带上执行耗时、权限判定结果（allow / ask / deny）、是否被截断落盘
- **新增**：API 的失败和中断也有事件了——`MessageDone` 只在成功时产出，而这两种恰恰最需要看
- **新增**：开关三个入口：`config.toml` 的 `[debug]`、`sa --debug [LEVEL]` / `sa run --debug`、REPL 里的 `/debug [off|on|verbose]`
- **升级**：debug 行写 **stderr**，正文写 stdout，可以 `2>debug.log` 单独存一份过程日志
- **升级**：客户端 SSE 加了 `api_request` / `api_response` 两个帧类型（前端 UI 承接留到后面做）

## 函数级改动

### `src/simpleagent/events.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `ApiRequest` | 新增 | 请求发出前的摘要：step / url / model / messages / tools / payload_bytes / outline / options。不含 header、不含 key、不含请求体正文 |
| `ApiResponse` | 新增 | 请求终态：step / status（ok、error、cancelled）/ elapsed / ttft / status_code / error / usage / finish_reason |
| `ToolCallStart.step`、`permission`、`readonly` | 新增 | 标出属于第几次请求、工具的默认权限等级、是否只读（决定并行还是串行） |
| `ToolResult.duration_ms`、`decision`、`truncated` | 新增 | 含权限判定的执行耗时、实际判定结果、输出是否被截断落盘 |
| `Event` | 修改 | 联合类型加上两个新事件 |

### `src/simpleagent/llm/client.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `LLM.stream()` | 修改 | 加 `step` 参数，让 debug 行能标轮次；`FakeLLM` 同步 |
| `LLMClient.endpoint()` | 新增 | 请求地址，只用于 debug 显示 |
| `LLMClient.request_options()` | 新增 | 影响请求体的非默认开关；`extra_body` 只放键名 |
| `message_outline()` | 新增 | 每条消息的 role + 字符数，verbose 档用来找「哪一条把上下文撑大了」 |
| `LLMClient.stream()` | 修改 | 开头 yield `ApiRequest`；成功、失败、中断都 yield `ApiResponse`。GeneratorExit 期间不 yield——async generator 关闭时 yield 会被判成 `ignored GeneratorExit` |
| `LLMClient._response()` | 新增 | 三种终态统一构造 `ApiResponse` |

### `src/simpleagent/tools/registry.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `ToolRegistry.get()` | 新增 | 给 loop 查工具的 permission / readonly |
| `ToolRegistry.execute()` | 修改 | 计时并把 `decision`、`truncated` 带进 `ToolResult`；内部的 `error()` 多一个 `decision` 参数；审批变量名 `decision` → `answer`，避免和判定结果撞名 |

### `src/simpleagent/agent/loop.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Agent.run()` | 修改 | `for _ in range(...)` → `for step in range(1, ...)`，step 传给 `llm.stream()` 和 `ToolCallStart` |

### `src/simpleagent/serve/frames.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `event_to_frame()` | 修改 | 新增 `api_request` / `api_response` 帧；`tool_call_start` 补 step / permission / readonly，`tool_result` 补 duration_ms / decision / truncated |

### `src/simpleagent/ui/debug.py`（新增）

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `DebugRenderer` | 新增 | 接管工具相关的两类事件（信息比普通行全），debug 行写 stderr；正文、思考内容仍交给 `Renderer` |
| `debug_level()` | 新增 | 配置里的 `enabled` / `verbose` → 一档 debug；verbose 隐含 enabled |
| `supports_color()` | 新增 | 从 `ui/repl.py` 挪过来的公共函数（尊重 `NO_COLOR`） |
| `fmt_bytes()`、`fmt_ms()`、`shorten()` | 新增 | 字节数、耗时、参数预览的格式化 |

### `src/simpleagent/ui/repl.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Renderer.on_event()` | 修改 | 末尾 `else` 改成 `elif isinstance(event, TextDelta)`：未知事件（两个 API 事件）不再被当正文，否则会访问不存在的 `.text` |
| `Repl.__init__()` | 修改 | 加 `err`、`debug` 参数 |
| `Repl.chat()` | 修改 | debug 档位非 off 时走 `DebugRenderer`；统计已在 ApiResponse 行里，不再重复打印 |
| `Repl._set_debug()` | 新增 | `/debug [off|on|verbose]` 的实现 |

### `src/simpleagent/ui/headless.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Headless.__init__()` | 修改 | 加 `err`、`debug` 参数 |
| `Headless.run()` | 修改 | debug 档位非 off 时事件交给 `DebugRenderer`，正文照写 stdout |

### `src/simpleagent/config.py` / `config.example.toml`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `DebugConfig` | 新增 | `enabled` / `verbose` 两个开关，注释里写明输出走 stderr |
| `Config.debug` | 新增 | 默认关闭 |

### `src/simpleagent/cli.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `main()` | 修改 | 顶层与 `run` 子命令各加一个 `--debug [LEVEL]`（子命令用 `SUPPRESS`，别覆盖父级的值） |

### `src/simpleagent/llm/fake.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `FakeLLM.stream()` | 修改 | 加 `step` 参数，和真实客户端一样产出 ApiRequest / ApiResponse；`requests` 记录里多一个 `step` 键 |

## 配置与依赖

- `~/.simpleagent/config.toml` 新增可选的 `[debug]` 段（`enabled`、`verbose`），不写就是关闭，老配置不用改
- 依赖没有变化：仍然只有 `openai` + `pydantic`，ANSI 转义自己写，没引入 rich

## 测试

- 新增 `tests/test_debug.py`：16 个用例，覆盖事件序列与 step 编号、API 失败（status=error / status_code）、中断路径、工具耗时与判定、截断标记、stderr 渲染（含 verbose 只打结构）、凭证不出现在 debug 输出里、配置档位映射、REPL `/debug` 命令、headless 分离输出、SSE 帧
- 修改 `tests/test_agent_loop.py`、`tests/test_llm_client.py`：事件序列断言把两个 API 事件排掉；`test_llm_client` 顺便断言 `ApiRequest` 的字段
- 结果：348 passed（另有 `tests/serve/test_web.py::test_pinned_session_stays_on_top` 是既有的时序 flaky，在 `fe02700` 上重复跑也是 3/5 通过，与本次改动无关）
- `ruff format` / `ruff check` 通过

## 相关笔记

- 设计方案与实施记录：`docs/design/debug-mode.md`
