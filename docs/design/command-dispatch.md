# 指挥台调度：一句任务自动派给空间，跨空间协作

> 状态：已实现（2026-09-24）。这份文档包括方案、改动记录和验证结果。变更记录见 [changelog/2026-09-24-command-dispatch.md](../changelog/2026-09-24-command-dispatch.md)。
> 对应 ROADMAP 的 M8「子 agent」：这里的子 agent 就是各个空间，包括 claude-code / opencode 空间。

## 1. 要解决什么

以前的指挥台只会原样转发：前端解析 `@空间名 任务`，在那个空间新建会话，把原话发过去。
它不理解任务，不会自己选空间，也不能把一件事拆给几个空间合作完成。

现在的指挥台：

- **直接说任务**（不带 `@`）：交给调度者。它挑合适的空间派过去，跑完把结论汇总回来。
- **需要多个空间配合**：调度者先出一张**计划卡**（每一步派给谁、做什么、等哪几步），你确认后才执行。
  互不依赖的步骤并行跑，有依赖的等前一步的结果回来，再写进下一步的任务描述。
- **`@空间名 任务`**：和以前一样直接下发，不经过调度者。

## 2. 和你确认过的三条口径

| 问题 | 结论 |
|---|---|
| 空间之间怎么共享信息 | 第一版**只经过调度者中转**：子任务的结论回到调度者，由它写进下一个任务描述。不做共享目录，也不做空间之间直接发消息 |
| 要不要先确认 | **只涉及一个空间就直接派；跨空间先出计划卡，确认后才执行** |
| 路由靠什么 | 空间设置里新加一栏「**简介**」，可以手改，也可以让模型**自动摘要（50～100 字）** |

为什么空间之间不直接发消息：外部 CLI（claude / opencode）的无头模式跑起来之后收不到中途消息，
「发消息」最后只能变成「开一个新会话」，也就等于派任务；多个 agent 互相派活还容易循环、成本失控、
难调试（`docs/research/` 里的调研也是这个结论）。长期知识本来就是共享的：M7 的全局记忆
`~/.simpleagent/memory/` 对所有内置执行者的空间都可见。

## 3. 架构：中心调度（hub-and-spoke）

```
指挥台输入（不以 @ 开头）
  └─ 调度者会话 —— 保留空间 sp_command（左栏不显示），内置 loop，只有两个工具
       ├─ propose_plan(steps) ─→ 审批器 → 面板上的计划卡 → 你点「确认执行」才返回
       ├─ dispatch(space, task) ─→ Runner.run_child：在目标空间新建子会话（meta 记 parent_session_id），
       │                          等它跑完，返回「状态 · 改动文件 · 验证 · 最后的回复」
       └─ 同一条回复里的多个 dispatch ─→ 注册表对只读工具本来就 gather 并行
以 @空间名 开头：和原来一样直接下发
```

- **调度者本身也是一个普通会话**：会话存储、SSE、审批、任务卡、对话视图都直接复用。
  点它的卡片就能进对话视图，看它每一步派了什么、收到了什么，也可以接着和它聊。
- **调度者只有调度工具**：不读写文件、没有记忆、没有技能、不接 MCP，职责单一。
- **防止递归**：子会话是目标空间里的普通会话，拿不到 dispatch。这是机制上保证的，不靠 prompt。
- **路由依据**：调度者的 system prompt 里列出每个打开着的空间：名字、id、简介、执行者 + 能力
  （内置能读写但写要批准 / 外部 CLI 只读 / 全放行）、目录。这份清单在会话开始时拼一次、之后不变，
  沿用 M7 的做法，保住前缀缓存。关掉的空间不在清单里，也派不过去。
- **跨空间确认由代码保证**（`command/tools.py`），不只靠 prompt：
  - 本轮第一个 dispatch 直接放行。要派到**第二个不同的空间**，而本轮还没有批准过的计划，就报错，让它先 `propose_plan`；
  - 计划批准之后，派到**计划外的空间**也报错；
  - 同一个空间上一个子任务还没跑完，不许再派（同一个目录里两个 agent 同时写会互相踩）；
  - 这些检查和登记之间没有 `await`，所以同一批并行的 dispatch 也是按调用顺序依次过关，并行绕不过去；
  - 计划先校验再给人看：写错了空间名、`after` 指向后面的步骤，直接打回给模型，不会拿一份派不出去的计划来问你；
  - 计划卡**没有「始终允许」**：前端不显示这个按钮。后端给调度者的审批器单独配一份「始终允许」记录，
    每轮新建一份，就算有人直接调接口传 always，也只在这一轮里生效，不会带到之后的调度。
