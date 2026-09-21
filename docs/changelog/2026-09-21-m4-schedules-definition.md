# M4 第 1 步：定时任务定义与时间表（schedules.toml + `sa schedule list`）

- 日期：2026-09-21
- 对比基线：`d1fddca`（README 安装地址改到新仓库）
- 对应里程碑：M4（8 步中的第 1 步：任务定义与时间表；还没有 daemon，任务不会被执行）

## 功能变化

- 新增：`schedules.toml` 任务定义格式——每个任务是一个 `[jobs.<名字>]` 表，字段包括 `cron`、`prompt`、`title`、`enabled`、`profile`、`cwd`、`allowed_tools`、`notify`、`notify_on`、`timeout`、`max_steps`。文件不存在时视为「没有任务」，不算错误。
- 新增：`sa schedule list` 子命令，列出所有任务的下次触发时间；加载失败的任务单独列出原因，此时退出码为 1，方便改完顺手检查。
- 升级：`sa init` 从「只生成 `config.toml`」改成「缺哪个配置文件就生成哪个」（`config.toml` + `schedules.toml`），两个都存在才报错、不覆盖；已有 `config.toml` 的老用户再跑一次就能补上 `schedules.toml` 模板。
- 设计：单个任务写错（cron 非法、字段拼错、prompt 为空等）只影响它自己，其余任务照常加载；只有 TOML 语法错误或顶层出现非 `jobs` 的未知键（比如 `[job.x]` 少写一个 s）才判定整个文件不可用。
- 设计：给了 `profiles` 时会交叉校验任务引用的 `profile` 是否存在于 `config.toml`，避免等到半夜任务触发才发现跑不起来。
- 设计：cron 只接受 5 段（分 时 日 月 周）或 `@hourly/@daily/@weekly/@monthly` 这类别名，拒绝 6 段写法（各家对第 6 段的理解不统一）。
- 新增：`docs/ROADMAP.md` 补充 M4 的 8 步拆分说明和当前进度（① 已完成，② ~ ⑧ 待做），并注明心跳不单独做机制、白名单模式匹配暂不做。

## 函数级改动

### `src/simpleagent/scheduler/models.py`（新文件）

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Job` | 新增 | pydantic 模型，对应 `[jobs.<name>]`；`extra="forbid"`，字段拼错直接报错而不是被静默忽略 |
| `Job._check_name()` | 新增 | 校验任务名只能用字母、数字、`_`、`-`，最长 64 字符（要当日志目录名和命令行参数用） |
| `Job._check_cron()` | 新增 | 规整多余空白，拒绝非 5 段/非法 cron 表达式，用 `croniter.is_valid` 校验 |
| `Job._check_prompt()` | 新增 | 去首尾空白，空 prompt 报错 |
| `Job.label` | 新增 | 属性，`title` 优先，没写就用 `name` |
| `Job.next_fire(after)` | 新增 | 用 `croniter` 算严格晚于 `after` 的下一次触发时间，时区跟随传入的 `after` |
| `Job.workdir(home)` | 新增 | 没写 `cwd` 时返回 `<home>/jobs/<name>/` 作为默认工作目录（相当于自带沙箱） |

### `src/simpleagent/scheduler/store.py`（新文件）

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `ScheduleError` | 新增 | 文件级错误（TOML 语法错、顶层结构不对），整个 `schedules.toml` 不可用时抛出 |
| `Schedules` | 新增 | `dataclass`，装 `jobs`（加载成功的任务）和 `errors`（任务名 → 失败原因） |
| `schedules_path()` | 新增 | 返回 `<home_dir>/schedules.toml` |
| `_brief(error)` | 新增 | 把 pydantic `ValidationError` 压成一行文本，和 `load_config` 同一个口径 |
| `load_schedules(path=None, *, profiles=None)` | 新增 | 读并校验 `schedules.toml`；按任务隔离错误；可选按 `profiles` 交叉校验 `profile` 是否存在 |
| `example_schedules()` | 新增 | 读打包进 wheel 的 `schedules.example.toml` 模板内容 |
| `init_schedules(path=None)` | 新增 | 生成带注释的 `schedules.toml` 模板；已存在时抛 `ScheduleError`，不覆盖 |

### `src/simpleagent/scheduler/__init__.py`（新文件）

- 新增：导出 `Job`、`ScheduleError`、`Schedules`、`init_schedules`、`load_schedules`、`schedules_path`。`example_schedules` 未导出（只在 `store.py` 内部给 `init_schedules` 用）。

### `src/simpleagent/cli.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `init_files()` | 新增 | `sa init` 的新实现：`config.toml`、`schedules.toml` 缺哪个生成哪个，两个都在时报错列出两个路径 |
| `format_fire_time(dt)` | 新增 | 把触发时间格式化成 `09-28 周一 09:00` 这种形式，供 `list_schedules` 用 |
| `list_schedules(now=None)` | 新增 | `sa schedule list` 的实现：文件不存在提示先 `sa init`；打印每个任务的 cron、下次触发时间、允许的工具、profile；末尾打印加载失败的任务和原因；有失败时返回 1 |
| `main()` | 修改 | 新增 `schedule` 子命令（`schedule list`）；`init` 命令改为调用 `init_files()`；顶层新增对 `ScheduleError` 的捕获，打印「定时任务配置错误：...」并返回 1 |

## 配置与依赖

- 新增运行时依赖 `croniter>=6.2`（`pyproject.toml`），带进传递依赖 `python-dateutil` 和 `six`（`uv.lock` 已更新）。
- 新增数据文件模板 `src/simpleagent/schedules.example.toml`，`sa init` 会把它拷到 `~/.simpleagent/schedules.toml`（或 `SIMPLEAGENT_HOME` 指向的目录）。
- **需要手动处理**：
  - 已经跑过 `sa init` 的老用户，`config.toml` 已存在，这次不会自动补 `schedules.toml`；需要再手动跑一次 `sa init` 才能拿到模板（`config.toml` 已存在会跳过、不覆盖）。
  - 拉取这份代码后要 `uv sync`，装上新增的 `croniter` 依赖，否则 `import simpleagent.scheduler` 会失败。
  - 现在还没有 `sa daemon`，`schedules.toml` 里定义的任务目前只能通过 `sa schedule list` 查看，不会被实际执行。

## 测试

- 新增 `tests/test_schedules.py`：30 个测试（含参数化展开），覆盖字段默认值/全字段解析、`cron`/`prompt` 校验、TOML 语法错误、顶层未知键、`jobs` 非表结构、单任务出错不连累其余任务、`profiles` 交叉校验、模板文件可加载且示例任务去注释后能直接用、cron 合法/非法用例（含跨周末的 `next_fire`）、`workdir` 默认值与自定义 `cwd`、以及 `sa schedule list` / `sa init` 的 CLI 行为（含空文件提示、语法错误提示、已存在文件不覆盖）。
- 测试结果：`uv run pytest -q` 389 passed。
- `uv run ruff check`：All checks passed。
- `uv run ruff format --check`：118 files already formatted（无需改动）。

## 相关笔记

- 无（M4 尚在进行中，按约定笔记在里程碑结束后统一写）
