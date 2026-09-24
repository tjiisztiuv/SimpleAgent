# 架构设计

## 选型

| 项 | 选择 | 理由 |
|---|---|---|
| 语言 | Python 3.12 + uv | LLM / MCP 生态最全，读源码学习成本低 |
| 模型接入 | 只接 OpenAI 兼容协议（Chat Completions） | 一套协议覆盖 DeepSeek / GLM / Qwen / Ollama 等，用 `base_url` 切换 |
| 实现方式 | 从零手写 agent loop，只依赖官方 `openai` SDK | 每个机制都自己写，学习价值最高 |
| 首要场景 | 个人自动化 | 定时任务、整理本地文件、通过 MCP 接日历/邮件/IM |

## 设计原则

1. **核心 loop 和前端解耦**：`Agent.run()` 返回异步事件流（`AsyncIterator[Event]`）。交互式 REPL、单次命令 `sa run`、定时 daemon 都只是事件流的消费方，所以同一个 agent 既能对话，也能被定时触发。
2. **内部消息直接用 OpenAI Chat Completions 格式**（dict），不再另造抽象。各家的差异在 profile 的 `quirks` 里处理。
3. **全程可观测**：每次 LLM 请求的完整请求体和响应都写到 `traces/`，包括失败和中断的请求。这样可以直接看到真实发出去的上下文。
4. **注册式扩展**：内置工具、MCP 工具、Skills、子 agent 统一注册成 `Tool`；新 feature 用配置开关接入，方便 A/B 对比。
5. **确定性测试**：`FakeLLM` 按脚本返回响应，不联网也能测 loop、权限、压缩等逻辑。客户端测试用 `httpx2.MockTransport` 模拟 SSE，走真实的 SDK 解析路径。
6. **全异步**（asyncio + `AsyncOpenAI`）：并行工具调用、MCP stdio、调度器、流式输出都需要。
7. **依赖尽量少**：运行时只依赖 `openai`、`pydantic`（openai 已依赖）和 `croniter`（M4，算 cron 的下次触发时间）。CLI 用标准库 `argparse`。

## 整体架构

```
 ┌──────────────────── 前端（消费事件流）────────────────────┐
 │  REPL 交互 (sa)    单次 headless (sa run)    定时 daemon (sa daemon) │
 └────────────────────────────┬─────────────────────────────┘
                              │ run(session, input) -> AsyncIterator[Event]
                       ┌──────▼──────┐   hooks
                       │ Agent Loop  │──────────► trace / 日志 / 通知
                       └──┬────┬───┬─┘
         ┌────────────────┘    │   └────────────────┐
  ┌──────▼───────┐     ┌───────▼──────┐     ┌───────▼────────┐
  │ Context 管理  │     │  LLM Client  │     │  Tool Runtime  │
  │ prompt 组装   │     │ OpenAI 兼容   │     │ 解析→权限→执行 │
  │ 预算/清理/压缩 │     │ 多 profile    │     │ →超时→截断      │
  └──────┬───────┘     └──────────────┘     └───────┬────────┘
  ┌──────▼───────┐                        ┌─────────┼─────────┬──────────┐
  │ Session JSONL │                     内置工具   MCP 工具   Skills   子 agent
  └──────────────┘
```

### Agent loop（M2，主体已实现，见 `agent/loop.py`）

下面是设计草图。M6 没有单独做 `context.build`：请求还是 system prompt + 历史，预算、清理、压缩在每次请求前原地改 Session（见「上下文管理」）。

```python
async def run(self, session, user_input) -> AsyncIterator[Event]:
    session.append({"role": "user", "content": user_input})
    for step in range(self.max_steps):
        async for ev in self.reduce_context(session):  # M6：超预算时先压缩 / 清理，原地改 Session
            yield ev  # ContextEdited
        messages = [system, *session.messages]
        async for ev in self.llm.stream(messages, tools=self.tools.schemas()):
            yield ev  # TextDelta / ReasoningDelta / MessageDone
        msg = ev.message  # 流结束时拼好的 assistant 消息
        session.append(msg)
        if not msg.get("tool_calls"):
            yield TurnEnd()
            return
        results = await asyncio.gather(*(self.tools.execute(tc, ctx) for tc in msg["tool_calls"]))
        for r in results:
            yield ToolResult(r)
            session.append(r.as_message())
    yield MaxStepsReached()
```

