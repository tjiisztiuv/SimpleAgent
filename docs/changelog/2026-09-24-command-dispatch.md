# M8 第一部分：指挥台跨空间调度（调度者 + propose_plan/dispatch）+ 空间简介自动摘要

- 日期：2026-09-24
- 对比基线：`943573c`（update）
- 对应里程碑：M8（第一部分，「子 agent」用各个空间实现；`task` 工具、`todo`、hooks 还没做）

## 功能变化

- 新增：指挥台不以 `@` 开头的输入交给**调度者**——保留空间 `sp_command`（左栏不显示）里的一个内置
  loop 会话，只有 `propose_plan` / `dispatch` 两个工具，不读写文件、没有记忆、没有技能、不接 MCP。
  它按各空间的「简介」挑合适的空间派发，子任务是目标空间里的普通会话（`meta.parent_session_id`
  指回调度者），跑完把「状态 · 改动文件 · 验证 · 最后的回复」汇总回来。
- 新增：**跨空间先出计划卡**。只涉及一个空间直接派；要派给第二个不同的空间时，代码强制要求先
  `propose_plan`（不只靠 prompt），计划要点「确认执行」才会真正派发；计划卡没有「始终允许」。
  互不依赖的步骤在同一条回复里并行 `dispatch`，有依赖的步骤等前一步结果回来再派。
- 新增：`@空间名 任务` 保持原来的直接下发，不经过调度者。
- 新增：面板里子任务卡片**缩进挂在调度者卡片下面**（`↳`），调度者卡片带「调度」标记；左栏里被
  派发的子会话带「派」标记。点任意卡片都能跳进对应会话；刷新页面后父子关系能从后台恢复。
- 新增：空间设置弹层新增「**简介**」文本框（上限 200 字，新建空间时隐藏，只在「空间设置」里显示），
  可点「自动生成」让模型读工作目录的 `AGENTS.md`（没有就读 `CLAUDE.md`）、`README.md` 开头、顶层文件
  列表和最近 10 个会话标题，写一段 50～100 字的简介。**生成结果只填进文本框，不自动保存**，没有素材
  可读时会提示先手写一句。对应新接口 `POST /api/spaces/{id}/describe`。
- 新增：`GET /api/sessions/{id}` 多返回 `seq`；`GET /api/meta` 多返回 `command_space_id`；
  `/api/panel/summary` 的条目和 `GET /api/sessions/{id}/summary` 多返回 `parent_session_id`。
- 新增：指挥台空间（`sp_command`）的 `PATCH`/`DELETE` 一律返回 400（系统空间，执行者和模型走
  `config.toml` 的 `[command]`，不允许改名/删除）。
- 文档：新增 `docs/design/command-dispatch.md`（方案、取舍、函数级改动、验证记录）；
  `docs/ROADMAP.md` 的 M8 标为进行中并写明进度；`docs/ARCHITECTURE.md` 代码结构加 `command/`、
  `spaces/describe.py`；`docs/design/client-ui.md` 新增 10.14 节。

## 修复

- 修复历史消息重放两遍的老 bug：页面在后台时打开会话，`subscribe` 直接返回、`lastSeq` 仍是 0；
  切回前台续传时带的是 `last_event_id=0`，服务端把这个会话的所有帧重放一遍，叠在已经画好的历史上
  （重放出来的审批卡按钮还能点，容易误导人）。修法：`GET /api/sessions/{id}` 返回读完历史后的
  帧号 `seq`，前端 `subscribe(id, { from: seq })` 只订阅这之后的帧。
- 修复 `Runner._outcomes` 内存泄漏（review 时发现）：原先每个会话收口时都往 `_outcomes` 里记一笔，
  只有 `run_child` 会取走，普通会话的记录永远留在内存里，`serve` 跑得越久占用越大；取消路径也不会
  `pop`。改为 `run_child` 先登记占位（`self._outcomes[meta.id] = None`），`_finalize` 只填已登记过
  的会话，`run_child` 在 `finally` 里 `pop` 取走。`tests/serve/test_command.py` 新增
  `runner._outcomes == {}` 的断言防止再次回归。
