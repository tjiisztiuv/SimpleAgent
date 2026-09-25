# 权限模式开关：只读 / 工作区 / 全放行，默认工作区

- 日期：2026-09-25
- 对比基线：`c92dec2`（版本号 0.2.1）
- 对应里程碑：M3（补做剩下的一半）

## 功能变化

- 新增：权限模式开关——只读 / 工作区 / 全放行，一个旋钮定下每类调用是放行、问还是拒，照 dsh 的
  「沙箱模式 + 审批策略」预设。默认改成**工作区**：REPL、`sa run`、客户端里改工作目录内的文件不再
  询问；bash 仍要问；越界写、危险命令照样拒绝。想回到升级前的行为，在 `config.toml` 写
  `[permissions] mode = "read-only"`。完整方案和按步骤的函数级改动记录见
  [docs/design/permission-mode.md](../design/permission-mode.md)，调研依据见
  [docs/research/2026-09-25-agent-permission-control.md](../research/2026-09-25-agent-permission-control.md)。
- 升级：`sa run` / 定时任务默认跟配置走，但**不继承「全放行」**——配置里改成全放行只影响交互场景，
  无人值守要全放行得显式 `sa run --mode full` 或 `schedules.toml` 里 `mode = "full"`；开跑时 stderr
  多打一行「权限：只读 · 允许：bash」。
- 升级：`sa schedule list` 的每行显示从「工具 只读工具」改成「权限 工作区 · 允许 bash」。
- 升级：REPL 新增 `/mode` 命令，不带参数列出三档并标出当前档，带参数切换，下一次工具调用起生效；
  启动时打一行「权限：工作区（/mode 切换）」，全放行时标红；`sa --mode 只读` / `sa --mode 全放行`
  可以在启动时指定这一次用哪个模式。
- 升级：空间的 `permission` 字段取值改成 `read-only` / `workspace` / `full`；`None` 表示没单独设过
  （内置执行者跟配置默认、外部 CLI 按只读）；旧值 `safe` 读作只读（懒迁移，保存一次空间设置才写回
  `read-only`）；内置执行者的空间现在也会在 `space.toml` 里写 `permission`（以前只有外部 CLI 写）。
- 升级：客户端「空间设置」的权限下拉对内置执行者也开放（三档），外部 CLI 仍是两档（只读 / 全放行，
  还没接「工作区」）；下拉下面一行说明放行范围，选全放行时变成红色警示；空间头部新增「权限：工作区」
  标签，全放行标红；改了权限后，这个空间里正在跑的会话从下一次工具调用起按新模式判定，不用等这一轮
  结束。
- 升级：`/api/meta` 的 `executors[].permissions` 每项多了 `description`；内置执行者的
  `default_permission` 按配置默认给；空间视图多了 `mode` 字段（实际生效的那档）。
- 修复：客户端实时收到的审批卡看不到「为什么要问」——`approval_request_frame` 没带 `reason`，只有
  刷新后从 `GET /api/approvals` 补出来的卡才有理由；现在实时帧也带上 `reason`。
- 修复（发布前审查发现）：`Runner._build_agent()` 原来用 `_run_turn` 开头读到的 `space` 对象定模式，
  等 MCP 启动期间在空间设置里改了模式，这一整轮仍按旧模式跑；改成从存储里现读空间。
- 修复（发布前审查发现）：打开指挥台会话时头部误显示「权限：工作区」——调度者没有文件工具、计划卡
  每次都要人确认，模式对它不起作用；现在指挥台不再显示这个标签。

## 函数级改动

### `src/simpleagent/permissions.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Mode`（`READ_ONLY` / `WORKSPACE` / `FULL`） | 新增 | 权限模式枚举，带 `.label` 中文名 |
| `MODE_LABELS` / `MODE_SUMMARY` | 新增 | 模式的中文名、一句话说明，REPL 和客户端共用 |
| `parse_mode(text)` | 新增 | 把用户敲的模式名（中英文都认）转成 `Mode`，认不出来抛 `ValueError` |
| `Scope.file_edit` | 新增字段 | 标记这次调用的全部副作用就是改 `paths` 里的文件，工作区模式只自动放行这一类 |
| `Policy.__init__` | 修改 | 新增 `mode: Mode = Mode.READ_ONLY` 参数，存成可变属性 `self.mode` |
| `Policy.decide()` | 修改 | 判定顺序改为：禁用 → 危险命令 → 全放行 → 越界 → 默认等级 allow → 工作区+`file_edit` → 问；危险命令挪到全放行之前保证也拦；越界的拒绝理由带上当前模式和「切到全放行」的出路 |

