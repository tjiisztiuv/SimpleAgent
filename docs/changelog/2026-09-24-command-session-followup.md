# 指挥台调度者支持追问已有会话（recent_sessions / followup），会话加互斥锁

- 日期：2026-09-24
- 对比基线：`3058400`（变更记录：README 安装前提醒 sa 撞名）
- 对应里程碑：M8

## 功能变化

- 新增：调度者新增 `recent_sessions`（列出打开空间里最近的会话）和 `followup`（在已有会话里接着问）两个工具，用户可以让调度者「追问」之前的任务，而不是每次都新开会话。
- 新增：`dispatch` 和 `followup` 共用同一道「跨空间先确认」关卡（`admit`），追问也算「派给这个空间」，涉及第二个空间时同样要先走 `propose_plan`。
- 新增：`Runner` 加会话级互斥锁（claim/release），同一个会话同一时间只能跑一轮；被占用时 HTTP 层（`/api/sessions/{id}/input`、`/rerun`）返回 409（`SessionBusy`）。以前只靠前端把输入框置灰，后端没有兜底。
- 新增：`SessionMeta` 加 `dispatched_by` 字段，记录「最近一次是哪个调度会话让它跑的」；控制面板卡片挂在最近那个调度者下面，和 `parent_session_id`（记出身，创建后不变）区分开。
- 升级：会话摘要 API 新增 `dispatched_by`、`locked` 字段；面板 summary 的 running/recent 列表项也带上 `dispatched_by`。
- 升级：控制面板（`web/`）支持「追问」操作：卡片上点「追问」，下一句直接发给该会话（不经过调度者）；输入框上方出现「追问 → …」提示条，Esc 可退出追问模式；正在追问的卡片有高亮样式。
- 新增：`.claude/skills/ship/SKILL.md`，把 `/ship` 上线流程（审查 → 自动检查 → 手动验证 → 写变更记录 → 提交推送 → 开 PR → 合并到 main → 收尾）固化成 skill，仅在用户敲 `/ship` 时触发。
- 新增：调研文档 `docs/research/2026-09-24-agent-to-agent-orchestration.md`，梳理 A2A / MCP / ACP / AG-UI 等 agent 间通信协议的分层，以及 Claude Code / Codex / Gemini CLI / OpenCode / Cursor / Devin / Kimi 等主流 agent 软件内部的多 agent 编排做法，为 M8 后续设计（`task` 工具、外部 agent、跨空间调度）提供依据。

## 函数级改动

### `src/simpleagent/command/__init__.py`

| 项 | 变化 | 说明 |
|---|---|---|
| 模块导出 | 修改 | 新增导出 `SessionBrief`；docstring 更新为「4 个调度工具」，补充设计文档引用 `docs/design/session-followup.md`（设计文档随后在 `d8cf11a` 补上） |

### `src/simpleagent/command/prompt.py`

| 项 | 变化 | 说明 |
|---|---|---|
| `COMMAND_PROMPT` | 修改 | 新增「追问已有的会话」一节：提示调度者遇到「接着改」「刚才那个」这类延续性任务时，先用 `recent_sessions` 找会话再 `followup`，找不到或有歧义要列候选问用户；说明手里的工具从两个（dispatch/propose_plan）变成四个 |

### `src/simpleagent/command/tools.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `ChildResult.followup` | 新增字段 | 标记这一轮是追问已有会话，不是新派发 |
| `ChildResult.render()` | 修改 | 追问时在状态文字后追加「（追问）」 |
| `SessionBrief` | 新增类 | `recent_sessions` 列表项，也是 `followup` 前置检查用的数据：space、session、标题、状态、是否是调度派出的、是否锁定、是否忙、最后一条回复摘要；带 `render()` |
| `_ago()` | 新增函数 | 把 ISO 时间戳换算成「3 分钟前」这类相对时间，供 `SessionBrief.render()` 用 |
| `Dispatcher.sessions()` | 新增协议方法 | 列出打开空间里最近的会话（不含指挥台） |
| `Dispatcher.find_session()` | 新增协议方法 | 按 id 找会话，不管在哪个空间 |
| `Dispatcher.run_child()` | 修改签名 | 新增 `session_id` 参数：为空新建子会话（原行为不变），不为空则在已有会话里接着跑（追问） |
| `RecentArgs` | 新增模型 | `recent_sessions` 工具入参：`space`（可选）、`limit`（默认 10，上限 30） |
| `FollowupArgs` | 新增模型 | `followup` 工具入参：`session`（会话 id）、`message`（接着说的话） |
| `admit()` | 新增函数（从 `dispatch()` 内联逻辑抽出） | `dispatch` 和 `followup` 共用的跨空间确认关卡：登记本轮用过的空间、拦截计划外派发、拦截同空间并发 |
| `dispatch()` | 修改 | 关卡逻辑改为调用 `admit()`，对外行为不变 |
| `recent_sessions()` | 新增函数 | 工具实现：解析可选的空间名，调用 `dispatcher.sessions()` 渲染成文本 |
| `followup()` | 新增函数 | 工具实现：按 id 找会话，依次校验存在、非调度会话自身、所在空间未关闭、未锁定、未占用，过 `admit()` 后调用 `run_child()` 追问 |
| `command_tools()` 返回的工具列表 | 修改 | 新增 `recent_sessions`、`followup` 两个 `Tool`（均 `readonly=True`，可并行） |