- **「一轮」的范围**：从你说一句话到调度者回复完。计划的审批会让 propose_plan 这次调用一直挂起，
  你点了才继续，所以「提计划 → 确认 → 按计划派」都在同一轮里完成，状态不用跨轮保存。
- **取消联动**：停掉调度者，它正在等的子任务也会跟着停。内置执行者直接取消子任务的 asyncio 任务；
  外部 CLI 走 `cancel()` 杀整个进程组。子任务是单独起的 asyncio 任务，外面套了一层 `shield`。
  不能直接 await `_run_input`：它用 `current_task()` 把自己登记进 `_tasks`，直接 await 的话登记的是
  调度者的任务。
- **子任务里的审批**：内置执行者的写操作照常弹审批卡，**直接显示在指挥台子任务的卡片上**，
  这是已有的 `/api/approvals` 轮询。调度者会一直等，直到你批完、子任务跑完。

## 4. 怎么用

1. **给空间写简介**：左栏空间卡片上的 ⚙ →「简介」栏。可以手写，也可以点「自动生成」：模型读工作目录的
   `AGENTS.md`（没有就读 `CLAUDE.md`）、`README.md` 开头、顶层文件列表和最近 10 个会话标题，写一段 50～100 字。
   生成结果**只填进文本框，不自动保存**，你改完点保存才生效。简介上限 200 字，换行会拍平成一行。
   什么素材都读不到时（通用空间还没跑过会话），会提示你先手写一句。
2. **在指挥台直接说任务**，比如「统计一下 9 月开销，写成一份报告文件」。
   只涉及一个空间就直接派；跨空间会先出计划卡，点「确认执行」或「拒绝」。
3. 子任务的卡片**缩进挂在调度者卡片下面**，左栏里的子会话带一个「派」标记。
   点任何一张卡片都能跳进对应的会话。
4. **可选配置**（`config.toml`）：
   ```toml
   [command]
   profile = "deepseek"   # 调度者用的模型，不填用 default_profile
   ```
   调度者只负责派活和汇总，用便宜快的模型就够了。

**需要你手动做的**：没有必须做的。建议给常用空间写简介，没写简介的空间调度者只能看名字和目录猜。
你本机的 `code`、`finance`、`main` 这几个名字，光看名字很难判断该派给谁。

## 5. 改动记录（按实施步骤）

### 第 1 步：空间简介

| 位置 | 改动 |
|---|---|
| `spaces/models.py` | `Space.description: str = ""`；新增 `DESCRIPTION_MAX = 200`、`clean_description()`：换行拍平、超长报 `ValueError`（不悄悄截断，截掉的半句可能正是关键） |
| `spaces/store.py` | `_space_to_toml` 非空时写 `description`；`_space_from_toml` 读取，老文件没有这一行照常读；`UPDATABLE_FIELDS` 加 `description`，`update_space` 保存前过一遍 `clean_description` |
| `spaces/describe.py`（新） | `gather_material(space, cwd, titles) -> str \| None` 拼素材（纯函数）；`describe_space(llm, material) -> str` 发一次不带工具的请求；`clip()` 去引号、拍平、兜底截断；`DescribeError` |
| `serve/runner.py` | `describe(space_id) -> Future[str]`、`_describe()`：固定用 `default_profile`，经 `llm_factory` 构造，测试时能注入 FakeLLM，用完 `close()` |
| `serve/app.py` | `POST /api/spaces/{id}/describe`：成功返回 200 `{"description"}`，**不保存**；素材为空返回 400，模型出错 502，超过 60 秒返回 504（并取消后台任务）|
| `web/*` | 设置弹层加「简介」文本框 +「自动生成」，只在「空间设置」里显示、新建时隐藏；`generateDescription()`；`req()` 支持单个请求自定义超时，自动生成用 70 秒 |

### 第 2 步：调度者空间与 prompt

| 位置 | 改动 |
|---|---|
| `spaces/models.py` | `COMMAND_SPACE_ID = "sp_command"`、`COMMAND_SPACE_NAME = "指挥台"` |
| `spaces/store.py` | `ensure_command_space()`：不存在就建，`opened=False`，执行者是内置 loop |
| `config.py` / `config.example.toml` | 新增 `CommandConfig(profile: str \| None)`，也就是 `[command] profile`；指定的 profile 不存在会报配置错误 |
| `command/prompt.py`（新） | `COMMAND_PROMPT`（角色、做法、规则）、`spaces_section(spaces)`、`command_prompt(spaces, now)` |
| `serve/app.py` | `Server.start()` 先调 `ensure_command_space()`；`/api/meta` 多返回 `command_space_id`；指挥台空间 PATCH、DELETE 返回 400 |

### 第 3 步：dispatch 工具 + Runner.run_child