### `src/simpleagent/tools/write_file.py`、`src/simpleagent/tools/edit_file.py`

| 改动 | 说明 |
|---|---|
| `scope` lambda 加 `file_edit=True` | 声明这两个工具属于「只改文件」，工作区模式据此放行 |

### `src/simpleagent/config.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `PermissionsConfig`（`mode`） | 新增 | 交互场景默认权限模式，中文名也认；`Config.permissions` |
| `PermissionsConfig.unattended(explicit)` | 新增 | 无人值守用哪个模式：显式给了就用，否则跟配置但不继承「全放行」 |

### `src/simpleagent/cli.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `mode_arg(text)` / `MODE_HELP` | 新增 | `--mode` 的 argparse 类型转换，认不出来列出可选值 |
| `list_schedules()` | 修改 | 每行显示从「工具 只读工具」改成「权限 … · 允许 …」 |
| `main()` | 修改 | 顶层和 `run` 子命令都加 `--mode`，分别传给 `Repl` / `Headless` |

### `src/simpleagent/ui/repl.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `COMMANDS["mode"]` / `HELP` | 新增 | `/mode` 命令说明 |
| `Repl.__init__` | 修改 | 新增 `mode` 参数，`Policy(cwd, mode=mode or config.permissions.mode)` |
| `Repl.run()` | 修改 | 启动时打一行「权限：…（/mode 切换）」，全放行标红 |
| `Repl._set_mode(text)` | 新增 | 不带参数列出三档和当前档，带参数切换，写错报错且不改 |
| `completer()` | 修改 | `/mode` 补全三个模式值 |

### `src/simpleagent/ui/headless.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Headless.__init__` | 修改 | 新增 `mode` 参数，按 `config.permissions.unattended(mode)` 建 `Policy`；存 `self.allowed_tools` |
| `Headless.permission_line()` | 新增 | 开跑时往 stderr 打一行「权限：… · 允许：…」 |
| `Headless.run()` | 修改 | 调用 `permission_line()` 写 stderr |

### `src/simpleagent/scheduler/models.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Job.mode` | 新增字段 | `Mode \| None`，和 `sa run --mode` 同义，中文名也认 |

### `src/simpleagent/agents/base.py`

| 改动 | 说明 |
|---|---|
| `SAFE` / `FULL` | 改成 `Mode.READ_ONLY.value` / `Mode.FULL.value`，外部 CLI 和内置执行者共用同一套取值 |

### `src/simpleagent/spaces/models.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `LEGACY_PERMISSIONS` / `normalize_permission(value)` | 新增 | 兼容旧值 `safe` 和中文名，统一存成 `Mode` 的值 |
| `validate_executor()` | 修改 | 内置执行者不再强制 `SAFE`；外部 CLI 只收只读/全放行，选工作区报错说明原因 |
| `SpaceSpec.permission`、`Space.permission` | 修改 | 类型改为 `str \| None = None`，`None` 表示没单独设过 |
| `Space.effective_mode(default)` | 新增 | 计算这个空间实际生效的模式：设过的按设的，没设过的内置执行者跟配置默认、外部 CLI 按只读 |

### `src/simpleagent/spaces/store.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `DEFAULT_PERMISSION` | 删除 | 不再需要「旧文件按只读」的常量兜底 |
| `_space_to_toml()` | 修改 | 设过 `permission` 才写（两种执行者都写），没设过不写，读回来还是「跟配置默认」 |
| `_load_permission(value)` | 新增 | 旧值 `safe` 读成 `read-only`；手改写错了按只读处理，不让空间从列表里消失 |
| `SpaceStore.change_executor()` | 修改 | `permission` 两种执行者都存；没给就回到「没设过」，外部 CLI 的全放行不会带到内置执行者上 |

