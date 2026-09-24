# 斜杠命令补全：CLI 的 Tab 补全 + 网页的 / 菜单

- 日期：2026-09-24
- 对比基线：`3058400`（变更记录：README 安装前提醒 sa 撞名）
- 对应里程碑：无（REPL 和工作台的可用性改进；方案见 `docs/design/slash-completion.md`）

## 功能变化

- 新增：CLI 的 `sa` REPL 里，`/` 开头的输入支持 Tab 补全——补内置命令名、技能名，`/model`、`/debug`
  还能接着补第一个参数（profile 名 / debug 档位）。用标准库 `readline` 的补全回调实现，兼容 macOS 上
  常见的 libedit 后端，没有新增依赖。
- 新增：网页工作台的输入框敲 `/` 弹出补全菜单（外观、交互照搬指挥台已有的 `@` 提及菜单）：命令在前、
  技能在后（带「技能」标签），前缀匹配的排在包含匹配的前面；↑↓ 选择、Tab 补全、Enter 选中（不需要
  再补参数的直接发送）、Esc 关闭、点击同 Enter。选了 `/model` 后接着弹 profile 列表，标出当前用的。
- 修复：网页端 `runCommand()` 此前把所有 `/` 开头的输入都拦下提示「未知命令」，导致 M7 加的
  `/技能名` 展开在网页上一直调不到（`runner.py` 的展开逻辑本身没问题，只是网页请求根本发不到后端）。
  现在 `/技能名` 会原样发给后端，由 runner 展开成技能全文；输入框里显示的还是用户原话。
- 修复：网页输入框在输入法选词时按 Enter 会被当成发送/选中补全项，现在 `isComposing` 时忽略该次
  Enter，只用来确认候选字。
- 改进：CLI 里只输入一个 `/` 就回车时，从提示「未知命令 /」改为显示帮助（和网页端一致）。
- 文档：`README.md` 补充 Tab 补全说明；`docs/ARCHITECTURE.md` 代码结构表加 `ui/complete.py`，
  注明 `ui/repl.py` 的命令表 `COMMANDS`；`docs/design/client-ui.md` 的 API 表加
  `GET /api/spaces/{id}/commands`；新增设计文档 `docs/design/slash-completion.md`（含取舍、坑点、
  已知限制）。

## 函数级改动

### `src/simpleagent/ui/complete.py`（新文件）

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `SlashCompleter.__init__(names, args=None)` | 新增 | 候选在会话开始时定下来（技能清单、profile 在会话里不变），命令名/技能名去重排序 |
| `SlashCompleter.candidates(line) -> list[str]` | 新增 | 纯逻辑：行首不是 `/` 不补；还在输命令名时补 `/名字`；命令后只补第一个参数（仅限有登记参数候选的命令）；第二个参数不补 |
| `SlashCompleter.complete(text, state) -> str \| None` | 新增 | readline 补全回调协议；不用 readline 传入的 `text`（只是光标所在的词），改用 `get_line_buffer()[:get_endidx()]` 取整行判断；`state=0` 时算好候选，唯一候选自动补一个空格 |
| `install_completer(completer) -> bool` | 新增 | 把补全函数接到 `readline` 上；按是否是 libedit 选择 `bind ^I rl_complete` 还是 `tab: complete` 绑定语法；把补全分隔符改成只按空白分词（默认分隔符含 `/`，否则 `/co` 只会拿到 `co`）；没有 `readline` 模块（如 Windows）时返回 `False` 且不做任何事 |

### `src/simpleagent/ui/repl.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `COMMANDS: dict[str, tuple[str, str]]` | 新增 | 内置命令名 → `(参数, 说明)`，`HELP` 和 CLI 补全的候选都从这张表生成，以后加命令只改一处 |
| `_pad(text, width) -> str` | 新增 | 按终端显示宽度补空格（中文字符按两格算），让 `HELP` 里参数长度不一的命令说明对齐 |
| `HELP` | 修改 | 从硬编码字符串改为由 `COMMANDS` 拼接生成，多了一行「按 Tab 补全…」的提示 |
| `Repl.completer() -> SlashCompleter` | 新增 | 组装 CLI 用的补全器：候选 = 内置命令 + 当前技能清单；`model` 补 `config.profiles`，`debug` 补 `DEBUG_LEVELS` |
| `Repl.run()` | 修改 | 仅当 `input_fn is input` 且 `sys.stdin.isatty()` 时才调用 `install_completer()`（测试注入的 `input_fn` 或非终端输入不会碰进程级 `readline`）；启动提示语改为「输入 /help 查看命令，Tab 补全」 |
| `Repl.command()` | 修改 | `case "help"` 改为 `case "help" | ""`，只输入 `/` 回车时也显示帮助（原来会提示「未知命令 /」） |

### `src/simpleagent/serve/app.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| 路由分发（`re.match` 列表） | 新增 | `GET /api/spaces/{id}/commands` |
| `Server._space_commands(space_id) -> Response` | 新增 | 返回该空间能用 `/技能名` 调的技能列表 `{"skills": [{"name", "description"}]}`；说明里的换行压成空格；空间不存在返回 404；外部 agent 空间、指挥台（`COMMAND_SPACE_ID`）、`config.skills.enabled = false` 时返回空列表；每次现扫 `skill_roots(...)` 下的技能目录（不读会话里冻住的那份） |

### `src/simpleagent/web/slash.js`（新文件）

