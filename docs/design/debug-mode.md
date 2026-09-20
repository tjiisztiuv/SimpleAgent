# Debug 模式设计方案

目标：在交互（REPL / `sa run`）时能一眼看清一轮对话里发生的三类动作——**API 调用**、**模型发起的 function call**、**工具的实际执行**——并且在需要时看到请求体规模、权限判定、耗时这些细节。

---

## 1. 设计约束与取舍

| 约束 | 来源 | 结论 |
|---|---|---|
| 核心 loop 与前端解耦，前端只消费事件流 | 架构原则 1 | debug 信息也走事件流，不在核心里 `print` |
| 事件可序列化，方便推给客户端 | 接口约定 5 | 新增事件必须是 dataclass、能转 JSON |
| 运行时依赖只有 `openai` + `pydantic` | 架构原则 7 | **不引入 rich**，自己写 ANSI 转义 |
| API key 不出现在代码、日志、trace | 安全约定 | debug 行只显示 URL path、model、字节数；不显示 header、不显示请求体正文 |
| M2+ 功能分步确认 | AGENTS.md 2.1 | 拆成 5 步（本次一次性实施，每步的取舍仍按上面的顺序决定） |

**取舍 1：debug 事件始终产出，开关放在渲染层。**
事件是 dataclass，产出成本只有一次 `json.dumps` 算字节数（一轮一次，毫秒级，相对 1s 的网络请求可忽略）。这样 `/debug` 能在会话中途开关，headless、未来的客户端也能拿到同一份信息。若把开关放进核心，中途切换就拿不到已发生的记录了。

**取舍 2：能扩字段就不加事件类型。**
`ToolResult` 已经覆盖「工具执行完」这个时机，加 `duration_ms` / `decision` 比新增 `ToolCallEnd` 更简单，SSE 帧的映射也要跟着改一处而不是两处。

**取舍 3：API 的失败/中断也要有事件。**
`MessageDone` 只在成功时产出，而 debug 最需要看的恰恰是失败和 Ctrl+C。新增 `ApiResponse` 覆盖三种终态：`ok` / `error` / `cancelled`。

---

## 2. 事件层改动（`events.py`）

### 2.1 新增两个事件

```python
@dataclass
class ApiRequest:
    """一次 LLM 请求发出前。只带摘要：不含 header、不含 key、不含完整请求体。"""

    step: int  # 本轮对话的第几次请求，从 1 开始
    url: str  # https://api.deepseek.com/v1/chat/completions
    model: str
    messages: int  # 请求体里的消息条数
    tools: int  # 携带的工具个数
    payload_bytes: int  # 请求体序列化后的字节数
    # 消息清单：role + 字符数，verbose 档用来找「哪一条把上下文撑大了」
    outline: list[dict[str, Any]] = field(default_factory=list)
    # 非默认的协议开关；extra_body 只放键名，值可能是厂商私有参数
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class ApiResponse:
    """一次 LLM 请求的终态；成功、失败、中断都会产出。"""

    step: int
    status: str  # "ok" | "error" | "cancelled"
    elapsed: float  # 秒
    ttft: float | None = None  # 首个 token 延迟
    status_code: int | None = None  # HTTP 状态码；连不上时为 None
    error: str | None = None  # "APIConnectionError: ..." 之类
    usage: Usage | None = None  # 成功时才有
    finish_reason: str | None = None
```

### 2.2 给现有事件补字段（全部带默认值，向后兼容）

```python
@dataclass
class ToolCallStart:
    call_id: str
    name: str
    arguments: str
    step: int = 0  # 新增：属于第几次请求，用来和 API 行对齐
    permission: str = "allow"  # 新增：工具的默认等级 allow / ask / deny
    readonly: bool = True  # 新增：决定这批调用是并行还是串行


@dataclass
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False
    duration_ms: float = 0.0  # 新增：含权限判定的执行耗时
    decision: str | None = None  # 新增："allow" / "ask" / "deny"
    truncated: bool = False  # 新增：输出是否被截断并落盘
```

`Event` 联合类型同步更新。

---

## 3. 产出方改动

### 3.1 `llm/client.py`

`LLM` Protocol 的 `stream` 加一个参数，让 debug 行能标出轮次：

```python
def stream(
    self,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    step: int = 0,
) -> AsyncIterator[Event]: ...
```

`LLMClient.stream` 里：

```python
url = urljoin(self.profile.base_url.rstrip("/") + "/", "chat/completions")
payload_bytes = len(json.dumps(request, ensure_ascii=False).encode("utf-8"))
yield ApiRequest(
    step,
    url,
    self.profile.model,
    len(request["messages"]),
    len(tools or []),
    payload_bytes,
    options,
)
```

`tracer` 已经有 `raw_chunks` 的先例，`options` 里放 `stream_usage` / `parallel_tool_calls` / `extra_body` 的键名（值可能敏感，只放键）。

