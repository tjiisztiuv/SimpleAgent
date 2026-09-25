# 权限模式：只读 / 工作区 / 全放行

> 状态：已实现（2026-09-25），还没提交。这份文档包括方案、按步骤的函数级改动记录和验证结果。
> 调研依据见 [research/2026-09-25-agent-permission-control.md](../research/2026-09-25-agent-permission-control.md)
> （pi、dsh、Claude Code 三家的权限控制对比）。

## 1. 要解决什么

M3 的权限只有一种姿态：读放行，写文件、跑命令每次都问，越界和危险命令直接拒。想少被打扰，只能在审批时按「a」
（本会话这个工具都放行），而它按工具名放行，对 bash 按一次就等于后面所有命令都不问了。

dsh 的做法是把「沙箱模式 + 审批策略」打包成一个预设，用户只拨一个旋钮。这次照这个思路，给 SimpleAgent 加一个
权限模式开关：**只读 / 工作区 / 全放行**。

## 2. 和你确认过的口径

| 问题 | 结论 |
|---|---|
| 打包方式 | 照 dsh：一个模式定下每类调用放行、问还是拒 |
| 默认模式 | **工作区** |
| bash 的系统级沙箱（Seatbelt） | 先搁置，以后做成一个可开关的选项 |
| 无人值守的默认模式 | 跟配置走，但**不继承「全放行」**（我提的，讲第 2 步时说明过，你没提异议） |
| 推进方式 | 第 1 步逐步确认；第 2、3 步你说「之后的你也都做了吧」，一口气做完，留这份文档 |

## 3. 方案

### 3.1 模式表

| 调用 | 只读 | 工作区（默认） | 全放行 |
|---|---|---|---|
| 读文件、搜索、只读 MCP | 放行 | 放行 | 放行 |
| 改工作目录里的文件（write_file / edit_file） | 问 | **放行** | 放行 |
| 改工作目录外的文件 | 拒 | 拒 | 放行 |
| bash、其他有副作用的工具（非只读 MCP、写记忆） | 问 | 问 | 放行 |
| 危险命令、配置里禁用的工具 | 拒 | 拒 | 拒 |

- 只读就是 M3 原来的行为，一格没变。
- 工作区相当于 Claude Code 的 acceptEdits、dsh 的 workspace-write。
- 全放行相当于 dsh 的 danger-full-access + never。危险命令黑名单照样拦：它是所有模式、所有前端共用的底线。

### 3.2 dsh 的两个旋钮在这里对应什么

- **沙箱模式 → 表里的放行范围**，由 `Policy` 按模式判定。
- **审批策略 → 表里「问」那一格交给谁**：有人（REPL / 客户端）问人；没人（`sa run` / 定时任务）按 `--allow`
  白名单答，名单外拒绝，也就是 dsh 的 `never`；外部 CLI 接不回我们的审批，也是 `never`。
- dsh 给全放行配 `never`，是因为全放行下没有要问的事。这张表里全放行那一列同样没有「问」，
  所以不需要单独的审批旋钮。

### 3.3 为什么工作区模式下 bash 还要问

没有 OS 沙箱，bash 一条命令会写到哪里只能猜命令字符串。调研时实测过，`bash -c "rm -rf ~"`、`find ~ -delete`
这类写法猜不出来。dsh 的原则是「不提供兑现不了的选项」，Claude Code 的 acceptEdits 也只放行几个能解析的文件命令。
等 Seatbelt 开关做好，工作区模式下 bash 那一格就能改成「在沙箱里直接跑」。

### 3.4 默认模式从哪来

| 入口 | 默认 | 怎么改 |
|---|---|---|
| REPL（`sa`） | `config.toml` 的 `[permissions] mode`（默认工作区） | 启动时 `sa --mode 只读`；会话里 `/mode 全放行` |
| `sa run` | 跟配置走，但配置是全放行时退回工作区 | `--mode full` 显式写才全放行 |
| 定时任务 | 同 `sa run` | `schedules.toml` 里 `mode = "full"` |
| 客户端的空间 | 空间设置里选；没设过的内置空间跟配置默认，外部 CLI 按只读 | 空间设置的「权限」下拉 |

模式名中英文都认：`read-only` / `只读`、`workspace` / `工作区`、`full` / `全放行`。

### 3.5 刻意不做的

- **不把模式写进 system prompt。** dsh 实测过，写了「只读」模型就不敢动手；system prompt 一变前缀缓存也全失效。
  模型只在被拒时从拒绝原因里得知当前模式和出路（「确实要写到外面，请用户切到『全放行』模式」）。