### `src/simpleagent/serve/app.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `_permission_options(executor)` | 新增 | 空间设置权限下拉的选项：内置三档（带 `description`），外部 CLI 两档 |
| `Server._meta()` | 修改 | `executors[].permissions` / `default_permission` 按执行者给 |
| `Server._space_view()` | 修改 | 新增 `mode` 字段（实际生效的模式） |
| `Server` 的空间更新路由 | 修改 | `permission` 不再兜底成 `safe`；改完调 `runner.apply_mode()` 让正在跑的会话立即切换 |

### `src/simpleagent/serve/runner.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Runner.apply_mode(space_id, mode)` / `Runner._apply_mode()` | 新增 | 把改模式的操作放到事件循环上，改掉这个空间里正在跑的内置会话的 `policy.mode` |
| `Runner._build_agent()` | 修改 | `Policy(cwd, mode=space.effective_mode(...))`；**从存储现读空间**（发布前审查修的时序 bug，见上） |
| `Runner._run_cli()` | 修改 | 用实际生效的模式（`space.effective_mode()`）拼外部 CLI 的启动参数和环境变量 |
| 调度者相关方法 | 修改 | 构建调度 prompt 时传入配置默认模式 |

### `src/simpleagent/serve/approval.py`、`src/simpleagent/serve/frames.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `approval_request_frame()` | 修改 | 新增 `reason` 参数，写进帧的数据里 |
| `APIApprover` 请求审批的方法 | 修改 | 调用 `approval_request_frame` 时带上 `req.reason` |

### `src/simpleagent/command/prompt.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `_BUILTIN_ABILITY` | 新增 | 内置执行者在各模式下能干什么，供调度者 prompt 用 |
| `_ability(space, default_mode)` | 修改 | 按空间实际生效的模式描述能力，而不是写死「读写都行」 |
| `spaces_section()`、`command_prompt()` | 修改 | 新增 `default_mode` 参数，透传给 `_ability` |

### `src/simpleagent/spaces/describe.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `gather_material()` | 修改 | 外部 CLI 的权限描述改看 `space.effective_mode()`，不再直接比较 `permission == "safe"` |

### `src/simpleagent/web/app.js`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `MODE_LABEL` / `fullAccessTitle(sp)` | 新增 | 模式的中文名映射；全放行提示文案按执行者区分 |
| `renderSpaces()`、`renderHeader()` | 修改 | 「全放行」标记看 `sp.mode`（不再只判断外部 CLI）；头部新增 `#ws-mode` 权限标签，全放行标红，**指挥台会话不显示** |
| `openModal()`、`syncModalFields()` | 修改 | 权限下拉对所有执行者显示；下拉下面一行说明取后端 `description`，选全放行时换成红色警示 |
| `executorFields()` | 修改 | 两种执行者都提交 `permission` 字段 |

### 前端文档/样式：`src/simpleagent/web/index.html`、`src/simpleagent/web/styles.css`

- `index.html`：头部新增 `#ws-mode` 标签；权限下拉 `#f-perm-wrap` 对所有执行者可见（不再 `hidden`）。
- `styles.css`：新增 `.field-hint.warn`（红色警示文案样式）。

## 配置与依赖

- 新增 `[permissions]` 配置表（`mode`，默认 `workspace`），`config.example.toml` 补了注释示例。
  **不需要手动处理**，但默认值变了——升级后 REPL / `sa run` / 客户端里改工作目录内的文件不再询问。
  想保留升级前的行为，在 `~/.simpleagent/config.toml` 里加：
  ```toml
  [permissions]
  mode = "read-only"
  ```
- `schedules.toml` 的 `Job` 新增 `mode` 字段（可选，不写就跟配置走且不继承全放行）；
  `schedules.example.toml` 的说明同步更新。**注意行为变了**：默认模式是工作区，没写 `mode` 的任务
  （和 `sa run`）不用列 `allowed_tools` 也能改工作目录里的文件；要保持升级前「写文件必须显式放行」，
  在任务里写 `mode = "read-only"`（或把配置默认改成只读）。定时任务的 daemon 还没做，目前受影响的是 `sa run`。