### `src/simpleagent/serve/app.py`

| 位置 | 变化 | 说明 |
|---|---|---|
| `/api/sessions/{id}/input` 处理逻辑 | 修改 | 捕获 `Runner` 抛出的 `SessionBusy`，返回 409 |
| `/api/sessions/{id}/rerun` 处理逻辑 | 修改 | 同上，会话忙时返回 409 |
| panel summary 相关逻辑 | 修改 | running/recent 列表项新增 `dispatched_by` 字段 |
| 会话摘要相关逻辑 | 修改 | 返回体新增 `dispatched_by`、`locked` 字段 |

### `src/simpleagent/serve/runner.py`

| 函数 / 方法 | 变化 | 说明 |
|---|---|---|
| `SessionBusy` | 新增异常类 | 会话正在跑一轮时抛出，HTTP 层转 409 |
| `Runner.__init__()` | 修改 | 新增 `_claimed`（会话 id → 占用凭据）和 `_claim_lock`，实现会话级互斥 |
| `Runner.run_input()` | 修改 | 先 `claim` 会话，占不到抛 `SessionBusy`；调度失败时释放占用 |
| `Runner.claim()` | 新增方法 | 占住一个会话跑一轮，返回占用凭据；已被占用返回 `None` |
| `Runner._release()` | 新增方法 | 释放占用；带 token 时只释放自己那一份，避免误放别人刚占上的 |
| `Runner.session_busy()` | 新增方法 | 查询会话是否正被占用 |
| `Runner.sessions()` | 新增方法 | 实现 `Dispatcher.sessions`：按 `updated_at` 排序返回最近会话的 `SessionBrief` 列表 |
| `Runner.find_session()` | 新增方法 | 实现 `Dispatcher.find_session`：按 id 查会话（不限空间，关掉的空间、指挥台自己的会话也能查到） |
| `Runner._brief()` | 新增方法 | 把 `Space` + `SessionMeta` 组装成 `SessionBrief`，含读取最后一条回复摘要 |
| `Runner.run_child()` | 修改签名与实现 | 新增 `session_id` 参数；为空创建新会话，不为空则复用已有会话并再次 `claim`（防止工具查过之后被别人抢占）；更新 `dispatched_by`；只统计追问这一轮新增的消息（记录追问前的消息数作为起点） |
| `Runner._run_input()` | 修改签名 | 新增 `token` 参数，改为薄包装：调用 `_run_turn()` 并在 `finally` 里释放占用（兜底，覆盖提前返回、构造阶段异常等未走 `_finalize` 的路径） |
| `Runner._run_turn()`（原 `_run_input` 主体，重命名后新增） | 新增/拆分 | 原来的执行逻辑搬到这里；执行者被锁定需提前返回时也显式释放占用 |
| `Runner._finalize()` | 修改 | 在发布终态帧之前释放会话占用，避免客户端收到「完成」帧后立刻追问，却撞上还没释放的占用 |

### `src/simpleagent/spaces/models.py`

| 项 | 变化 | 说明 |
|---|---|---|
| `SessionMeta.dispatched_by` | 新增字段 | 记录最近一次让这个会话跑起来的调度会话 id；`from_dict` 同步解析，缺省为 `None` |

### `src/simpleagent/spaces/store.py`

| 项 | 变化 | 说明 |
|---|---|---|
| `update_meta` 允许更新的字段集合 | 修改 | 加入 `dispatched_by`，使其可以通过 `update_meta` 写入 |

### `src/simpleagent/web/app.js`

| 函数 / 状态 | 变化 | 说明 |
|---|---|---|
| `panelState.replyTo` | 新增字段 | 追问模式状态：记录当前正在追问哪个会话 |
| `DISPATCH_PLACEHOLDER` | 新增常量 | 抽出原本内联的输入框占位符文案，追问模式退出时用来还原 |
| `restoreDispatch()` | 修改 | 已有卡片按后台的 `updated_at` 刷新：变了就说明又跑过一轮（被追问、在别处接着聊），换到最近那个调度者下面、刷新状态和最后一句。只看「在不在 running 列表里」不够：追问可能在两次轮询（3 秒）之间就跑完了，卡片会一直停在上一轮 |
| `cardParent()` | 新增函数 | 卡片挂靠逻辑：优先用 `dispatched_by`，没有则退回 `parent_session_id` |
| `renderDispatch()` | 修改 | 卡片挂靠改用 `cardParent()`；跑完且未锁定、未在等审批的会话展示「追问」按钮；正在追问的卡片加高亮样式 |
| `setReplyTo()` | 新增函数 | 进入追问模式：记录目标会话，刷新提示条和卡片列表，聚焦输入框 |
| `clearReplyTo()` | 新增函数 | 退出追问模式 |
| `renderReplyTo()` | 新增函数 | 渲染/清除输入框上方的「追问 → …」提示条 |
| `sendReply()` | 新增函数 | 追问模式下发送：直接 POST 到该会话的 `/input`；失败把原话还给输入框并显示失败原因 |
| `pollDispatch()` | 修改 | 轮询摘要后，非调度台会话的卡片挂靠随 `dispatched_by` 更新 |
| `dispatch()` | 修改 | 追问模式下拦截，改走 `sendReply()` |
| 键盘事件处理（`boot()` 内） | 修改 | Esc 在追问模式下调用 `clearReplyTo()` |