- **越界写仍然拒，不改成「问」。** 无人值守时审批由白名单代答，白名单只看工具名，`--allow write_file`
  就会变成哪里都能写。要写工作目录外就切全放行；逐次升权等沙箱开关一起做。
- **REPL 的模式不写进会话文件。** 模式是「这次打开时给的授权」，`--resume` 回来按启动时的模式。
- **外部 CLI 暂时没有「工作区」档。** claude 的 acceptEdits、opencode 的 external_directory 都还没接，
  接上之前不在下拉里提供。

## 4. 怎么用

```toml
# ~/.simpleagent/config.toml
[permissions]
mode = "workspace"   # 只读 read-only / 工作区 workspace / 全放行 full
```

```bash
sa --mode 只读                       # 这次 REPL 用只读
sa run "整理下载目录" --mode full     # 无人值守要全放行得显式写
```

REPL 里 `/mode` 列出三档和当前这档，`/mode 全放行` 切换，下一次工具调用起生效。启动时会打一行 `权限：工作区（/mode 切换）`。

客户端里在「空间设置」选权限。下拉下面有一句说明，选全放行时变成红色警示；空间头部显示「权限：工作区」，
全放行标红，左栏也有「全放行」标记。改了之后，这个空间里正在跑的会话从下一次工具调用起按新模式判定。

## 5. 改动记录（按实施步骤）

### 第 1 步：核心判定

- `permissions.py`
  - 新增 `Mode`（`READ_ONLY` / `WORKSPACE` / `FULL`，带 `.label`）、`MODE_LABELS`、`parse_mode(text) -> Mode`（中英文都认）。
  - `Scope` 加 `file_edit: bool = False`：这次调用的全部副作用就是改 `paths` 里的文件。工作区模式只自动放行这一类。
    要工具显式声明，不从「有 paths、没 command」推断：推断错了是少问一次，声明漏了只是多问一次。
  - `Policy.__init__` 加 `mode: Mode = Mode.READ_ONLY`，存成可变属性 `self.mode`。代码里的默认值保持只读，
    产品默认（工作区）由各入口从配置读了传进来。
  - `Policy.decide()` 新顺序：禁用 → 危险命令 → 全放行 → 越界 → 默认等级 allow → 工作区 + `file_edit` → 问。
    危险命令从越界后面挪到前面，保证全放行也拦。越界的拒绝理由里写明当前模式和「切到全放行」这条出路。
- `tools/write_file.py`、`tools/edit_file.py`：scope 加 `file_edit=True`。

### 第 2 步：终端入口

- `permissions.py`：新增 `MODE_SUMMARY`，每个模式一句话，REPL 和客户端共用。
- `config.py`：新增 `PermissionsConfig`（`mode: Mode = WORKSPACE`，中文名也认；`unattended(explicit)` 实现
  「显式给了就用，否则跟配置但不继承全放行」），`Config.permissions`。
- `config.example.toml`：加注释掉的 `[permissions]` 段和说明。
- `cli.py`：
  - 新增 `mode_arg()`（argparse 用，认不出来列出可选值）、`MODE_HELP`。
  - 顶层 `--mode` 传给 `Repl`；`sa run --mode` 传给 `Headless`。
  - `list_schedules()` 那行从「工具 只读工具」改成「权限 工作区 · 允许 bash」。
- `ui/repl.py`：
  - `Repl.__init__` 加 `mode`，`Policy(cwd, mode=mode or config.permissions.mode)`。
  - `COMMANDS` 加 `/mode`，`HELP` 提到 `/mode` 的补全；`completer()` 给 `/mode` 补参数。
  - 新增 `_set_mode(text)`：不带参数列三档、标出当前，带参数切换，写错报错且不改。
  - `run()` 启动时打 `权限：…（/mode 切换）`，全放行用红色。
- `ui/headless.py`：`Headless.__init__` 加 `mode`，按 `config.permissions.unattended(mode)` 建 Policy；
  存 `self.allowed_tools`；新增 `permission_line()`，开跑时往 stderr 打 `权限：只读 · 允许：bash`。
- `scheduler/models.py`：`Job` 加 `mode: Mode | None`（中文名也认）。
- `schedules.example.toml`：说明改成先讲模式，再讲 `allowed_tools` 回答「要确认」的那些调用。

### 第 3 步：客户端

