# 斜杠命令补全：CLI 的 Tab 补全 + 网页的 / 菜单

## 1. 要解决什么

- **CLI**（`sa` REPL）：命令有十几个，外加每个技能都能用 `/技能名` 调，全靠记。`readline` 已经 import 了，
  但只拿来做方向键和输入历史，没有注册补全函数。
- **网页**（`sa serve` 工作台）：输入框只认 `/help`、`/verify`、`/model` 三个，没有任何提示。
- **顺带发现的 bug**：M7 在 `runner.py` 的 `_run_input` 里加了 `/技能名` 展开，但网页的 `runCommand()`
  写得更早，所有 `/` 开头的输入都被它拦下提示「未知命令」，**网页端一直调不了技能**。补全菜单要列技能，
  这个必须一起修。

## 2. 取舍

**CLI 用标准库 readline 的补全回调（方案 A），不引入 prompt_toolkit（方案 B）。**

| | A：readline completer | B：prompt_toolkit |
|---|---|---|
| 体验 | 按 Tab 补全，按两下 Tab 列候选；只显示名字 | 边输入边弹下拉菜单，带说明 |
| 依赖 | 无 | 新增 prompt_toolkit + wcwidth |
| 输入层 | 不动，还是 `input()` | 换成 `PromptSession.prompt()`；审批提示是在事件循环运行中调用输入函数的，要特殊处理；`"""` 多行输入和测试注入的 `input_fn` 都得改 |

用户确认用 A。

几个实现上的坑：

- 本机 uv 装的 Python，`readline` 是 **libedit** 包装的（`readline.__doc__` 里有 libedit），绑定 Tab 要写
  `bind ^I rl_complete`，GNU readline 写 `tab: complete`。libedit 列候选时也显示不了说明。
- readline 默认的分隔符里有 `/`，不去掉的话输入 `/co` 时交给补全函数的词只有 `co`。改成只按空白分词。
- CPython 的 readline 模块把「补全后自动追加的字符」设成了空（两种后端都是），唯一候选时不会自动补空格。
  我们自己在唯一候选后面加空格，补完 `/model ` 就能直接输入参数、再按 Tab 补 profile。
- 补全函数不用 readline 传进来的 `text`（它只是光标所在的那个词，看不出前面是哪个命令），而是取
  `get_line_buffer()[:get_endidx()]` 整行来判断。

**网页的菜单只算候选的部分拆成纯函数（`slash.js`）**，照 `markdown.js` 的写法，node 里能 require 来测；
画菜单和处理按键留在 `app.js`。外观、交互照搬指挥台已有的 `@` 补全（`.mentions`）。

**技能清单按空间取（`GET /api/spaces/{id}/commands`）**：技能在各空间 cwd 下的 `.agents/skills/` 等目录，
每个空间不一样。只扫技能目录（`discover_skills(skill_roots(...))`），不加载整个 Knowledge。外部 agent 空间和
指挥台返回空列表，因为 runner 只在内置 loop 的普通空间展开 `/技能名`。

**和内置命令同名的技能不列**：两端发送时都是内置命令先处理，同名技能根本调不到。

## 3. 怎么用

**CLI**：
- `/` + Tab Tab：列出全部命令和技能
- `/co` + Tab Tab：列出 `/code-review /compact /context`；`/rel` + Tab：补成 `/release `
- `/model ` + Tab：补 profile 名；`/debug v` + Tab：补成 `verbose`
- 只输入 `/` 回车：显示帮助（以前提示「未知命令 /」）

**网页**：
- 输入框敲 `/` 弹出菜单：命令在前、技能在后（带「技能」标签），名字前缀匹配的排在包含匹配的前面
- ↑↓ 选择；Tab 补全；Enter 补全，不带参数的（`/help`、`/verify`、某个 profile）直接执行；Esc 关闭；点击同 Enter
- 选了 `/model` 之后接着弹 profile 列表，当前的标「当前」
- `/技能名 补充说明` 原样发给后端，由 runner 展开成技能全文；界面上显示的还是原话
- `/help` 卡片里也列出技能

## 4. 改动记录（按实施步骤）

### 第 1 步：CLI 的 Tab 补全

- `ui/complete.py`（新）
  - `SlashCompleter(names, args)`：候选在会话开始时定下来（技能清单、profile 在会话里本来就不变），名字去重排序
  - `SlashCompleter.candidates(line) -> list[str]`：纯逻辑。行首不是 `/` 不补；还在输入命令名时补 `/名字`；
    命令后面补第一个参数（只给了候选的命令）；第二个参数不补
  - `SlashCompleter.complete(text, state)`：readline 回调协议。`state=0` 时取整行算好候选，唯一候选补空格
  - `install_completer(completer) -> bool`：接到 readline 上，按 libedit / GNU 选绑定语法；没有 readline 返回 False
- `ui/repl.py`
  - 新增 `COMMANDS: dict[str, tuple[str, str]]`（名字 → 参数、说明），`HELP` 改为从表生成，多了一行 Tab 的说明
  - 新增 `_pad(text, width)`：按终端显示宽度补空格（中文占两格），`/compact [重点]` 的说明和其他行对齐了
  - 新增 `Repl.completer() -> SlashCompleter`：内置命令 + 技能名；`model` 补 `config.profiles`，`debug` 补 `DEBUG_LEVELS`
  - `Repl.run()`：`input_fn is input` 且 stdin 是终端时才 `install_completer`（测试注入 input_fn 时不碰进程级的 readline）；
    启动提示改成「输入 /help 查看命令，Tab 补全」
  - `Repl.command()`：`case "help" | ""`，只输入 `/` 也显示帮助