`except BaseException` 分支改成**先 yield 一个 `ApiResponse(status=...)` 再 raise**——generator 的 except 块里可以 yield，这样 Ctrl+C 也能看到这次调用花了多久、断在哪。成功路径在 `MessageDone` 之前 yield `ApiResponse(status="ok", ...)`。

`llm/fake.py` 的 `FakeLLM.stream` 同步加 `step: int = 0` 参数（忽略即可，或记进 `self.requests` 供测试断言）。

### 3.2 `tools/registry.py`

`execute()` 里包一层计时，并把判定结果带出来：

```python
start = time.monotonic()
...


def error(message: str, *, decision: str | None = None) -> ToolResult:
    return ToolResult(
        call_id,
        name,
        f"错误：{message}",
        is_error=True,
        duration_ms=(time.monotonic() - start) * 1000,
        decision=decision or (judgment.decision.value if judgment else None),
    )
```

- `judgment` 在参数校验之后才有，所以校验失败的两条路径 `decision=None`
- `DENY` 路径带上 `decision="deny"`；`ASK` 被拒绝带 `"ask"`，被批准也带 `"ask"`——这个信息本身有价值（说明这一刀卡在审批上）
- 截断标记：`if tool.truncate_output: before = len(content); content = self.trim(...); truncated = len(content) < before`

### 3.3 `agent/loop.py`

```python
for step in range(1, self.max_steps + 1):
    ...
    async for event in self.llm.stream(request, tools=self.tools.schemas(), step=step):
        yield event
    ...
    yield ToolCallStart(
        call.get("id") or "",
        function.get("name") or "",
        function.get("arguments") or "",
        step=step,
        permission=tool.permission if (tool := self.tools.get(name)) else "allow",
        readonly=self.tools.is_readonly(name),
    )
```

`ToolRegistry` 加一个 `get(name) -> Tool | None`（现在只有 `is_readonly` 间接暴露）。

### 3.4 `serve/frames.py`（可选，P2）

```python
if isinstance(event, ApiRequest):
    return Frame(session_id, "api_request", {...})
if isinstance(event, ApiResponse):
    return Frame(session_id, "api_response", {...})
```
`ToolResult` 帧的 payload 补 `duration_ms` / `decision`。前端 UI 承接留到后面做，第一步先不碰前端。

---

## 4. 开关：配置 + CLI + REPL 命令

`config.py`：

```python
class DebugConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    verbose: bool = False  # 额外显示消息清单（role + 字节数）、请求体片段
```

`Config` 加 `debug: DebugConfig = Field(default_factory=DebugConfig)`；`config.example.toml` 加一段带注释的示例。

三档语义：

| 档 | 显示内容 |
|---|---|
| off（默认） | 和现在完全一样 |
| on | API 行（URL / model / msgs / tools / 大小 → 状态码 / 耗时 / ttft / token）、工具行（名 + 参数 + 权限 + 耗时 + 结果行数）、错误高亮 |
| verbose | 再加每轮的消息清单（role + 字符数）、请求体前 N 字节（脱敏）、被截断时落盘路径 |

入口：

- `sa --debug` / `sa run --debug`（子命令用 `default=argparse.SUPPRESS`，别覆盖父级）
- REPL 命令 `/debug [on|off|verbose]`，不带参数显示当前档位；`HELP` 加一行
- `Repl.__init__` 加 `debug: bool | None = None`（`None` = 听配置的），`Headless.__init__` 同理

---

## 5. 渲染：新增 `ui/debug.py`

现有 `Renderer` 只有 `DIM/RED/BOLD/RESET` 四个样式，debug 需要区分 API / 工具 / 权限三类，扩展一个调色板：

```python
CYAN, YELLOW, MAGENTA, GREEN = "\033[36m", "\033[33m", "\033[35m", "\033[32m"
```

| 元素 | 样式 |
|---|---|
| API 请求 / 响应 | 青色 + 粗体徽章 `⟩API` `⟨200` |
| 工具名 | 黄色 + 粗体 |
| 权限判定 allow / ask / deny | 灰 / 品红 / 红色 |
| 耗时、字节数、行数 | 灰色 |
| 工具出错、API 失败 | 红色 |
| 轮次分隔 | 灰色细线 `── 轮 2 ──` |

实现成一个子类，正文渲染仍走 `Renderer`，只接管新事件和工具行：

```python
class DebugRenderer(Renderer):
    def __init__(self, out: TextIO, color: bool, show_reasoning: bool, verbose: bool = False): ...

    def on_event(self, event: Event) -> None:
        if isinstance(event, ApiRequest):
            self.api_request(event)
        elif isinstance(event, ApiResponse):
            self.api_response(event)
        else:
            super().on_event(event)

    def tool_start(self, event: ToolCallStart) -> None: ...  # 覆盖：加轮次、权限标记
    def tool_result(self, event: ToolResult) -> None: ...  # 覆盖：加耗时、行数、截断提示
```

`Repl.chat()` 里按当前档位选渲染器，其余代码不动。非 debug 时行为与现在逐字节一致（颜色、分段逻辑都在 `Renderer` 里）。