- `agents/base.py`：`SAFE` / `FULL` 改成 `Mode.READ_ONLY.value` / `Mode.FULL.value`，外部 CLI 和内置执行者共用一套取值。
- `spaces/models.py`：
  - 新增 `LEGACY_PERMISSIONS = {"safe": "read-only"}`、`normalize_permission()`（兼容旧值 safe 和中文名）。
  - `validate_executor()`：内置执行者不再强制 `safe`；外部 CLI 只收只读 / 全放行，选工作区报错说明原因。
  - `SpaceSpec.permission`、`Space.permission` 改成 `str | None = None`：None 是「没单独设过」。
  - 新增 `Space.effective_mode(default)`：设过的按设的；没设过的内置执行者跟配置默认，外部 CLI 按只读。
- `spaces/store.py`：
  - 去掉 `DEFAULT_PERMISSION`；`_space_to_toml()` 设过才写 `permission`（两种执行者都写）。
  - 新增 `_load_permission()`：旧值 safe 读成 read-only；手改写错了按只读处理，不让空间从列表里消失。
  - `change_executor()`：permission 两种执行者都存；没给就回到「没设过」（外部 CLI 的全放行不会带到内置执行者上）。
- `serve/app.py`：
  - 新增 `_permission_options(executor)`：内置三档（`label` + `description`），外部 CLI 两档。
  - `_meta()` 的 `permissions` / `default_permission` 按执行者给（内置默认取配置）。
  - `_space_view()` 加 `mode`（实际生效的那档），界面显示和标红都看它。
  - `_update_space()`：permission 不再兜底成 safe；改完调 `runner.apply_mode()`。
- `serve/runner.py`：
  - `_build_agent()`：`Policy(cwd, mode=space.effective_mode(...))`。
  - 新增 `apply_mode(space_id, mode)` / `_apply_mode()`：放到事件循环上，把这个空间里正在跑的内置会话的
    `policy.mode` 改掉，从下一次工具调用起生效。外部 CLI 的权限在拉起进程时就定了，改了只影响下一轮。
  - `_run_cli()`：用实际生效的模式拼外部 CLI 的参数和环境变量。
  - 调度者的 prompt 传入配置默认模式。
- `command/prompt.py`：`_ability()` 按空间实际生效的模式描述内置空间能干什么；`spaces_section()`、`command_prompt()`
  加 `default_mode` 参数。
- `spaces/describe.py`：外部 CLI 的权限描述改看 `effective_mode()`。
- `web/index.html`、`web/app.js`、`web/styles.css`：
  - 权限下拉对所有执行者都显示；编辑时选中 `sp.mode`；`executorFields()` 两种执行者都提交 `permission`。
  - 下拉下面一行说明取后端给的 `description`；选全放行时换成红色警示（`.field-hint.warn`），文案按执行者区分。
  - 头部新增 `#ws-mode` 小标签「权限：工作区」，全放行标红；左栏和头部徽标的「全放行」提示不再只限外部 CLI。
  - 新增 `MODE_LABEL`、`fullAccessTitle()`。

### 顺手修的：审批卡看不到理由

调研时发现的缺口⑤：`approval_request_frame()` 没带 `reason`，客户端实时收到的审批卡看不到「为什么要问」，
只有刷新后从 `GET /api/approvals` 补出来的卡才有。现在帧里带上 `reason`（`serve/frames.py`、`serve/approval.py`）。

### 实测时改的

- 权限下拉的选项一开始写成「工作区：工作目录里改文件不用问；……」，在对话框里被截断。改成选项只写模式名，
  说明放到下拉下面。
- 外部 CLI 选工作区时的报错文案去掉了中文引号两边多余的空格。

### 发布前审查时修的

- `serve/runner.py` 的 `_build_agent()`：原来用 `_run_turn` 开头读到的 `space` 对象定模式。`_run_turn` 读完空间后还要等 MCP
  启动，这期间在空间设置里改了模式，`apply_mode` 找不到这个会话（agent 还没登记），新 agent 又按旧对象建，整轮都按旧模式跑。
  现在 `_build_agent()` 从存储里现读空间；补了回归测试 `test_build_agent_reads_the_latest_mode`。
- `web/app.js` 的 `renderHeader()`：打开指挥台的会话时，头部也会显示「权限：工作区」。调度者没有文件工具、计划卡每次都要人确认，
  模式对它不起作用，显示出来是误导。现在指挥台不显示这个标签（前端没有自动化测试，在浏览器里手动确认过）。

## 6. 验证

**自动化**：`ruff format`、`ruff check` 通过；`pytest` 全量 809 个通过（改动前 780 个）。新增和调整的测试：