- 修复空间简介里的控制字符（如 `\x07`）会原样写进 `space.toml`、导致该空间从左栏消失（review 时
  发现）：控制字符写进 TOML 字符串后解析会失败，`list_spaces` 遇到解析失败的空间会直接跳过。新增
  `spaces/models.py` 的 `one_line()`（控制字符换成空格、连续空白含换行拍平成一个空格），
  `clean_description()` 和 `spaces/describe.py` 的 `clip()` 共用它；`tests/spaces/test_store.py`
  加了对应用例。
- 修复计划卡里 `after` 步骤号未转义的问题（review 时发现）：`planHtml()` 拼 `after` 列表时补上
  `escapeHtml`。
- 修复指挥台面板标题被说明文字挤成两行（「指挥 / 台」）：说明文字改短。
- 修复子会话不出现在左栏对应空间下的问题：`pollDispatch()` 轮询发现变化时顺带 `refreshSessions()`
  刷新左栏。

## 函数级改动

### `src/simpleagent/command/__init__.py`（新增文件）

模块级 re-export：`command_prompt`、`spaces_section`（来自 `prompt.py`），
`ChildResult`、`Dispatcher`、`command_tools`、`resolve_space`（来自 `tools.py`）。没有自己的实现。

### `src/simpleagent/command/prompt.py`（新增文件）

| 函数 / 类 | 说明 |
|---|---|
| `COMMAND_PROMPT` | 调度者的固定 system prompt 主体：角色、做法（先判断单/多空间、需要多空间先 `propose_plan`）、规则（只读空间不能派写活、任务不清楚先问、计划被拒不能绕开私自派） |
| `_where(space)` | 这个空间的目录说明：绑定目录的空间给 `cwd`，通用空间给「临时目录」提示 |
| `_ability(space)` | 这个空间能干什么：内置执行者读写都行（写要批准）；外部 CLI 按权限档分「全放行」/「只读」 |
| `spaces_section(spaces)` | 拼出 prompt 里的「可用的空间」一节：每个空间的名字、id、简介（没写则提示只能靠名字和目录判断）、执行者能力、目录 |
| `command_prompt(spaces, now=None)` | 拼出调度者完整 system prompt：主体 + 日期（星期几）+ 空间清单；会话开始时拼一次，之后不变（保前缀缓存） |

### `src/simpleagent/command/tools.py`（新增文件）

| 函数 / 类 | 说明 |
|---|---|
| `ChildResult`（dataclass） | 一个子任务的结果：`space_id`/`space_name`/`session_id`/`status`/`reply`/`reason`/`files`/`verification` |
| `ChildResult.render()` | 把结果拼成一段文字回给调度者：状态、失败原因、改动文件、验证结果、最后回复（超过 `REPLY_LIMIT`=4000 字截断并注明全文在哪个子会话） |
| `Dispatcher`（Protocol） | 工具需要的调度能力：`targets()` 返回可派的空间列表，`async run_child(space_id, task, *, parent)` 建子会话并等它跑完；`Runner` 实现它，测试里可以换假的 |
| `resolve_space(targets, ref)` | 按 id 精确匹配 → 名字精确匹配 → 名字忽略大小写匹配找空间；重名要求用 id；不能解析成指挥台自己 |
| `DispatchArgs` / `PlanStep` / `PlanArgs`（pydantic） | `dispatch`/`propose_plan` 的参数模型；`PlanStep.after` 是依赖的步骤号列表 |
| `_TurnState`（dataclass） | 一轮调度内的状态：`used`（本轮派过的空间）、`plan`（本轮批准过的计划涉及的空间，`None`=还没有）、`running`（正在跑的空间）；随 `command_tools()` 每轮新建 |
| `command_tools(dispatcher, *, parent, approver)` | 生成这一轮用的 `propose_plan`、`dispatch` 两个 `Tool`：内部闭包 `propose_plan(args, ctx)` 先校验步骤（`after` 只能指向前面的步骤号）、无审批器时直接拒绝、批准后记下 `state.plan`；闭包 `dispatch(args, ctx)` 按 `state.plan`/`state.used`/`state.running` 做「跨空间必须先确认」「计划外空间拒绝」「同一空间不许并发」三道检查（检查与登记之间没有 `await`，并行调用也按顺序过关），再调 `dispatcher.run_child()`。`propose_plan` 设 `readonly=False`（保证同批里先确认后派发），`dispatch` 设 `readonly=True`（允许并行） |