要点：
- 流式 `tool_calls` 按 `index` 分片到达，要自己拼（`StreamAccumulator` 已实现）
- 模型给出非法 JSON 参数时，把错误作为 tool 结果回给模型自我纠正，不抛异常（`ToolRegistry.execute`：未知工具、非法 JSON、参数校验失败、工具异常都转成 `错误：...` 文本）
- Ctrl+C 或 API 出错时，给还没返回结果的 tool_call 补“执行被中断”结果，否则下一次请求会被 API 拒绝；已输出的部分正文保留，这一轮什么都没留下就撤回用户消息（`Agent._repair`）
- 同一条消息里的多个 tool_call：全是只读工具就 `asyncio.gather` 并行；只要有一个写操作（`Tool.readonly=False`，目前是 `write_file` / `edit_file` / `bash`）就按原顺序依次执行，避免“先写 A 再读 A”读到旧内容或两个写互相覆盖。判断在 `ToolRegistry.execute_many` 里
- 工具输出过长：完整内容落盘（`~/.simpleagent/tool_outputs/`，保留 7 天），只给模型返回开头部分和文件路径，截断统一在 `ToolRegistry.trim` 里做，行数和字符数两个上限都要满足；自己分页的工具（`read_file`）用 `truncate_output=False` 跳过
- 含写操作的一批调用串行执行时，每完成一个就写进历史：中途被中断，已经执行完的调用保留真实结果

### Tool 抽象（M2）

```python
class ListDirArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(".", description="要列出的目录，绝对路径或相对工作目录的路径")
    depth: int = Field(2, ge=1, le=5, description="展开层数，1 表示只列直接子项")


@tool(name="list_dir", description="...")  # readonly=True、permission="allow"
async def list_dir(args: ListDirArgs, ctx: ToolContext) -> str: ...


@tool(
    name="write_file",
    description="...",
    readonly=False,
    permission="ask",  # M3：改动前要先问
    scope=lambda args, ctx: Scope(paths=(ctx.resolve(args.path),)),  # M3：报告改动了哪里
)
async def write_file(args: WriteFileArgs, ctx: ToolContext) -> str: ...
```
- schema 由 `Args.model_json_schema()` 生成，去掉 pydantic 自动加的 `title`，包装成 `{"type": "function", "function": {...}}`
- 可以预期的失败抛 `ToolError`，消息原样回给模型；相对路径用 `ctx.resolve()` 基于 `ctx.cwd` 解析
- `ToolContext` 有 `cwd` 和 `output_dir`；`ctx.save_output(content, name)` 把完整输出落盘。后续加 session、approver（审批器）、进度上报、中断信号
- `Tool.readonly` 标记这个工具会不会改动外部状态，决定同一批调用是并行还是串行
- `Tool.permission` 是默认权限等级（`allow` / `ask` / `deny`），`Tool.scope` 报告这次调用会改动
  哪些绝对路径、要跑什么命令。两者都由注册表在执行前交给权限判定器，`Tool` 自己不做判断
- `Tool.parameters`（M5）：现成的 JSON Schema，给了就原样交给模型，不再从 `args_model` 生成。
  MCP 工具用它，参数只由通用的 `McpArgs` 查「是个 JSON 对象」，字段校验交给 server。
  `Tool.confirm_reason`（M5）：需要确认时给人看的说明，没有就用默认的「会改动文件或执行命令」
- **审批器不再挂在 ToolContext 上**：W2 曾经在 `ToolContext` 上放过 `approver`，
  引入权限判定器之后它成了第二条审批通道，M3 删掉了。审批统一由 `ToolRegistry` 负责：
  先判定，判定结果是 ask 才调审批器