| 文件 | 覆盖 |
|---|---|
| `test_permissions.py` | 整张模式表（3 种模式 × 8 类调用）；`parse_mode`；工作区只信显式 `file_edit`；越界理由写明模式和出路；内置写工具声明了 `file_edit`；工作区下写工作目录内不问、越界直接拒、bash 照样问；全放行写工作目录外；中途切模式下一次调用生效 |
| `test_config.py` | 默认工作区；中英文取值；写错报错；`unattended()` 五种组合 |
| `test_repl.py` | 默认跟配置、`--mode` 覆盖；`/mode` 列表和切换；写错不改；切到只读后下一次 write_file 要问；启动行；`sa --mode yolo` 报错 |
| `test_complete.py` | `/mode ` 补全三个值 |
| `test_headless.py` | 只读下没 `--allow` 拒写；工作区默认写工作目录不用 `--allow`、bash 仍要；不继承全放行；stderr 的权限行 |
| `test_schedules.py` | `sa schedule list` 显示每个任务的模式；写错的 mode 报出来 |
| `spaces/test_store.py` | `effective_mode` 规则；内置空间的 permission 能存能读；外部 CLI 拒绝工作区；旧值 safe、中文、写错的读法 |
| `serve/test_serve.py` | 实时审批帧带 `reason`；没设权限的空间按工作区跑、写文件不弹审批；`apply_mode` 只改这个空间的会话 |
| `serve/test_web.py` | `/api/meta` 的权限选项和默认值 |
| `test_command_tools.py` | 调度者 prompt 按模式描述内置空间 |

几条原来就有的测试跟着新语义改了：审批流程的测试把空间设成只读（工作区下写文件不再弹审批，就没有审批可测）；
`sa run` 的 stderr 现在多一行权限状态；`sa schedule list` 的显示文案。

**手动**（临时数据目录 + `uv run sa serve --port 8391`，没碰 `~/.simpleagent-dev`）：

- API：没设过权限的内置空间 `permission=None, mode=workspace`；`permission="全放行"` 存成 `full`；
  `space.toml` 里手改成旧值 `safe` 的外部 CLI 空间读出来是 `read-only`；外部 CLI 选工作区返回 400 和原因。
- 浏览器：左栏只有全放行空间带红色「全放行」；老空间头部显示「权限：工作区」；设置里下拉三档、默认选中工作区、
  下面一行说明；改选全放行出现红色警示；保存后头部变成红色「权限：全放行」，左栏出现标记，`space.toml` 写入 `permission = "full"`；
  外部 CLI 空间的下拉只有两档，旧值 safe 显示为只读。
- 终端：`sa --mode 只读` 启动行显示「权限：只读」；`/mode` 列三档；`/mode 全放行` 切换；`/mode yolo` 报错；
  `sa --mode yolo` 被 argparse 拒绝并列出可选值；`sa run --help` 里有 `--mode`。

没有用真实模型跑端到端（临时配置里的 profile 指向不存在的地址）。「工作区下写文件不弹审批」「切模式后下一次调用生效」
这两条由 FakeLLM 驱动的 Runner / REPL 测试覆盖。

## 7. 已知限制和后续可以做的

- **工作区模式下 bash 还要问。** 等 Seatbelt 沙箱开关：打开后工作区模式下 bash 在沙箱里直接跑，被拦时带理由申请一次。
- **「a / 本次会话始终允许」仍按工具名放行**（调研里的缺口③）。在工作区模式下它只影响 bash 和 MCP 写操作，但对 bash 按一次
  还是等于后面都不问。可以学 Claude Code 按命令前缀记，或者对 bash 不给这个选项。
- **客户端审批不会超时**（缺口⑥）：文档说「不在线 / 超时按拒绝」，代码是一直等。这次没动。
- **外部 CLI 没有工作区档**：claude 可以用 `--permission-mode acceptEdits`，opencode 要配 `edit: allow` 加 `external_directory`。
- **切模式不会放行已经挂着的审批卡**：比如从只读切到全放行，之前弹出的卡还要手动点。
- **旧值 `safe` 是懒迁移**：读的时候当只读，保存一次空间设置才写成 `read-only`。
- **新建空间会把当时的默认模式存下来**：向导把预选的默认值一起提交，之后改 `config.toml` 的默认模式，
  只影响没单独设过的空间（老空间、通过 API 建且没带 permission 的空间）。
- **`sa --mode` 对 `sa serve` 不起作用**：客户端的模式按空间设，默认看配置。
- **按工具、按命令写的规则还没进配置**（ROADMAP 里 M3 剩下的另一半），比如「这个项目 `uv run pytest` 不用问」。