### `src/simpleagent/spaces/describe.py`（新增文件）

| 函数 / 类 | 说明 |
|---|---|
| `DescribeError`（Exception） | 没拿到简介（API 报错、模型没给正文），HTTP 层转 502 |
| `_read_head(path, limit=DOC_CHARS)` | 读一个文件开头（`DOC_CHARS`=2000 字），空文件返回 `None`，超长加省略提示 |
| `_first_existing(cwd, names)` | 按顺序找第一个存在的文件（`AGENTS.md`/`CLAUDE.md`、`README.md`/`README`/`readme.md`） |
| `_listing(cwd)` | 顶层文件列表：跳过隐藏文件，目录名带 `/`，最多 `LIST_LIMIT`=50 项 |
| `gather_material(space, cwd, titles)` | 拼出给模型看的素材（指令文件开头、README 开头、目录列表、去重后最近 `TITLE_LIMIT`=10 个会话标题，过滤掉「新会话」）；什么都没有返回 `None` |
| `clip(text)` | 把模型输出整理成一行：`one_line()` 拍平 + 去掉首尾引号，超过 `DESCRIPTION_MAX` 兜底截断加省略号 |
| `describe_space(llm, material)` | 发一次不带工具的请求拿简介；`llm.stream()` 抛异常统一转成 `DescribeError`；没拿到正文也抛 `DescribeError` |

## 函数级改动（修改的文件）

### `src/simpleagent/spaces/models.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `COMMAND_SPACE_ID` / `COMMAND_SPACE_NAME` | 新增 | `"sp_command"` / `"指挥台"`，调度者住的保留空间 |
| `DESCRIPTION_MAX` | 新增 | 空间简介字数上限 200 |
| `one_line(text)` | 新增 | 把控制字符换成空格、把连续空白（含换行）拍平成一个空格、去首尾空白；供 `clean_description`、`describe.clip` 共用，防止控制字符写进 `space.toml` |
| `clean_description(text)` | 新增 | 整理空间简介：`one_line()` 后超过 200 字直接 `ValueError`（不悄悄截断） |
| `SessionMeta.parent_session_id` | 新增字段 | 指挥台派发的子会话记着调度者的会话 id，创建时定下、不开放给 `update_meta` 修改 |
| `SessionMeta.from_dict` | 修改 | 读取 `parent_session_id` |
| `Space.description` | 新增字段 | 默认空字符串，调度者靠它决定把任务派给谁 |

### `src/simpleagent/spaces/store.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `UPDATABLE_FIELDS` | 修改 | 加入 `"description"` |
| `_space_to_toml(space)` | 修改 | `description` 非空时写入 |
| `_space_from_toml(data, space_id)` | 修改 | 读取 `description`（老文件没有这一行时给默认空串） |
| `SpaceStore.ensure_command_space()` | 新增 | 调度者住的系统空间不存在就建：`opened=False`（左栏不显示），执行者固定内置 loop，`sa serve` 启动时调用 |
| `SpaceStore.update_space(space_id, **fields)` | 修改 | 改 `description` 字段时先过一遍 `clean_description()` |
| `SpaceStore.create_session(space_id, agent=None, *, parent=None)` | 修改 | 新增关键字参数 `parent`，写入新会话的 `parent_session_id` |