| 位置 | 改动 |
|---|---|
| `spaces/models.py` | `SessionMeta.parent_session_id`：创建时定下，之后不变，不开放给 `update_meta` |
| `spaces/store.py` | `create_session(space_id, agent=None, *, parent=None)` |
| `command/tools.py`（新） | `ChildResult`（带 `render()`，回复超过 4000 字截断并注明全文在哪个子会话）；`Dispatcher` 协议（`targets()`、`run_child()`）；`resolve_space()`：按 id → 名字 → 忽略大小写查找，重名时要求用 id，不能派给指挥台自己；`command_tools(dispatcher, *, parent, approver)` |
| `serve/runner.py` | `targets()`：左栏打开着的空间，不含指挥台；`run_child()`：新建子会话、单独起任务、`shield` + 取消联动、结果组装（复用 `panel/summary.summarize` 拿改动文件和验证状态）；`_outcomes` 由 `_finalize` 记下终态和失败原因，交给 `run_child`；`_build_agent` 遇到 `sp_command` 就转给 `_build_commander()` |
| `serve/app.py` | `/api/panel/summary` 的条目和 `/api/sessions/{id}/summary` 多返回 `parent_session_id` |

`dispatch` 设成 `readonly=True`：对调度者来说派发可以并行，各个空间在自己的目录里跑，改不到调度者这边。

### 第 4 步：跨空间确认

`propose_plan` 在工具**内部**先校验计划，再调审批器（`ApprovalRequest.tool_name = "propose_plan"`，
`arguments` 是规范化后的计划 JSON，带 `space_id`），没有走注册表的 ask 流程。原因是注册表先问人再执行，
计划写错了也得先让你批，批完才报错。它设成 `readonly=False`：同一批调用里只要有它，就按顺序执行，
保证先确认、后派发。无人值守（没有审批器）时一律拒绝。

### 第 5 步：前端

| 位置 | 改动 |
|---|---|
| `dispatch()` | 不以 `@` 开头的交给新增的 `dispatchToCommander()`：在 `sp_command` 建会话再发送，失败时把原话还给输入框；以 `@` 开头保持原来的直接下发 |
| `renderDispatch()` | 按 `parentId` 排成树，子任务卡片缩进（`↳`）；调度者卡片带「调度」标记；计划审批用 `planHtml()` 按步骤显示，按钮只有「确认执行 / 拒绝」 |
| `pollDispatch()` | 调度者还在跑时每次都刷新 summary，把新派出的子任务补成卡片；有变化时顺带刷新左栏 |
| `addApprovalCard()` | 对话视图里的计划审批也按步骤显示，不给「本次会话始终允许」 |
| `findSpace()`、`loadCommandSpace()` | 指挥台空间左栏不显示，但点进调度会话时，头部、日志、导出都要用到它 |
| 左栏会话行 | `parent_session_id` 不为空时显示「派」标记 |
| `index.html` / `styles.css` | 指挥台的说明和占位文字；`.dispatch.child`、`.plan`、`.badge.b-cmd` |

### 第 6 步：文档

新增本文件；`ROADMAP.md` 更新 M8 的状态和进度说明；`ARCHITECTURE.md` 的代码结构加上 `command/`；
`client-ui.md` 新增 10.14 节，指向本文件。

### 顺手修的两个问题

1. **打开会话后历史显示两遍**（已有的 bug，这次在浏览器实测时发现）：页面在后台时打开会话，
   `subscribe` 直接返回，`lastSeq` 还是 0；切回前台续传带的是 `last_event_id=0`，服务端就把这个会话
   所有的帧重放一遍，叠在已经画好的历史上。以前只是显得乱，现在重放出来的计划卡按钮还能点，会误导人。
   修法：`GET /api/sessions/{id}` 多返回 `seq`（读完历史之后的帧号），前端 `subscribe(id, { from: seq })`
   只要这之后的帧。
2. 指挥台标题被说明文字挤成两行（「指挥 / 台」）：说明文字改短了。

### 发布前审查时修的

1. **`Runner._outcomes` 越攒越多**：原来 `_finalize` 给每个会话都记一笔收口结果，只有 `run_child` 会取走，
   普通会话的记录永远留在内存里；调度者被取消时也不取。现在改成 `run_child` 先登记占位，
   `_finalize` 只填登记过的，`run_child` 在 `finally` 里取走。
2. **简介里的控制字符会让空间消失**：`\x07` 这类字符原样写进 `space.toml`，TOML 读不回来，
   `list_spaces` 会跳过这个文件。新增 `spaces/models.py` 的 `one_line()`，`clean_description()` 和 `describe.clip()` 共用。