- 内置 7 个工具：`list_dir` / `read_file`（带行号、offset+limit，流式读取，按行数和约 3 万字符自己分页）/ `write_file`（整篇写入、自动建目录）/ `edit_file`（唯一匹配或 `replace_all`，保留原换行符，返回 unified diff）/ `glob`（`*`、`?`、`**` 自己转正则）/ `grep`（正则搜内容，跳过二进制和大文件）/ `bash`（`create_subprocess_shell`、自成进程组，超时 / 输出超过 10MB / Ctrl+C 时杀掉整组；stderr 合并进 stdout、stdin 是 DEVNULL；环境变量去掉各 profile 的 `api_key_env`）。目录遍历、忽略清单、按 `\n` 分行、可取消的线程执行共用 `tools/walk.py`
- 审批器由前端注入（M3 已实现，见「权限」章节）：REPL 版读一行输入（y / a / 其他键拒绝）；
  headless 版按 `--allow` 白名单判断，需要 ask 的一律拒绝，并把拒绝原因回给模型；
  客户端版推 SSE 帧后挂起等回调

### 权限（M3，`permissions.py`）

拆成两层：**判定**（这次要不要问）和**询问**（这次让不让）。

```
tool_call → Registry.judge() → Policy.decide() ─┬─ ALLOW → 执行
                                                ├─ DENY  → 拒绝原因回给模型
                                                └─ ASK   → Approver.request() → 允许 / 拒绝
```

- `Policy` 是纯函数，无 IO。判定顺序固定：**工具禁用 → 路径越界 → bash 危险命令 → 默认等级**。
  越界和危险命令必须排在默认等级之前，否则工具只要声明 `permission="allow"` 就能穿透边界。
- `Scope.paths` 只放**会被改动**的绝对路径；只读工具不报，因此不受工作目录边界限制
  （读 `~/.zshrc` 是日常需求，为它弹一次确认不划算）。`../` 要在 scope 里就 `resolve()` 掉。
- bash 的危险命令清单刻意很窄：只拦「问了也不该答应」的——`rm -rf` 指向家目录 / 系统根目录 /
  整个工作目录，`mkfs` / `dd` / `fdisk` / `diskutil`，`shutdown` / `reboot`，fork bomb，
  管道直接喂解释器（远程脚本执行），写裸块设备。可弥补的（`rm -rf node_modules`、装错包、
  push 错分支）照常走 ask。拦太宽，模型会学着绕开审批，反而更危险。
- 三种审批器共用同一个异步接口，保住「审批器是异步接口」那条约定：

  | 实现 | 场景 |
  |---|---|
  | `ConsoleApprover`（`ui/approve.py`） | REPL：y / a（本次会话都允许）/ 其他键拒绝 |
  | `WhitelistApprover` | `sa run` 和以后的 `sa daemon`：名单外一律拒绝，原因回给模型 |
  | `APIApprover`（`serve/approval.py`） | 桌面客户端：推一帧 SSE 后 `await Future` |

  危险判定放在 Policy 里，是三个前端共享的底线：定时任务也绕不过去。

### 会话持久化（M3，`agent/session.py`）

`sessions/<id>.jsonl`，每行一条**操作记录**而不是一条消息：

```
{"op":"meta", ...}   {"op":"append","message":{...}}   {"op":"truncate","n":5}   {"op":"stats",...}
{"op":"replace","changes":[[3,{...}]]}   M6：原地替换几条消息（清理旧工具结果），一条记录里全部改完
{"op":"compact","cut":12,"messages":[...]}   M6：前 12 条换成摘要；被压掉的原文还在前面的 append 行里
```

需要 `truncate` 是因为 Ctrl+C 之后 `Agent._repair` 要**撤回**已经写进历史的半条对话。
append-only 的文件表达「撤销」就得靠这种墓碑行；全量重写的话，进程崩在写一半时会丢掉整份历史。

消息历史只能通过 `Session.add` / `add_many` / `truncate` / `replace` / `compact` / `record_stats` 改，它们负责同步写盘。
写盘失败静默忽略（持久化是加分项，磁盘满了不该让对话中断）。

### MCP 客户端（M5，`mcp/`）

手写，不依赖官方 SDK。四层，每层只依赖下一层：