headless 同样支持：`sa run --debug` 时输出同样的行，颜色按 `isatty` 判断，不强制。

---

## 6. 验证

新增 `tests/test_debug.py`，用 `FakeLLM` + 临时 cwd，不联网：

1. **事件序列**：跑一轮「文本 → 工具调用 → 文本」，断言事件顺序为
   `ApiRequest(1) → ... → ApiResponse(1,"ok") → MessageDone → ToolCallStart(step=1) → ToolResult → ApiRequest(2) → ApiResponse(2,"ok")`
2. **失败路径**：脚本里塞一个 `openai.APIStatusError`，断言有 `ApiResponse(status="error", error=...)`，`status_code` 正确
3. **中断路径**：`agent.cancel()` 后有 `ApiResponse(status="cancelled")`
4. **工具字段**：`ToolResult.duration_ms > 0`；越权路径 `decision == "deny"`；长输出 `truncated is True`
5. **渲染**：`DebugRenderer` 用 `StringIO` + `color=False` 输出，断言含 URL、工具名、耗时；断言输出里**不含** API key（拿一份带 `sk-` 的假 key 走一遍）
6. **回归**：`uv run pytest` 全绿；`ruff format` / `ruff check` 无新增告警

手动验收：

```bash
uv run sa --debug            # 交互里 /debug verbose 切到详细档
> 看看 src 里有多少个 py 文件
uv run sa run --debug "..."  # headless 同样输出
```

---

## 7. 实施步骤（逐步确认）

| 步骤 | 内容 | 改动文件 |
|---|---|---|
| 1 | 事件层：新增 `ApiRequest` / `ApiResponse`，给 `ToolCallStart` / `ToolResult` 补字段 | `events.py`（+ 可选 `serve/frames.py`） |
| 2 | 产出方：client 发 API 事件、registry 记耗时与判定、loop 带 step | `llm/client.py`、`llm/fake.py`、`tools/registry.py`、`agent/loop.py` |
| 3 | 开关：`[debug]` 配置、`--debug` CLI、`/debug` REPL 命令 | `config.py`、`config.example.toml`、`cli.py`、`ui/repl.py`、`ui/headless.py` |
| 4 | 渲染：`ui/debug.py` 调色板 + `DebugRenderer` | 新增 `ui/debug.py`，`ui/repl.py`、`ui/headless.py` 接线 |
| 5 | 测试 + 变更记录 | `tests/test_debug.py`、`docs/changelog/` |

每步先讲解、你确认后再改代码。第 1、2 步纯加字段和事件，不影响现有行为；第 3、4 步才碰到用户界面。

## 8. 三个待定问题的结论

1. **verbose 只打结构，不打正文**：`msgs system 412B · user 28B · assistant 96B · tool 312B`，
   外加 `opts stream_usage=true · parallel_tool_calls=true · extra_body=thinking`（extra_body 只放键名）。
   要看请求体全文去 `traces/`。
2. **debug 行走 stderr，正文走 stdout**：这样 `sa run --debug 2>debug.log` 能把过程单独存一份，
   正文仍可以管道给别的程序。代价是 stdout 通常是块缓冲，写 debug 行前要先 `flush()` 一次，
   否则两个流合起来看会乱序（`DebugRenderer._write` 里做了）。
3. **客户端帧映射同步加**：`api_request` / `api_response` 两个帧，ToolCallStart / ToolResult 帧
   补上新字段。前端 UI 承接留到后面做，帧类型先占好位。

## 9. 实施记录

| 步骤 | 文件 | 状态 |
|---|---|---|
| 1 事件层 | `events.py`、`serve/frames.py` | ✅ |
| 2 产出方 | `llm/client.py`、`llm/fake.py`、`tools/registry.py`、`agent/loop.py` | ✅ |
| 3 开关 | `config.py`、`config.example.toml`、`cli.py`、`ui/repl.py`、`ui/headless.py` | ✅ |
| 4 渲染 | 新增 `ui/debug.py` | ✅ |
| 5 测试 | 新增 `tests/test_debug.py`（16 个用例） | ✅ |

实现与方案的出入：

- `ApiResponse` 比方案里多了 `usage` 和 `finish_reason`：统计行一次写完，debug 模式下不再
  重复打印 `MessageDone` 的那行统计。
- `ApiRequest` 多了 `outline`（role + 字符数），这是 verbose 档要显示的消息清单。
- `ToolRegistry` 加了 `get(name)`，`is_readonly` 保持原样；未知工具的 `permission` 按 `allow` 上报。
- 中断路径的 `ApiResponse(status="cancelled")` **可能发不出来**：异步生成器在被取消的那一刻
  `yield` 会再抛一次 `CancelledError`。client 里把这次 `yield` 包在 `try/except` 里吞掉，
  保证原异常照常传播；测试对此用宽松断言（凡是发出的都标成 cancelled，绝不标成 ok/error）。