3. `planHtml()` 里的依赖步骤号补上了 `escapeHtml`。

## 6. 验证

**自动测试**：`uv run pytest` 共 718 个全部通过（改动前 691 个，新增 27 个）。`ruff format`、`ruff check` 都通过。

| 文件 | 覆盖 |
|---|---|
| `tests/test_command_tools.py`（12 个） | 单空间直接派；第二个空间没有计划时被拒；并行派两个空间时只有第一个放行；批准计划后并行执行（用闸门验证两个子任务确实同时在跑）；计划被拒；计划外的空间被拒；错误计划不会拿去问人；无人值守时拒绝；同一空间同一批派两次被拒；按 id / 大小写 / 重名查找；结果截断和失败原因；prompt 里的空间清单 |
| `tests/serve/test_command.py`（7 个） | Runner + FakeLLM 端到端：单空间派发（子会话记 parent、调度者只有两个工具、子会话没有 dispatch）；跨空间审批（批准前没有任何子会话，批准后两个都完成）；取消调度者时子任务跟着取消；子任务起不来时失败原因回到调度者；`targets()` 跳过关掉的空间；指挥台空间的 HTTP 保护和 `parent_session_id`；`[command] profile` 校验 |
| `tests/serve/test_describe.py`（5 个） | PATCH 简介和 200 字上限；自动生成返回建议但不保存（去引号、不带工具、用完关闭客户端）；没有素材返回 400；模型出错返回 502 / 空间不存在返回 404；素材拼接（AGENTS.md 优先、README 截断、跳过隐藏文件、标题去重去掉「新会话」）|
| `tests/spaces/test_store.py`（+2） | 简介在 toml 里读写往返（引号、换行）、超长被拒 |
| `tests/serve/test_serve.py`（+1） | 会话接口带 `seq`，从这个帧号续传不会重放 |

**手动验证**（浏览器）：临时 `SIMPLEAGENT_HOME` + 脚本化 FakeLLM，不联网，不碰 `~/.simpleagent*`：

- 空间设置里「自动生成」：没有素材时提示先手写；有 README 时建议填进文本框，保存后 `space.toml` 里有 `description`；
- 指挥台说一个跨空间任务：先出计划卡（步骤、依赖都显示，只有「确认执行 / 拒绝」），这时两个空间都没有子会话；
  确认后两个子任务挂在调度者卡片下面，code 子任务写文件的审批卡直接显示在它的卡片上，批准后完成，调度者汇总；
- 左栏的两个子会话带「派」标记；点调度者卡片进对话视图，头部显示「指挥台 / …」，历史只显示一遍；
- 拒绝计划：调度者不派任何子任务，回复「计划取消了」；
- `@finance 任务`：照旧直接下发，不带「调度」「派」标记；
- 刷新页面：任务卡的父子关系从后台恢复。

**没做的**：没有接真实模型实测。真实模型能不能按描述挑对空间、会不会乖乖先 `propose_plan`，
要你用 `uv run sa serve`（开发模式，端口 8385）加真实 key 试一下。代码层面的硬性检查兜底了
「跨空间必须先确认」，模型不听话只会收到报错，不会越权。

**测试稳定性**：最终代码的全量测试跑了 4 遍，有 1 遍 `tests/serve/test_web.py::test_pinned_session_stays_on_top` 失败，
单独跑能过。这是已有的时序问题：6 个会话在同一毫秒内创建，`updated_at` 相同时排序靠随机的 id 后缀，
和这次的改动无关，已单独开了一个修复任务。

## 7. 已知限制和后续可以做的

- **只经过调度者中转**：大产物（报告、数据文件）只能靠在任务描述里写路径。以后可以加每次编排一个共享目录，
  需要给 Policy 加额外可写目录；外部 CLI 的 safe 档写不进去。
- **safe 档外部 CLI 空间只能读**：调度者知道这一点，不会把写的活派过去，但这类空间能干的活有限。
- **调度者不带记忆和技能**：像「理财的事交给 finance」这类偏好，现在只能写进空间简介。
  以后可以把记忆索引只读地放进调度者的 prompt。
- **空间清单在调度会话开始时冻结**：会话中途新建或改了空间，下一个调度会话才看得到。
  dispatch 按当前的空间列表校验，不会派到已经关掉、删掉的空间。
- **子任务结果只回传最后一条回复**，最多 4000 字，全文在子会话里；中间过程不进调度者的上下文，这正是隔离的意义。
- **serve 重启**：正在跑的调度和子任务都会中断（同一个进程），和普通会话一样。
- 调度会话在左栏看不到，只能从指挥台卡片进。卡片超过 24 小时就不再恢复，以后要回看得有个入口，比如按空间浏览的「查看全部」。