### `src/simpleagent/web/index.html`

- 新增 `#dispatch-reply` 容器（追问模式的提示条挂载点）

### `src/simpleagent/web/styles.css`

- 新增 `.dispatch.replying`、`.reply-chip` 及其子元素样式（追问中的卡片高亮、提示条外观）

### `.claude/skills/ship/SKILL.md`（新文件）

- 新增 `/ship` 上线流程 skill（`disable-model-invocation: true`，仅用户显式敲 `/ship` 触发）：固化审查 diff → `ruff format`/`ruff check`/`pytest` → 必要时手动起临时服务验证 → 写变更记录（交给 `changelog-writer` 子 agent）→ 提交推送 → `gh pr create` → 检查合入条件后合并 → 切回 main 拉取的完整步骤，并列出「停下来问用户」的边界情况（设计层面的问题、非本次改动引起的测试失败、疑似 API key、和 main 冲突等）。

### `docs/research/2026-09-24-agent-to-agent-orchestration.md`（新文件）

- 新增 Agent-to-Agent 协议与多 agent 编排调研，共约 268 行。内容包括：三个核心结论（协议按通信边界分层、主流 agent 软件内部编排基本自研不走标准协议、写操作只交给一个 agent）；协议分层表（A2A / MCP / ACP / AG-UI / AGNTCY / ANP）；A2A 细节（Agent Card、Task 状态机、Message/Artifact、方法列表、v1.0 新特性）；MCP 作为 agent 调 agent 最短路径；进程内编排的六种模式；Claude Code / OpenAI Codex / Gemini CLI / OpenCode / Cursor / Devin / Kimi Agent Swarm 等主流 agent 软件的具体做法；收敛出的规律；对 SimpleAgent M8（`task` 工具、外部 agent、跨空间调度）的启发；文末附可靠性分级和来源列表。

## 配置与依赖

- 无新增依赖、配置项，`pyproject.toml` / `uv.lock` 无改动。
- `SessionMeta` 新增的 `dispatched_by` 字段对旧数据向后兼容（读取时缺省为 `None`），不需要迁移。
- 需要手动处理：无。

## 测试

- `tests/serve/test_command.py`：新增/扩展追问相关的端到端测试，覆盖「新调度会话追问旧调度会话派出的子会话（跨调度者接力，卡片改挂到新调度者下面、只统计这一轮回复）」「取消调度者时联动取消正在追问的子会话」「`run_child` 在真正占用会话时发现已被别人抢占（工具查过之后到开跑之间的空档）」「`sessions()` / `find_session()` 的排序、过滤与锁定标记」等场景，另扩展了原有的单空间派发测试（工具集从 2 个变成 4 个）和面板摘要测试（`dispatched_by`/`locked` 字段）。
- `tests/serve/test_serve.py`：新增会话互斥锁相关测试，覆盖「会话忙时 `input`/`rerun` 返回 409、不落任何消息」「claim 在正常收口、被取消、启动失败（profile 不存在）、执行者被锁定等各种退出路径下都会释放，且释放要赶在终态帧之前」。
- `tests/test_command_tools.py`：新增 `recent_sessions` / `followup` 工具的单元测试（假 Dispatcher，不起 Runner），覆盖追问跨空间确认关卡与 dispatch 共用、同一批里 dispatch 和 followup 混用、已批准计划内追问、拒绝追问不存在/调度会话自身/已关闭空间/已锁定/正在跑的会话、真正占用时发现被抢占后空间不再算「在跑」、`recent_sessions` 的列表渲染与过滤（按空间、按数量上限）、`SessionBrief.render()` 的文案、`ChildResult` 的追问标记。
- 测试结果：734 passed（`uv run pytest -q`）
- `uv run ruff check`：通过
- `uv run ruff format --check`：无需改动
- 手动验证：浏览器实测（临时 `SIMPLEAGENT_HOME` + 按规则应答的假模型，不联网）：新调度会话追问旧调度会话派出的子会话、卡片随之改挂；子任务卡和调度卡上的「追问」、Esc 退出；会话在跑时再发返回 409、原话留在输入框。实测中发现并修掉了上面 `restoreDispatch()` 那条的问题。没有接真实模型，详见 [design/session-followup.md](../design/session-followup.md) 第 6 节

## 相关笔记

- 无（M8 里程碑尚未完结，学习笔记按约定在里程碑结束后统一写）