### `src/simpleagent/config.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `CommandConfig` | 新增 | 对应 `[command]`：`profile: str \| None`（不填用 `default_profile`） |
| `Config.command` | 新增字段 | 默认工厂 `CommandConfig` |
| `Config._check_default_profile` | 修改 | 追加校验：`command.profile` 填了但不在 `profiles` 里则报配置错误 |

### `src/simpleagent/serve/app.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Server.start()` | 修改 | 先调用 `self.store.ensure_command_space()` 再启动 `runner` |
| `Server.handle(...)` | 修改 | 新增路由：`POST /api/spaces/{id}/describe` → `_describe_space` |
| `Server._meta()` | 修改 | 返回体新增 `command_space_id` |
| `Server._update_space(space_id, body)` | 修改 | `space_id == COMMAND_SPACE_ID` 时直接返回 400（系统空间不可改） |
| `Server._describe_space(space_id)` | 新增 | 调 `runner.describe(space_id)`，同步等结果（超时 `DESCRIBE_TIMEOUT`=60 秒）；空间不存在 404，素材为空 400，模型出错 502，超时 504 并取消后台任务；成功返回 200 `{"description": ...}`，**不落盘** |
| `Server._delete_space(space_id)` | 修改 | `space_id == COMMAND_SPACE_ID` 时直接返回 400（系统空间不可删除） |
| `Server._session_messages(session_id)` | 修改 | 返回体新增 `seq`（`runner.bus.next_seq(session_id) - 1`，即读完历史后的帧号） |
| `Server._panel_summary()` | 修改 | `running` 和 `recent` 的每个条目新增 `parent_session_id` |
| `Server._session_summary(session_id)` | 修改 | 返回体新增 `parent_session_id` |

### `src/simpleagent/serve/runner.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Runner.__init__` | 修改 | 新增 `self._outcomes: dict[str, tuple[str, str \| None] \| None]`，记调度者正在等的子会话的收口状态 |
| `Runner.describe(space_id)` | 新增 | 把 `_describe()` 扔进后台事件循环，返回 `concurrent.futures.Future[str]` 供 HTTP 层同步等待 |
| `Runner.targets()` | 新增（`Dispatcher` 协议实现） | 左栏打开着的空间，不含指挥台自己 |
| `Runner.run_child(space_id, task, *, parent)` | 新增（`Dispatcher` 协议实现） | 在目标空间新建子会话（带 `parent`），登记 `_outcomes` 占位，单独起一个 `asyncio.create_task` 跑 `_run_input` 并用 `shield` 包住等待；调度者被取消时按情况 `cancel()`（外部 CLI 杀进程组）或直接取消任务，再把取消传给自己；`finally` 里 `pop` 取走收口结果；组装 `ChildResult`（复用 `panel/summary.summarize` 取改动文件和验证状态，取最后一条 assistant 消息当 `reply`） |
| `Runner._finalize(...)` | 修改 | 收口时若 `session_id in self._outcomes`，把 `(status, reason)` 写进去，供 `run_child` 取走 |
| `Runner._build_agent(space, session, executor)` | 修改 | `space.id == COMMAND_SPACE_ID` 时转给新增的 `_build_commander()` |
| `Runner._build_commander(space, session)` | 新增 | 造调度者的 `Agent`：模型用 `config.command.profile` 或 `default_profile`；空间清单 `command_prompt(self.targets())` 在会话开始时拼一次并缓存进 `self._prompts`；工具只有 `command_tools(self, parent=session.id, approver=...)`；单独一份 `APIApprover`（独立的「始终允许」记录，不受其他会话影响） |
| `Runner._describe(space_id)` | 新增 | 读空间目录和最近会话标题（`gather_material`），素材为空抛 `ValueError`；固定用 `default_profile` 经 `llm_factory` 构造 LLM（构造失败转 `DescribeError`），调 `describe_space()`，`finally` 里 `llm.close()` |

### `src/simpleagent/web/app.js`