```
McpManager（manager.py）     多个 server：并行启动、失败隔离、崩溃后下次调用时重启、按需启动、关闭
  └ McpServer                一个 server 的句柄，也是 wrap_tools 要的 ToolCaller
wrap_tools（tools.py）       McpTool → 注册表里的 Tool（mcp__<server>__<tool>、schema 原样、按 annotations 定权限）
McpClient（client.py）       协议会话：server/discover 探测 → 新协议（每个请求带 _meta）或旧协议（initialize）
StdioTransport（transport.py）子进程 + 按行收发 JSON-RPC：id → Future、超时取消、stderr 排空、关 stdin → SIGTERM → SIGKILL
```

- **server 跟着进程走，不跟着会话走**：REPL 和 `sa run` 各持有一个 `McpManager`，`sa serve` 在 Runner
  的后台事件循环里持有一个、所有空间共用。启动时等所有 server 都有结果再接受第一个问题，会话里工具列表不变。
- **按需启动**（`start = "lazy"`）：工具列表要在第一次请求前定下来，server 却还没起，所以靠缓存——
  连上时把原始工具清单写到 `mcp_cache/<名字>.json`（`mcp/cache.py`，带启动参数指纹，参数变了就作废），
  下次启动直接拿它登记工具（状态 `standby`），第一次调用时才起进程（不算重启）。真实工具和缓存对不上时
  本次会话照旧、缓存更新，下次启动生效。没缓存就照常启动一次。
- **权限**：`readOnlyHint` 的工具（`trust_annotations = true` 时）allow + 可并行，其余 ask；配置里
  `permissions` 可以按工具覆盖。MCP 工具没有 `scope`，**工作目录边界管不到它们**，边界由 server 自己负责。
- **子进程环境是白名单**（`HOME` / `PATH` / `LANG` / 代理），密钥用 `env_vars` 只写变量名。
- server 的 `instructions` 截到 2000 字、标明是第三方内容后，追加到 system prompt 末尾。
- 详细的取舍和实测记录见 [notes/M5-mcp-client.md](notes/M5-mcp-client.md)。

### 上下文管理（M6，`agent/context.py`）

**预算**：下一次请求会发多少 token = 上次请求的实际用量 + 之后新增消息的估算。

- `usage.prompt_tokens` 精确覆盖上次发出的 system + 工具 + `messages[:n]`，loop 把它记成
  `Session.context_anchor`（只在内存里）。估算前先过 `prepare_messages`，按实际发出去的样子算
- 新增部分按字符估：ASCII 3 个字符一个 token、非 ASCII 一个字一个、每条消息加 4，往大了取。
  没用 tiktoken：多一个依赖，而且各家 tokenizer 不一样（实测 DeepSeek 上这套规则高估 20%～30%）
- **校准系数** = 实际用量 ÷ 同一段的字符估算，每次请求更新（`Session.context_calibration`，限制在 0.5～2）。
  清理、压缩、`truncate` 删进覆盖范围会作废实际用量，但不作废系数：之后全量估算再乘系数，
  不然清理完紧接着判断要不要压缩，用的是偏高 25% 的数，可能白压一次
- 输入上限 = `context_window` − 给输出留的余量（`max_tokens`，没配就取 16k 和窗口 1/4 中较小的）
- REPL 的 `/context` 显示总量、构成和「上次实际 vs 同一段按字符估」的误差

**三级策略**，每次请求前都查（不只在每轮开始时，daemon 一轮几十步也会撑爆）：

1. **写入时截断**（M2，`ToolRegistry.trim`）：单个工具结果超过 3 万字符 / 500 行只留开头，完整内容落盘
2. **清理旧工具结果**（`Agent._clear_tool_results`，占到 `[context].clear_at`，默认 60%）：较早的 tool 结果
   换成 `[已清理] …完整内容存在 <路径>，需要时用 read_file 读取`，只动 content，配对不断。不清最近
   `keep_tool_results` 个（默认 3）、模型还没看过的、已清过的、短于 300 字符的、以及模型把清理文件读回来的。
   原文落到 `tool_outputs/cleared-<内容摘要>.txt`，文件名只由内容决定，占位符一字不差
