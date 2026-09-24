# 空间新建后支持修改执行者，切换只影响新会话

- 日期：2026-09-24
- 对比基线：`04b9873`（M7：记忆 + 技能 + 项目指令（AGENTS.md、memory/、SKILL.md））
- 对应里程碑：M8 前置（工作台，W 系列）

## 功能变化

- 新增：空间建好之后可以修改执行者（`simpleagent` / `claude-code` / `opencode`），走 `PATCH /api/spaces/{id}` 带 `executor` 等字段，前端复用「新建空间」弹窗改名为「空间设置」。
- 新增：切换执行者只影响**之后新建的会话**。会话的执行者在创建时锁在 `meta.agent`，runner 按 `meta.agent` 分路，不再看 `space.executor`。
- 新增：`meta.agent != space.executor` 的老会话变为只读——发消息 / 重跑 API 直接返回 409；即使绕过前端调 API，runner 在真正执行前也会拒绝（发一帧 `error`，不落用户消息、不改会话状态）。切回原执行者即可继续。
- 新增：空间还有会话在跑时切执行者返回 409；`kind`（任务形态）、`cwd`（工作目录）不开放修改。
- 新增：前端空间卡片加了 ⚙「空间设置」按钮；老会话在列表里显示原执行者的徽标（变淡表示只读），输入框被禁用并提示原因。
- 升级：`docs/design/client-ui.md` 补充「建好之后能切执行者」的设计说明和 `SpaceStore.change_executor` 签名。

## 函数级改动

### `src/simpleagent/spaces/models.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `locked_reason(session_executor, space_executor)` | 新增 | 会话被锁住时给人看的原因文案，runner 和 API 共用，避免两处措辞不一致 |
| `validate_executor(executor, *, cli_model, permission, command)` | 新增 | 把原来内嵌在 `Space.from_spec` 里的「谁跑」字段合法性校验抽出来，供新建（`from_spec`）和切换（`change_executor`）共用 |
| `Space.from_spec()` | 修改 | 校验逻辑改为调用 `validate_executor`，行为不变 |

### `src/simpleagent/spaces/store.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `UPDATABLE_FIELDS` | 新增（模块级常量） | `update_space` 能逐字段改的白名单，从函数内部的局部变量提升为模块常量，供 `app.py` 复用做前置校验 |
| `SpaceStore.change_executor(space_id, executor, *, profile=None, cli_model=None, permission=SAFE, command=None, args=None)` | 新增 | 切换空间执行者，一次性改完 `executor` / `cli_model` / `permission` / `[agent]` 这组关联字段，不走 `update_space` 的逐字段 `setattr`；切到 `simpleagent` 会清空外部 CLI 的配置；同一执行者只改权限时保留原有的 `command` / `args`；`profile` 两种执行者都保留，切回内置时接着用 |
| `SpaceStore.update_space()` | 修改 | 内部改为引用模块级 `UPDATABLE_FIELDS`，行为不变 |

### `src/simpleagent/spaces/__init__.py`

| 改动 | 说明 |
|---|---|
| 导出 `locked_reason`、`validate_executor` | 让 `serve/app.py`、`serve/runner.py` 可以从包顶层导入 |

### `src/simpleagent/serve/app.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Server._update_space()`（`PATCH /api/spaces/{id}`） | 修改 | 把请求体拆成「谁跑」这组字段（`executor` / `cli_model` / `permission` / `command` / `args`）和普通字段两部分；先校验完所有字段合法性再动手，避免执行者已切、后面字段才报错导致空间处于半改状态；空间有会话在跑（`runner.space_busy`）时拒绝并返回 409；`executor` 组字段调用 `store.change_executor`，其余字段仍走 `update_space` |
| `Server._locked(space_id, session_id)` | 新增 | 判断会话是否因空间切换执行者而被锁住，锁住则返回 `locked_reason` 文案；供发消息 / 重跑接口提前拦截，让前端同步拿到 409 而不必等 SSE 里的 error 帧 |
| `Server._session_input()` | 修改 | 增加 `self._locked()` 检查，锁住时返回 409 |
| `Server._session_rerun()` | 修改 | 同上，增加锁定检查 |

### `src/simpleagent/serve/runner.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Runner.space_busy(space_id)` | 新增 | 判断空间是否有会话正在跑（看内存里的 `_tasks`，而不是可能因进程崩溃卡死在 `running` 的 `meta.status`），切执行者前先问一下 |
| `Runner._run_input()` | 修改 | 改为读会话的 `meta.agent` 作为执行者（而不是 `space.executor`）；`meta.agent != space.executor` 时说明空间中途切过执行者，发布 `error_frame`（用 `locked_reason` 文案）后直接返回，不落用户消息、不改状态、不走 `_finalize` |
| `Runner._run_cli()` | 修改 | 新增 `executor` 参数，内部不再读 `space.executor`，改用调用方传入的会话执行者，日志和错误文案同步替换 |
| `Runner._build_agent()` | 修改 | 新增 `executor` 参数，校验和报错文案改用传入的执行者 |

## 配置与依赖

无变化，不需要用户手动处理。

## 测试

- 新增：`tests/serve/test_change_executor.py`，8 个测试：新会话切执行者后使用新的（`test_new_session_uses_new_executor`）、老会话被 runner 拒绝执行（`test_locked_session_rejected_by_runner`）、切回原执行者后解锁（`test_switch_back_unlocks`）、空间有会话在跑时拒绝切换（`test_space_busy`）、`PATCH` 接口的正常切换往返（`test_patch_executor_roundtrip`）、非法输入被拒（`test_patch_executor_rejects_bad_input`）、运行中切换的 409（`test_patch_executor_conflicts_while_running`）、锁住的会话发消息返回 409（`test_locked_session_input_returns_409`）。
- 修改：`tests/spaces/test_store.py` 新增 6 个测试，覆盖 `change_executor` 的内置转 CLI、CLI 转内置、同执行者保留手写命令、非法输入拒绝且不落盘、`update_space` 拒绝改 `executor`、老会话的 `meta.agent` 不受空间切换影响。
- 结果：`uv run ruff format --check` 157 个文件已是标准格式；`uv run ruff check` 全部通过；`uv run pytest -q` 691 passed（含本次新增的 14 个）。
- 手动在浏览器里验证尚未做（待确认：⚙ 弹窗的实际交互、锁住会话的徽标和禁用效果）。

## 相关笔记

无