| 函数 | 变化 | 说明 |
|---|---|---|
| `req(method, path, data, timeoutMs)` / `api.post(p, d, t)` | 修改 | 支持单个请求自定义超时（默认仍是 20 秒），自动生成简介用 70 秒 |
| `findSpace(id)` | 新增 | 在左栏空间和指挥台系统空间里找空间；`renderHeader` / `selectSession` / `renderLogs` / `exportMarkdown` / `runCommand` / 启动时恢复会话都改用它 |
| `loadCommandSpace()` | 新增 | 拉 `/api/spaces/sp_command` 存进 `state.commandSpace`；`loadSpaces` / `refreshSessions` 调用 |
| `boot()` | 修改 | 从 `/api/meta` 记下 `command_space_id`；绑定「自动生成」按钮 |
| `renderSpaces()` | 修改 | 会话行 `parent_session_id` 不为空时显示「派」标记 |
| `planHtml(argsText)` | 新增 | 把 `propose_plan` 的审批参数按步骤渲染（依赖步骤号也转义），解析不了就原样显示 |
| `addApprovalCard(...)` | 修改 | 计划审批显示为「确认执行计划」，按钮只有「确认执行 / 拒绝」 |
| `subscribe(sessionId, { resume, from })` | 修改 | 新增 `from`：从给定帧号之后订阅（修历史重放两遍） |
| `selectSession(spaceId, sessionId)` | 修改 | 用 `GET /api/sessions/{id}` 返回的 `seq` 调 `subscribe(id, { from: seq })` |
| `restoreDispatch(summary)` | 修改 | 恢复的卡片带 `parentId` |
| `dispatchLine(d, cls)` | 修改 | 计划审批显示「等你确认计划」 |
| `renderDispatch()` | 修改 | 按 `parentId` 排成树，子任务缩进（`↳`）；调度者卡片带「调度」标记；计划审批按步骤显示、无「始终允许」；卡片索引改为指向排好序的列表 |
| `pollDispatch()` | 修改 | 有变化时刷新左栏；调度者在跑时每轮刷新 summary，把新派出的子任务补成卡片 |
| `dispatch()` | 修改 | 不以 `@` 开头的交给 `dispatchToCommander()`；`@` 开头保持直派，卡片带 `parentId: null` |
| `dispatchToCommander(text)` | 新增 | 在 `sp_command` 建会话并发送原话，失败时把原话还给输入框 |
| `openModal(sp)` / `saveSpaceSettings()` | 修改 | 「简介」栏只在空间设置里显示；保存时带上 `description` |
| `generateDescription()` | 新增 | 调 `POST /api/spaces/{id}/describe`，结果只填进文本框；弹窗已关或换了空间时丢弃结果 |

### `src/simpleagent/web/index.html` / `styles.css`

- `index.html`：设置弹层新增「简介」文本框和「自动生成」链接；指挥台说明文字、输入框占位文字改写。
- `styles.css`：`.field textarea`、`.field-label`；`.dispatch.child`、`.dispatch .arrow`；`.plan` 系列；`.badge.b-cmd`。

## 配置与依赖

- 新增可选配置段 `[command]`（`src/simpleagent/config.example.toml` 已加注释示例）：
  ```toml
  [command]
  profile = "deepseek"   # 调度者用的模型，不填用 default_profile
  ```
  不写这一段行为不变（等同 `profile = None`，用 `default_profile`）。填了一个不存在的 profile 会在
  配置加载时报错。
- 无新增第三方依赖，不需要 `uv sync`。
- `space.toml` 新增可选字段 `description`，老文件没有这一行照常读取（默认空串）。
- 数据目录新增一个系统空间 `spaces/sp_command/`（`sa serve` 启动时自动创建，左栏不显示）。
- **需要手动处理**：没有必须做的。建议给常用空间写一句简介（左栏 ⚙ →「简介」，可点「自动生成」），
  没写简介的空间调度者只能凭名字和目录猜，像 `code`、`finance` 这类名字很难看出该派给谁。

## 测试