### 第 2 步：网页的 / 菜单

- `serve/app.py`
  - 路由新增 `GET /api/spaces/{id}/commands`
  - 新增 `Server._space_commands(space_id)`：返回 `{"skills": [{"name", "description"}]}`，说明里的换行压成空格；
    空间不存在 404；外部 agent、指挥台、`[skills] enabled = false` 返回空列表
- `web/slash.js`（新）
  - `slashMenu(before, {commands, skills, profiles, current})`：光标前的文字 → `{items}` 或 `null`。
    每项有 `label / hint / desc / tag`（显示用）、`value`（选中后替换光标前的全部文字）、`submit`（Enter 选中后是否直接发送）
  - `rank(entries, q)`：前缀匹配在前、包含匹配在后，不分大小写，同档保持原顺序
- `web/app.js`
  - 新增 `WEB_COMMANDS`（help / verify / model 的名字、参数、说明），`/help` 从它生成，并列出技能
  - 新增 `skillsFor(spaceId, fresh)`：按空间缓存技能；输入框里刚敲出 `/` 时带 `fresh` 重拉一次（技能目录可能改过）
  - `runCommand(text)` 改为返回「是否已在前端处理」：是当前空间的技能名就返回 false
  - `send()`：`if (text.startsWith("/") && await runCommand(text)) return;`，技能照普通消息发出去
  - 新增菜单相关的 `updateSlash / refreshSlash / closeSlash / drawSlash / applySlash / onInputKeydown`；
    菜单项用 mousedown + preventDefault，避免输入框先失焦把菜单关掉
  - 输入框 keydown 换成 `onInputKeydown`，input 事件多调一次 `updateSlash`，blur 时关菜单
- `web/index.html`：在 `app.js` 之前加载 `slash.js`
- `web/styles.css`：`.composer` 加 `position: relative`；新增 `.slash-menu / .slash-item / .slash-label / .slash-hint / .slash-desc`

### 顺手修的

- **网页调不了技能**（见第 1 节），`runCommand` 的返回值改完就好了。
- 输入框 keydown 开头加了 `if (e.isComposing) return;`：输入法选词时按 Enter 是确认候选字，不该发送，也不该选菜单项。

### 文档

- `README.md`：REPL 命令一段加了 Tab 补全的说明
- `docs/ARCHITECTURE.md`：代码结构里加 `ui/complete.py`，`ui/repl.py` 注明命令表 `COMMANDS`
- `docs/design/client-ui.md`：API 表加 `GET /api/spaces/{id}/commands`

## 5. 验证

自动测试（`uv run pytest`，全部 **733 passed**，其中新增 15 个；`ruff format`、`ruff check` 干净）：

- `tests/test_complete.py`（新，8 个）：命令名补全和去重；第一个参数补全、第二个不补；普通文本和行首有空格的不补；
  readline 回调协议（只看光标之前、唯一候选补空格）；`HELP` 列全了表里的命令且中文参数对齐；表里每个命令都有实现、
  不会落到「未知命令」；只输入 `/` 显示帮助；`Repl.completer()` 带上了 cwd 里的技能和 profile
- `tests/serve/test_slash.py`（新，5 个，没有 node 自动跳过）：`/` 列出命令再列技能、同名技能不列；前缀在前、包含在后、
  不分大小写；`/model ` 列 profile 并标出当前；不在行首、已经在写参数、多行时不弹；没有技能和 profile 时也能用
- `tests/serve/test_web.py`（+2 个）：接口列出空间 cwd 里的技能、说明压成一行、普通空间返回空、不存在的空间 404；
  外部 agent 空间和指挥台返回空列表

手动（用 scratchpad 里的临时 `SIMPLEAGENT_HOME`，没碰 `~/.simpleagent-dev`）：

- **CLI**：用伪终端驱动真实的 `uv run sa`（libedit 后端）。`/re` + Tab 补成项目技能 `/release `；`/co` 按两下 Tab
  列出 `/code-review /compact /context`；`/mo` + Tab + `o` + Tab 得到 `/model other `，回车后提示已切换
- **网页**：`sa serve` 起在临时端口，在内置浏览器里建了一个 cwd 带两个技能的空间：
  - 敲 `/` 弹出 3 个命令 + 2 个技能，长说明截断显示
  - `/re` 过滤后是 `/release`、`/code-review`；↓ + Tab 补成 `/code-review `
  - `/mo` + Enter 补成 `/model ` 并列出 `fake 当前`、`other`；↓ + Enter 直接切换，提示「已切换到 other」
  - `/he` + Enter 直接执行，帮助卡片里列出了技能；Esc 能关菜单
  - `/release v1.0` + Enter：消息发到了后端，模型历史里是展开后的技能全文（`$ARGUMENTS` 已替换为 v1.0）；
    界面上显示原话。控制台没有报错

## 6. 已知限制和后续可以做的

- CLI 列候选时没有说明（libedit 的限制），说明看 `/help`、`/skills`。以后想要 Claude Code 那种弹出菜单，再评估 prompt_toolkit。
- 网页菜单用的技能清单是现扫的，而会话展开 `/技能名` 用的是会话开始时冻住的那份。会话中途新增的技能能在菜单里看到，
  但在这个会话里不会展开（原话发给模型）；新会话就一致了。
- 网页端只有 `/help`、`/verify`、`/model` 三个前端命令，CLI 的 `/compact`、`/context`、`/clear` 等还没有网页版，
  要做的话需要后端接口。
- 中文输入法的中文标点模式下按 `/` 打出来的是「、」，两端都不会触发补全，要先切到英文标点。