| 函数 | 变化 | 说明 |
|---|---|---|
| `rank(entries, q)` | 新增 | 名字前缀匹配的排在前面，其次是包含匹配的，不分大小写，同档保持原顺序 |
| `slashMenu(before, {commands, skills, profiles, current})` | 新增 | 纯函数：光标前的文字 → 菜单项列表或 `null`。识别两种场景：`/关键字`（列命令 + 技能，同名技能因发送时内置命令优先而被过滤掉不列）和 `/model 关键字`（列 profile，标出当前）；每项含 `label / hint / desc / tag`（展示用）、`value`（选中后替换光标前文字）、`submit`（Enter 选中后是否直接发送） |

### `src/simpleagent/web/app.js`

| 函数 / 变量 | 变化 | 说明 |
|---|---|---|
| `state.skills` | 新增 | 空间 id → 技能列表的缓存，供 `/` 菜单和 `/help` 用 |
| `WEB_COMMANDS` | 新增 | 前端处理的命令表（`help` / `verify` / `model` 的名字、参数、说明），`/help` 从它生成 |
| `skillsFor(spaceId, fresh=false)` | 新增 | 按空间缓存技能列表；`fresh=true` 时强制重拉一次（技能目录可能改过）；拉取失败时沿用上次缓存 |
| `runCommand(text) -> Promise<boolean>` | 修改 | 从「处理完直接 return」改为返回是否已在前端处理掉；`/help` 补充列出技能；是当前空间的技能名时返回 `false`，交给调用方原样发送 |
| `updateSlash()` | 新增 | 输入事件里调用：刚敲出 `/` 时顺手用 `fresh=true` 重拉一次技能，再按当时输入重画菜单 |
| `refreshSlash()` | 新增 | 调 `slashMenu()` 算候选，更新 `slash` 状态并重画 |
| `closeSlash()` | 新增 | 关闭菜单并重画（清空） |
| `drawSlash()` | 新增 | 把候选渲染成菜单 DOM，外观复用 `.mentions`；菜单项绑定 `mousedown` + `preventDefault`（避免输入框先失焦把菜单关掉，导致 click 落空） |
| `applySlash(i, submit)` | 新增 | 选中一项：用该项 `value` 替换光标前文字；`submit && it.submit` 时直接调用 `send()`，否则重新弹菜单（用于选完 `/model` 接着列 profile） |
| `onInputKeydown(e)` | 新增 | 输入框按键统一入口：`isComposing` 时忽略；菜单开着时 `↑↓` 移动选中、`Tab`/`Enter` 应用选中项、`Esc` 关闭；菜单没开时 `Enter`（非 Shift）照常调用 `send()`（原来直接挂在 `keydown` 上的逻辑搬到这里） |
| `send()` | 修改 | 发送前先 `closeSlash()`；`/` 开头的输入改为 `if (text.startsWith("/") && await runCommand(text)) return;`，前端未处理（如 `/技能名`）时继续走普通消息发送路径 |
| `boot()` 里的事件绑定 | 修改 | 输入框 `keydown` 换成 `onInputKeydown`；`input` 事件里追加调用 `updateSlash()`；新增 `blur` 事件调用 `closeSlash()` |

### `src/simpleagent/web/index.html`

- `app.js` 之前新增 `<script src="/assets/slash.js"></script>`。

### `src/simpleagent/web/styles.css`

- `.composer` 加 `position: relative`（补全菜单贴着它的上沿定位弹出）；新增 `.slash-menu / .slash-item /
  .slash-label / .slash-hint / .slash-desc` 样式，外观和指挥台已有的 `.mentions` 提及菜单一致。

## 配置与依赖

- 无新增依赖（CLI 沿用标准库 `readline`，网页沿用现有的原生 JS，没有引入 prompt_toolkit）。
- 无配置项变化。
- 用户无需手动处理：不用改 `~/.simpleagent/config.toml`，不用重新 `uv sync`。

## 测试

- `tests/test_complete.py`（新，8 个）：命令名补全和去重；`/model ` 补第一个参数、第二个参数不补；
  普通文本和行首带空格的不触发补全；readline 回调协议（只看光标之前的内容、唯一候选自动补空格）；
  `HELP` 覆盖了 `COMMANDS` 表里的全部命令且中文参数对齐；表里每个命令在 `Repl.command()` 里都有实现、
  不会落到「未知命令」分支；只输入 `/` 回车会显示帮助；`Repl.completer()` 带上了当前 cwd 下的技能和
  profile。
- `tests/serve/test_slash.py`（新，5 个；本次环境有 node，未跳过）：`/` 先列命令再列技能、同名技能不列；
  前缀匹配排前、包含匹配排后且不分大小写；`/model ` 列出 profile 并标出当前；不在行首/已经在写参数/多行
  时不弹菜单；没有技能和 profile 时菜单仍能正常工作。
- `tests/serve/test_web.py`（+2 个）：`GET /api/spaces/{id}/commands` 能列出空间 cwd 里的技能、说明里的
  换行压成一行；普通空间没有技能时返回空列表；空间不存在返回 404；外部 agent 空间和指挥台返回空列表。
- 测试结果：`uv run pytest -q` 全部 **733 passed**；`uv run ruff check` 与 `uv run ruff format --check`
  均干净。
- 手动验证（用临时 `SIMPLEAGENT_HOME`，未触碰 `~/.simpleagent-dev`）：伪终端驱动真实 `uv run sa`
  （libedit 后端）验证了 `/re` + Tab 补成技能 `/release `、`/co` 按两下 Tab 列出多个候选、
  `/model ` + Tab 补 profile 名；在内置浏览器里对 `sa serve` 试了网页菜单的键盘/鼠标交互、
  `/model` 切换、`/help` 里的技能列表、Esc 关闭菜单，以及 `/release v1.0` 发送后确认后端模型历史里是
  展开后的技能全文（界面上仍显示用户原话）。

## 相关笔记

- 无（学习笔记按里程碑统一在 `docs/notes/` 补，这次不是独立里程碑，暂不写）。