3. **摘要压缩**（`Agent.compact`，按清完的样子算还占到 `compact_at`，默认 80%）：切点只落在 user 消息或
   紧跟工具结果的 assistant 消息上，tool_calls 和结果要么一起压、要么一起留；保留最近约 1/4 的原文。
   摘要请求 = `[system, *messages[:切点], user(压缩指令)]`，tools 照带；压缩后是
   `user([对话摘要] …)`（保留部分以 user 开头时再加一条固定的 assistant 回复），然后是保留的原文

**和前缀缓存的关系**（这一节最重要的取舍）：

- system prompt 和工具列表整个会话不变（有回归测试守着）
- 清理改的是历史中间，缓存从第一条被改的消息起全部失效：过了阈值就一次清完，能省下的不到输入上限的 5% 就不清
- 摘要请求的 `messages[:切点]` 要和上一次请求**一字不差**才吃得到缓存：
  - 要压缩就**先压缩再清理**：先清的话前缀被改掉，被清的结果反正也要压进摘要
  - `reasoning_echo = "current_turn"` 时，压缩指令默认会被当成新的一轮、去掉本轮的思考内容。所以
    `stream(continue_turn=...)` 按上一次请求的回传起点来定：它的最后一条 user 在切点之前就接着回传，
    在切点之后（压的全是更早的轮次）就全去掉。实测 DeepSeek 摘要请求的缓存命中从 30%～58% 到 95%～97%
- 压缩之后第一次请求除了 system 和工具定义，缓存全部失效——这是压缩排在最后的原因

**其余入口**：

- REPL 的 `/compact [重点]`：切在最后一条 user 上（`last_turn_cut`），完整保留最后一轮
- 服务端说上下文超长（`is_context_overflow`：状态码 400 / 413 + 各家的说法）且还没有任何输出：强制压缩一次、
  只留最后一步（估算已经不可信），重试这一步；每步只兜底一次。Ollama 超过 `num_ctx` 是悄悄截断而不是报错，
  `context_window` 要配准
- 失败（API 报错、摘要为空）产出带 `error` 的 `ContextEdited`，历史不动，本轮不再重试
- 所有改动走 `Session.replace` / `compact`（JSONL 各一条记录），并产出 `ContextEdited` 事件：
  REPL / `sa run` 显示一行提示，工作台转成 `context_edited` 帧
- **工作台的两份历史**：`sessions/<sid>.jsonl` 按事件镜像，给界面显示实际发生过什么；`sessions/model/<sid>.jsonl`
  是 `SessionStore` 的操作记录，模型看到的历史，清理、压缩、中断修复都随手落盘。老会话第一次用到时迁移过来，
  顺手补上悬空的 tool_call（`fill_missing_results`）

详细的取舍和实测记录见 [notes/M6-context-engineering.md](notes/M6-context-engineering.md)。

### 项目指令、记忆、技能（M7，`knowledge/`）

三样东西让 agent 不再每个会话都从零开始，共同的做法是**渐进式披露**：常驻 system prompt 的只放
「目录」，内容按需取。

| | 常驻 system prompt 的 | 按需取的 | 谁来写 |
|---|---|---|---|
| 项目指令 | AGENTS.md 全文（总共不超过 3.2 万字） | 超出部分：`read_file` 读原文 | 用户 |
| 长期记忆 | `memory/MEMORY.md` 索引（每条一行，最多 6000 字） | 正文：`memory_read` | 模型（`memory_write`，默认要确认）和用户 |
| 技能 | 每个技能一行名字 + 描述（最多 8000 字） | SKILL.md 正文：`load_skill`；附带文件：`read_file` / `bash` | 用户 |

```
Knowledge.load(config, cwd)            会话开始时读一次（knowledge/__init__.py）
  ├ load_instructions()                个人 AGENTS.md → git 根 → … → cwd，每层取 AGENTS.md，没有再 CLAUDE.md
  ├ MemoryStore.read_index()           记忆索引的快照
  └ discover_skills(skill_roots())     个人 skills/ → [skills].dirs（相对路径按 git 根到 cwd 每层展开），先到先得
.prompt_section()   → build_system_prompt() 追加在「# 环境」之后：项目指令 → 长期记忆 → 技能
.tools()            → memory_read / memory_write / memory_delete / load_skill，排在内置工具之后、MCP 工具之前
```