- 没有新增第三方依赖，不需要 `uv sync`。

## 测试

- `tests/test_permissions.py`：整张模式表（3 种模式 × 8 类调用）；`parse_mode`；工作区模式只信显式
  `file_edit`；越界拒绝理由写明模式和出路；写工具已声明 `file_edit`；中途切模式下一次调用生效。
- `tests/test_config.py`：`PermissionsConfig` 默认工作区；中英文取值；写错报错；`unattended()` 的
  五种组合。
- `tests/test_repl.py`：默认跟配置走、`--mode` 覆盖；`/mode` 列表和切换；写错不改；切到只读后下一次
  `write_file` 要问；启动行；`sa --mode yolo` 报错。
- `tests/test_complete.py`：`/mode ` 补全三个值。
- `tests/test_headless.py`：只读下没 `--allow` 拒写；工作区默认写工作目录内不用 `--allow`、bash 仍要问；
  不继承全放行；stderr 的权限行。
- `tests/test_schedules.py`：`sa schedule list` 按任务显示模式；写错的 `mode` 报错。
- `tests/spaces/test_store.py`：`effective_mode` 规则；内置空间的 `permission` 能存能读；外部 CLI 拒绝
  工作区；旧值 `safe`、中文名、写错值的读法。
- `tests/serve/test_serve.py`：实时审批帧带 `reason`；没设权限的空间按工作区跑、写文件不弹审批；
  `apply_mode` 只改这个空间的会话；新增回归测试 `test_build_agent_reads_the_latest_mode`（覆盖发布前
  审查发现的时序 bug）。
- `tests/serve/test_web.py`：`/api/meta` 的权限选项和默认值。
- `tests/test_command_tools.py`：调度者 prompt 按模式描述内置空间的能力。
- `tests/serve/test_change_executor.py`、`tests/serve/test_model_history.py`、`tests/test_debug.py`：
  跟着新默认值（工作区）调整既有断言（比如把测试空间显式设成只读，好让审批流程照旧可测；stderr 断言
  加上新的权限行）。
- 前端（`web/app.js` 的 `renderHeader()` 指挥台不显示权限标签）没有自动化测试，浏览器里手动确认过。
- 测试结果：`uv run pytest -q` 810 passed；`origin/main`（`c92dec2`）上是 759 个，这次净增 51 个。
- `uv run ruff check`：All checks passed。
- `uv run ruff format --check`：185 files already formatted。

## 手动验证

临时数据目录（`SIMPLEAGENT_HOME` 指向会话 scratchpad）+ `uv run sa serve`，没碰 `~/.simpleagent-dev`：

- API：没设过权限的内置空间返回 `permission=None`、`mode=workspace`；`permission="全放行"` 存成 `full`；
  `space.toml` 手改成旧值 `safe` 的外部 CLI 空间读出 `read-only`；外部 CLI 选 `workspace` 返回 400 和原因。
- 浏览器：权限下拉内置三档、默认选中工作区、下面一行说明；选全放行出现红色警示；保存后头部红色
  「权限：全放行」、左栏出现标记、`space.toml` 写入 `permission = "full"`；外部 CLI 空间下拉只有两档，
  旧值 safe 显示为只读；指挥台会话头部不显示权限标签。
- 终端：`sa --mode 只读` 启动行显示当前模式；`/mode` 列表、切换、写错报错；`sa --mode yolo` 被 argparse
  拒绝并列出可选值；`sa run --help` 里有 `--mode`。
- 没有用真实模型跑端到端（临时 profile 指向不存在的地址）：「工作区下写文件不弹审批」「切模式后下一次
  调用生效」由 FakeLLM 驱动的 Runner / REPL 测试覆盖。

## 相关笔记

- 无（学习笔记按里程碑结束统一写，M3 这部分是补做，暂未写 `docs/notes/`）。