- 新增 `tests/test_command_tools.py`（12 个）：单空间直接派；第二个空间没有计划时被拒；并行派两个
  空间时只有第一个放行；批准计划后并行执行（用闸门验证两个子任务确实同时在跑）；计划被拒；计划外的
  空间被拒；错误计划（`after` 越界）不会拿去问人；无人值守时拒绝；同一空间同一批派两次被拒；按
  id/大小写/重名查找；结果截断和失败原因；prompt 里的空间清单渲染。
- 新增 `tests/serve/test_command.py`（7 个）：Runner + FakeLLM 端到端——单空间派发（子会话记
  `parent_session_id`、调度者只有两个工具、子会话没有 `dispatch`）；跨空间审批（批准前没有任何子
  会话，批准后两个都完成）；取消调度者时子任务跟着取消；子任务起不来时失败原因回到调度者；`targets()`
  跳过关掉的空间；指挥台空间的 HTTP 保护和 `parent_session_id`；`[command] profile` 校验非法值。
- 新增 `tests/serve/test_describe.py`（5 个）：`PATCH` 简介和 200 字上限；自动生成返回建议但不保存
  （去引号、不带工具、用完关闭客户端）；没有素材返回 400；模型出错返回 502、空间不存在返回 404；
  素材拼接（`AGENTS.md` 优先于 `README`、README 截断、跳过隐藏文件、标题去重去掉「新会话」）。
- `tests/spaces/test_store.py` 新增 2 个：简介在 `space.toml` 里读写往返（引号、换行、控制字符拍平）、
  超长被拒（`ValueError` 且不落盘）。
- `tests/serve/test_serve.py` 新增 1 个：会话接口带 `seq`，从这个帧号续传不会重放历史帧。
- 测试结果：`uv run pytest -q` → **718 passed**（改动前 691，新增 27 个）。
- `uv run ruff check`：All checks passed。
- `uv run ruff format --check`：166 files already formatted（无需改动）。
- 已知的不稳定测试：`tests/serve/test_web.py::test_pinned_session_stays_on_top`（6 个会话在同一毫秒
  内创建，`updated_at` 相同时排序靠随机的 id 后缀，单独跑能过），全量跑 4 遍里出现过 1 次，与本次
  改动无关，已另开任务跟踪，不影响本次的 718 passed 结果。

### 手动验证（浏览器）

用临时 `SIMPLEAGENT_HOME` + 脚本化 FakeLLM 起服务（不联网，不碰 `~/.simpleagent*`），走通：

- 空间设置「自动生成」：没有素材时提示先手写；有 `README.md` 时生成建议并填进文本框，保存后
  `space.toml` 里出现 `description`；
- 指挥台说一句跨空间任务：先出计划卡（步骤、依赖都显示，只有「确认执行 / 拒绝」），此时两个目标
  空间都还没有子会话；点「确认执行」后两个子任务挂在调度者卡片下面，其中一个空间写文件的审批卡
  直接显示在它自己的卡片上，批准后完成，调度者汇总结果；
- 左栏两个子会话都带「派」标记；点调度者卡片进对话视图，头部正确显示「指挥台 / …」，历史只显示
  一遍（验证了「打开会话历史重放两遍」的修复）；
- 拒绝计划：调度者不派任何子任务；
- `@finance 任务`：照旧直接下发，不带「调度」「派」标记；
- 刷新页面：任务卡的父子关系从后台正确恢复；
- `curl` 验证：`sp_command` 的 `PATCH`/`DELETE` 都返回 400。

**没有用真实模型验证**：调度者能否按空间简介准确选对空间、遇到跨空间任务是否会乖乖先调用
`propose_plan`，需要用 `uv run sa serve`（开发模式，端口 8385）配真实 key 实测。代码层面的硬性检查
（`command/tools.py` 里的三道校验）兜住了「跨空间必须先确认」，即使模型不听话也只会收到工具报错，
不会绕过确认直接派发。

## 相关笔记

- 无（M8 还没结束，`docs/notes/` 学习笔记按约定在整个里程碑完成后统一写；方案、取舍和详细改动记录
  见 `docs/design/command-dispatch.md`）。