- **会话内不变**：三样都在会话开始时定下来，中途改文件下个会话才生效；模型写的记忆也不回灌进本会话的
  system prompt（它从对话里已经知道了）。工作台的 Agent 每轮新建，所以 Runner 按会话缓存
  `(Knowledge, system prompt)`，连日期一起冻住
- **记忆**：每条一个 `.md`（frontmatter：name / description / updated），索引由工具维护——只改链接到这个
  文件的那一行，人手动加的分组、备注都保留；写文件先写临时文件再改名。记忆工具不报 `scope`：
  记忆目录在工作目录之外，报了路径会被边界拒掉。写和删默认 ask，`sa run` 里没人确认就拒绝
  （`--allow memory_write` 放行）；`[memory] confirm_writes = false` 改成直接放行
- **技能**：按 SKILL.md 开放标准，frontmatter 用手写的 YAML 子集解析（`knowledge/frontmatter.py`，不引
  PyYAML）。列表按名字排序；个人技能优先于项目里的同名技能。`disable-model-invocation: true` 的不进 prompt，
  只能用户用 `/技能名 补充说明` 调用（`SkillCatalog.expand_command`，正文里的 `$ARGUMENTS` 换成补充说明；
  REPL、`sa run`、工作台都支持，工作台界面上显示原话、模型看到展开后的全文）
- 详细的取舍和实测记录见 [notes/M7-memory-skills-instructions.md](notes/M7-memory-skills-instructions.md)。

### LLM Client 与配置（M1，已实现）

配置文件 `~/.simpleagent/config.toml`，由 `sa init` 生成，模板见 [`config.example.toml`](../src/simpleagent/config.example.toml)。每个 profile 包含：

- `base_url` / `model` / `api_key_env`（只写环境变量名，不写 key 本身）。key 的值先查环境变量，再查 `~/.simpleagent/.env`。`.env` 只读进内存、不写入 `os.environ`；用户自己 export 的 key 会进 `os.environ`，所以工具启动子进程时还要显式去掉所有 profile 的 `api_key_env`（`Config.api_key_env_names()` → `ToolContext.hidden_env`）
- `extra_body`：厂商私有参数，原样合并进请求体，用于试验新 feature（比如思考开关）
- `quirks`：
  - `reasoning_field`：思考内容所在字段（`reasoning_content` / `reasoning`）
  - `reasoning_echo`：历史里的思考内容是否回传（`none` / `current_turn` / `all`）
  - `stream_usage`：是否发送 `stream_options.include_usage`
  - `parallel_tool_calls`：是否允许并行工具调用

实现细节：
- 会话历史里的思考内容统一存到 `reasoning_content` 字段，发请求前由 `prepare_messages` 按 profile 改名或去掉
- usage 同时兼容 OpenAI 的 `prompt_tokens_details.cached_tokens` 和 DeepSeek 的 `prompt_cache_hit_tokens`
- `base_url` 是回环地址时不读代理环境变量，避免本机代理把 Ollama 的请求转走
- 重试直接用 SDK 自带的 `max_retries`；上下文超长错误在 M6 转去做压缩

### 数据目录

`~/.simpleagent/`（可用 `SIMPLEAGENT_HOME` 覆盖）。从源码仓库跑（`config.dev_checkout()` 认出
`pyproject.toml` + `.git`）时默认换成 `~/.simpleagent-dev/`，`sa serve` 默认端口也从 8384 换成 8385，
这样同一台机器上开发用的 `uv run sa` 和日常装的 `sa` 互不干扰。优先级：`SIMPLEAGENT_HOME` > 开发模式 > 默认。

| 路径 | 用途 | 引入 |
|---|---|---|
| `config.toml` | 配置 | M1 |
| `traces/<session>/<n>.json` | 每次 LLM 请求/响应 | M1 |
| `sessions/*.jsonl` | 命令行会话持久化（`sa --resume`） | M3 ✅ |
| `schedules.toml` | 定时任务定义（`sa init` 生成模板，`sa schedule list` 查看） | M4 🚧 |
| `jobs/<name>/` | 没写 `cwd` 的定时任务的默认工作目录 | M4 🚧 |
| `logs/` | 定时任务运行日志 | M4 |
| `tool_outputs/` | 过长工具输出的完整内容 | M2 |
| `mcp_cache/<name>.json` | 按需启动（`start = "lazy"`）的 MCP server 上次的工具清单 | M5 |
| `AGENTS.md` | 个人指令，所有项目通用（会话开始时进 system prompt） | M7 ✅ |
| `memory/` | 长期记忆：`MEMORY.md` 索引 + 每条一个 `.md` | M7 ✅ |
| `skills/<名字>/SKILL.md` | 个人技能 | M7 ✅ |

## 长期形态：个人 AI 工作台（具体内容待设计）

目标是一个**桌面客户端**形式的个人 AI 工作台。业务逻辑全部在自己写的 Python 代码里；客户端只负责界面和消息通知，不承载逻辑，所以客户端用什么技术栈不影响核心。

```
  桌面客户端（工作台界面、系统通知）     终端 REPL      IM / webhook
              │                          │               ▲
              └──────── 本地 API（WebSocket）──┘               │
                            │                               │
  ┌─────────────────────── Python 后台引擎 ────────────────────────┐
  │  会话管理 · 定时调度 · 事件总线 · 通知路由（客户端在线推客户端，否则走系统通知/IM）│
  └────────────────────────────┬───────────────────────────────┘
                               │
                     Agent Loop / 工具 / MCP / 外部 agent
```

- M4 的 `sa daemon` 往后演进成这个常驻的后台引擎：调度任务、交互会话、通知都在同一个进程里
- 后台任务需要审批时，客户端在线就推给客户端等待确认（带超时），不在线就按无人值守策略处理
- 外部 agent（Claude Code / OpenCode）作为工具接入：先用无头 CLI（`claude -p --output-format stream-json`、`opencode run --format json`），再考虑 ACP 协议

### M2 起就要守住的接口约定

为了让终端、桌面客户端、定时任务共用同一个核心：

1. **状态归 `Agent` / `Session`**：消息历史、用量、当前模型不放在任何界面层（M1 暂时在 `Repl` 里，M2 已迁到 `Agent` / `Session`）
2. **审批器是异步接口**：`Approver.request(req) -> ApprovalDecision`，终端、客户端、无人值守
   各自实现。判定要不要问（`Policy`）和执行询问（`Approver`）是两层，见「权限（M3）」
3. **工具能上报进度**：`ToolContext.emit(event)`，长时间运行的工具（比如外部 agent）靠它流式反馈
4. **取消是显式调用**：`agent.cancel()`；Ctrl+C、客户端的停止按钮都只是调用方
5. **事件可序列化**：事件带 `type` 字段、能转 JSON，方便通过本地 API 推给客户端

## 代码结构

✅ 表示已实现，🚧 表示部分实现，其余按里程碑逐步加入。

```
src/simpleagent/
  cli.py                  ✅ argparse 入口：sa / sa init / sa run / sa sessions / sa serve / sa --resume /
                          🚧 sa schedule list / sa mcp list
  config.py               ✅ TOML + 环境变量 → pydantic 配置模型（M5 起含 [mcp_servers.<名字>]）
  config.example.toml     ✅ sa init 使用的配置模板
  events.py               ✅ 事件类型（TextDelta / ReasoningDelta / ApiRequest / ApiResponse /
                          ✅ MessageDone / ToolCallStart / ToolResult / ContextEdited / MaxStepsReached）
  trace.py                ✅ 请求/响应全量落盘
  permissions.py          ✅ 权限：Decision / Scope / Policy（含 bash 危险命令识别）、审批器协议
  llm/client.py           ✅ 流式调用、chunk 拼接、思考内容、quirks
  llm/fake.py             ✅ 测试用的脚本化模型
  agent/prompt.py         ✅ system prompt 组装：基础提示 + 环境 + 项目指令 / 记忆索引 / 技能列表（M7）
  agent/loop.py           ✅ Agent loop：工具调用循环、max_steps、中断后修复历史
  agent/session.py        ✅ 会话状态：消息历史、用量、JSONL 持久化与恢复
  agent/context.py        ✅ token 预算与校准、清理旧工具结果、摘要压缩的切点与指令、
                          ✅ 手动压缩的切点、上下文超长报错识别（M6）
  tools/                  ✅ Tool 抽象、注册表、7 个内置工具
  tools/base.py           ✅ ToolContext（cwd / output_dir / hidden_env / save_output）、ToolError、
                          ✅ Tool（readonly / truncate_output / permission / scope /
                          ✅ parameters / confirm_reason）、@tool
  tools/registry.py       ✅ schema 生成、execute（失败转错误文本）、execute_many（只读并行 / 含写串行，分批产出）、输出截断
  tools/walk.py           ✅ 忽略清单、带剪枝的目录遍历、二进制判断、按行读取、可取消的线程执行（glob / grep / read_file / list_dir 共用）
  tools/output.py         ✅ 输出截断：按行数和字符数截断 + 落盘提示
  tools/{list_dir,read_file,write_file,edit_file,glob,grep,bash}.py  ✅ 内置工具
  schedules.example.toml  🚧 sa init 生成的定时任务模板
  scheduler/models.py     🚧 Job：schedules.toml 里的一个任务（cron 校验、下次触发时间、默认工作目录）
  scheduler/store.py      🚧 读 schedules.toml：按任务隔离错误，一个任务写错不连累其余任务
  scheduler/                 定时 daemon、运行日志（M4 后续步骤）
  mcp/transport.py        ✅ MCP stdio 传输层：子进程、按行收发 JSON-RPC、id → Future 多路复用、
                          ✅ 超时/取消通知、stderr 排空、关 stdin → SIGTERM → SIGKILL（M5）
  mcp/client.py           ✅ MCP 协议会话：server/discover 探测，新协议每个请求带 _meta、旧 server 退回
                          ✅ initialize；tools/list 翻页、tools/call 结果转文本（M5）
  mcp/tools.py            ✅ 配置 → server 子进程（环境变量白名单 + env / env_vars）；MCP 工具 → Tool
                          ✅ （mcp__<server>__<tool>、schema 原样、按 annotations + 配置定权限）（M5）
  mcp/manager.py          ✅ McpManager：并行启动、失败隔离、崩溃后下次调用时重启（5 分钟内最多 3 次）、
                          ✅ instructions 进 system prompt；REPL / sa run / sa serve 共用（M5）；按需启动
  mcp/cache.py            ✅ 按需启动用的工具清单缓存（启动参数指纹、原子写）
  knowledge/__init__.py   ✅ Knowledge：会话开始时读项目指令、记忆索引、技能清单，拼 prompt、给工具（M7）
  knowledge/frontmatter.py ✅ Markdown frontmatter 的 YAML 子集解析和生成（技能、记忆共用）
  knowledge/instructions.py ✅ AGENTS.md 查找（个人 → git 根 → … → cwd，CLAUDE.md 兜底）、字数预算
  knowledge/memory.py     ✅ MemoryStore（每条一个文件、索引按行维护、原子写）、memory_read/write/delete
  knowledge/skills.py     ✅ SKILL.md 发现（先到先得、同名覆盖记录）、load_skill、/技能名 展开
  command/                ✅ 指挥台调度者：调度 prompt（空间清单）、propose_plan / dispatch 工具、
                          ✅ Dispatcher 协议（Runner 实现 run_child）（M8 第一部分，见 design/command-dispatch.md）
  spaces/describe.py      ✅ 空间简介的自动摘要：读 AGENTS.md / README / 顶层文件 / 会话标题，调一次模型
  ui/repl.py              ✅ 交互式 REPL（写操作终端确认；M5 起有 /mcp，M6 起有 /context、/compact，
                          ✅ M7 起有 /memory、/skills、/prompt 和 /<技能名>）
  ui/headless.py          ✅ `sa run`：无人值守单次执行，白名单审批
  ui/approve.py           ✅ ConsoleApprover：终端 y / a / 其他键拒绝
  ui/debug.py             ✅ debug 输出：API 调用与工具调用的过程，走 stderr（off / on / verbose / full）
tests/                    ✅ 各模块对应测试
```
