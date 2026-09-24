# SimpleAgent

自用的本地 agent，两个目的：一是**在本地解决真实问题**，二是**搞清楚 agent 是怎么实现的**——
agent loop、工具调用、权限、上下文都从零手写，只依赖 OpenAI 兼容协议，不套任何 agent 框架。

长期方向是个人 AI 工作台：我平时用 AI 有两条路，一是按目录启动 Claude Code 或 OpenCode
（投资、健康、旅游各一个目录，尽量不让项目绑死在某一家 agent 上），二是随手在网页上聊。
所以这个项目要做的，是把那些散在各目录里的 AI 任务管起来：派任务、看状态、收例行任务的结果。

当前版本 **0.2**，能力见下面的「能做什么」，路线图见 [ROADMAP](docs/ROADMAP.md)。

## 能做什么

- **终端里聊**：`sa` 进 REPL，流式输出，可切模型、看用量、存会话、随时 Ctrl+C 打断。
- **让模型动手**：内置 7 个工具（`list_dir` `read_file` `write_file` `edit_file` `glob` `grep` `bash`），
  读类工具并行跑、写类工具串行跑，工具报错原样回给模型让它自己纠正。
- **有边界的权限**：读操作直接放行，写文件和跑命令要你按键确认；越出工作目录和明显危险的命令直接拒绝。
- **无人值守**：`sa run "..."` 跑完就退出，配合 cron / launchd 用；没人确认时写操作一律拒绝，除非 `--allow` 明确放行。
- **浏览器工作台**：`sa serve` 起本地服务，用浏览器管理「空间」（一类任务一个目录），
  看流式对话、工具卡、审批卡，跑验证命令，控制面板汇总跨空间的任务、消息和待办。
- **调度外部 agent**：空间的执行者可以选 Claude Code 或 OpenCode，它们以无头模式跑，
  输出被翻译成同一套事件，界面上和内置 loop 长得一样。
- **给 sa 发消息**：脚本和定时任务可以用 `sa inbox push` 把结论投进控制面板。
- **接 MCP server**：在配置里写上 `[mcp_servers.<名字>]`，它的工具就以 `mcp__<名字>__<工具>`
  出现在模型面前，终端、`sa run`、工作台都能用；新旧两代 MCP 协议都支持。
- **记得住、带得上**：自动读工作目录的 `AGENTS.md`（没有就读 `CLAUDE.md`）；让它「记住」的事存成
  Markdown 文件，下个会话还知道；按 `SKILL.md` 标准写的技能，模型按需加载，也能 `/技能名` 直接调用。

## 安装

> **命令名冲突**：macOS 自带一个同名的 `/usr/sbin/sa`（系统记账统计）。装完敲 `sa --version` 如果看到
> `illegal option` 和一行 `usage: sa [-abcd...]`，或者要在 cron、launchd 这类 PATH 很短的环境里用，
> 就改用同时装好的长名字 `simple_agent`（和 `sa` 完全等价），或者写绝对路径 `~/.local/bin/sa`。

用 [uv](https://docs.astral.sh/uv/) 从 GitHub 装成全局命令，之后在任意目录都能直接敲 `sa`（本机没有 Python 3.12 时 uv 会自动下载）：

```bash
uv tool install "git+https://github.com/tjiisztiuv/SimpleAgent.git@main"
sa --version                     # 确认装好了
```

- 装的是 `main` 分支的一份快照，本地改代码不影响它；要更新就加 `--reinstall` 再执行一次
- 想固定在某个版本就把 `@main` 换成 tag，比如 `@v0.2.0`
- 不建议在本机开发目录里 `uv tool install --editable .`：那样日常用的 `sa` 就是正在改的代码，
  见下面「开发」一节的隔离方式
- 卸载：`uv tool uninstall simpleagent`（`~/.simpleagent/` 里的配置和会话不会删）
- 命令装在 `~/.local/bin/`；不在 PATH 里的话执行一次 `uv tool update-shell`

不想装也可以：在仓库目录下 `uv sync` 后用 `uv run sa` 代替下面所有的 `sa`（这时是开发模式，数据目录是 `~/.simpleagent-dev/`）。

## 第一次使用

```bash
sa init                                              # 生成 ~/.simpleagent/config.toml 和 schedules.toml
echo 'DEEPSEEK_API_KEY=sk-...' >> ~/.simpleagent/.env # 或者直接 export，二选一
chmod 600 ~/.simpleagent/.env
sa                                                   # 进入对话，Ctrl+D 或 /exit 退出
```

默认用 `deepseek` 这个 profile。配置文件里还预置了 GLM、通义千问、本地 Ollama 等，
换模型改 `default_profile`，或者启动时 `sa -m glm`。key 只写环境变量名，不会存进配置文件。

## 三种用法

### 1. 终端对话

```bash
sa                       # 新开一轮
sa -m local              # 指定 profile，比如本地 Ollama
sa --resume              # 接着最近一次继续聊
sa --resume se_x1y2      # 接着指定会话
sa sessions              # 列出已保存的会话
```

REPL 里的命令：`/model [name]` 切模型、`/tools` 列工具、`/mcp` 看 MCP server 状态、`/usage` 看用量、
`/context` 看上下文占用和构成、`/compact [重点]` 把早期对话压成摘要、`/memory` 看长期记忆、
`/skills` 看技能、`/<技能名> [补充说明]` 调用技能、`/prompt` 看完整的 system prompt、`/clear` 清空历史、`/help`、`/exit`。
输入 `/` 开头时按 Tab 补全命令名、技能名和 `/model`、`/debug` 的参数（按两下 Tab 列出候选）。
要输入多行，单独一行敲 `"""` 开始，再敲一次 `"""` 结束；Ctrl+C 中断当前回复，Ctrl+D 退出。

模型自己决定调哪个工具，终端里用灰色显示调用和结果预览。一轮对话最多请求模型 `max_steps` 次（默认 20），
防止它绕圈子。过长的工具输出只把开头回给模型，完整内容存在 `~/.simpleagent/tool_outputs/`（保留 7 天）。

### 2. 浏览器工作台

```bash
sa serve                 # 默认 127.0.0.1:8384，只监听本机
sa serve --port 9000     # 端口被占用时换一个
```

然后浏览器打开 <http://127.0.0.1:8384/>。界面分两栏，左边是空间列表和控制面板，右边是当前会话。

- **空间**：一类任务的容器。「通用」空间自动分配一个 tmp 目录，随手算点东西用；
  「绑定目录」空间指向真实项目目录，进那个目录干活。每个空间只显示最近 5 个会话，其余收进「查看全部」。
- **执行者**：新建空间时选 `simpleagent`（内置 loop）、`claude-code` 或 `opencode`。
  选外部 CLI 时要先在本机装好并登录，权限档有「只读」（默认）和「全放行」两档——
  它们的无头模式没法把审批实时问回来，所以只能事先定档，全放行的空间界面上会标红。
- **右栏四个 tab**：对话、变更（这轮改了哪些文件）、文件（工作目录树）、日志（trace 和错误）。
  头部可以停止、重跑、导出 Markdown、手动跑验证。
- **验证状态**：空间可以配一条验证命令（比如 `uv run pytest -q`），跑完自动执行，用退出码判定。
  验证通过之后如果又有文件被改动，状态会降级成「已失效」，不会骗人。
- **控制面板**：跨空间看正在跑和刚跑完的任务，直接 `@空间名 任务描述` 下发任务；
  收脚本投来的消息；记待办。

### 3. 无人值守

```bash
sa run "整理 ~/Downloads，把截图归到 Screenshots 子目录"
sa run "跑一遍测试并总结失败原因" --allow bash
sa run "..." --cwd ~/projects/foo --allow write_file,edit_file
```

跑完就退出，适合放进 cron / launchd。没有人在终端前面，所以需要确认的写操作一律按拒绝处理，
要放行就用 `--allow` 逐个列出工具名。

脚本和定时任务也可以反过来给 sa 发消息，投进控制面板：

```bash
sa inbox push -t "备份完成" -b "NAS 增量备份 12.3 GB"
uv run pytest 2>&1 | sa inbox push -t "夜间测试" --level warn --source schedule
```

不需要 `sa serve` 在跑。详见 [给 sa 发消息](docs/send-message.md)。

## 权限

读操作（看目录、读文件、搜索）直接执行；写文件、改文件、跑命令会在终端问一句：
`y` 允许、`a` 本次会话都允许、其他键拒绝。以下两种情况连问都不问，直接拒绝并把原因告诉模型：

- 目标路径在工作目录之外（`../secret.txt`、`/etc/hosts`）
- 必然造成不可恢复损失的命令（`rm -rf ~`、`rm -rf /usr`、`mkfs`、`curl … | sh`、fork bomb 等）

拒绝理由会回给模型，它通常能自己换个办法。工作台里这个确认变成一张审批卡，
选「允许」「拒绝」或「本次会话始终允许」。

API key 只从环境变量或 `~/.simpleagent/.env` 读，不写进配置文件，也不会出现在 trace 里；
`bash` 工具启动子进程时会把这些 key 从环境变量里摘掉，免得模型 `env` 一下就看到了。

## MCP server

在 `~/.simpleagent/config.toml` 里加一段，比如让模型能读写 `~/notes`：

```toml
[mcp_servers.filesystem]
command = "npx"
args = ["--prefer-offline", "-y", "@modelcontextprotocol/server-filesystem", "~/notes"]
```

`--prefer-offline` 让 npx 本地有缓存就不联网；不加的话每次启动都会去 npm 仓库查最新版，
网络或代理不通时会一直挂到超时。

```bash
sa mcp list              # 把每个 server 启动一遍，列出状态和工具（不需要 API key）
```

- **启动**：`sa` / `sa run` / `sa serve` 启动时把所有 server 一起拉起来，等它们都有结果再开始；
  某个 server 起不来只影响它自己。REPL 里按 Ctrl+C 可以跳过卡住的 server，`/mcp` 看详情。
- **按需启动**：不常用的 server 写 `start = "lazy"`。sa 记住它上次连上时的工具清单
  （`~/.simpleagent/mcp_cache/`），之后启动就不等它，模型第一次调用它的工具时才拉起来；
  起不来的话报错交给模型，不影响启动。第一次还没缓存时照常启动一次；`sa mcp list` 会真连一遍并刷新缓存。
- **权限**：server 标了只读（`readOnlyHint`）的工具免确认，其余要确认，`sa run` 里要用
  `--allow mcp__filesystem__write_file` 这样逐个放行。**工作目录边界管不到 MCP 工具**，
  server 能碰什么由它自己的参数决定——上面给的 `~/notes` 就是它能读写的全部范围。
- **密钥**：server 默认只拿到 `HOME`、`PATH`、`LANG`、代理这些环境变量。需要 token 的写成
  `env_vars = ["GITHUB_TOKEN"]`，值放环境变量或 `~/.simpleagent/.env`，不要写进配置文件。
- **崩溃**：server 中途退出的话，下次调用时自动重启（5 分钟内最多 3 次）；退出时正在执行的那次调用
  不会自动重试，由模型决定要不要重来。

其余选项（只暴露部分工具、按工具覆盖权限、超时、协议代际）见 `sa init` 生成的配置模板末尾。

## 项目指令、记忆和技能

三样东西都在**会话开始时读一次**，会话中途改了文件，下一个会话才生效（system prompt 在会话内保持不变，
前缀缓存才命中得了）。启动时会打一行「项目指令 … · 记忆 3 条 · 技能 2 个」，`/prompt` 能看到拼出来的全文。

**项目指令**：和 Claude Code、Codex 一样读 `AGENTS.md`。

- 个人的写在 `~/.simpleagent/AGENTS.md`，所有项目通用（比如「回答用中文，先给结论」）
- 项目的放在项目里：从 git 根目录一路到当前目录，每层一份，越深越具体；不在 git 仓库里只看当前目录
- 某一层没有 `AGENTS.md` 就读 `CLAUDE.md`，给 Claude Code 写过的目录不用再抄一份

**长期记忆**：对它说「记住……」，它会调 `memory_write` 存进 `~/.simpleagent/memory/`（每条一个 `.md`，
`MEMORY.md` 是索引）。每个会话开始时索引进 system prompt，要细节时它自己 `memory_read`。

- 写和删要你按 `y` 确认：记忆会进之后每个会话，别让网页或文件里的内容骗它记下假东西。
  嫌烦就在 `[memory]` 里 `confirm_writes = false`
- `sa run` 里没人确认，默认不许写；定时任务要写记忆就 `--allow memory_write`
- 都是普通 Markdown：可以直接改、删、调整 `MEMORY.md` 的顺序和分组，`/memory` 会提示对不上的地方

**技能**：一个目录一个技能，里面一个 `SKILL.md`（格式是 agentskills.io 的开放标准，Claude Code 的技能直接能用）：

```markdown
---
name: weekly-note
description: 写本周小结。用户要「周报」「本周小结」时使用
---
1. 用 list_dir 看一下工作目录……
2. 按下面的格式输出……
```

- 放在 `~/.simpleagent/skills/<名字>/`（个人），或者项目里的 `.agents/skills/<名字>/`、`.claude/skills/<名字>/`；
  同名的个人技能优先
- system prompt 里只有名字和 `description`，模型觉得任务对得上才用 `load_skill` 读正文，
  所以 `description` 要写清楚「什么时候用」
- 也可以自己点名：`/weekly-note 这周重点是 M7`，正文里的 `$ARGUMENTS` 会换成后面的补充说明。
  frontmatter 加 `disable-model-invocation: true` 的技能只能这样手动调用

## 配置和数据

配置在 `~/.simpleagent/config.toml`（`sa init` 生成，里面每项都有注释）：

| 配置项 | 作用 |
|---|---|
| `default_profile` | 默认用哪个模型 profile |
| `show_reasoning` | 是否显示思考内容 |
| `max_steps` | 一轮对话最多请求模型几次，默认 20 |
| `system_prompt` | 覆盖默认的 system prompt |
| `[trace]` | 是否把每次请求响应落盘，调试协议时用 |
| `[tool_output]` | 工具结果回给模型的字符和行数上限 |
| `[context]` | 上下文快满时怎么腾地方：占到多少比例清理旧工具结果、保留最近几个，再满就把早期对话压成摘要 |
| `[instructions]` | 读哪些指令文件（默认 `AGENTS.md`，没有再 `CLAUDE.md`）、总字数上限 |
| `[memory]` | 长期记忆开关、写记忆要不要确认 |
| `[skills]` | 技能开关、除了 `~/.simpleagent/skills/` 还从哪些目录找（比如加上 `~/.claude/skills`） |
| `[panel]` | 控制面板的消息多久自动归档 |
| `[profiles.*]` | 各家模型：`base_url`、`api_key_env`、`model`、`context_window`，以及各家私有参数 |
| `[mcp_servers.*]` | MCP server：`command`、`args`、`env_vars`，以及工具过滤、权限覆盖、超时 |

数据都在 `~/.simpleagent/` 下（可以用环境变量 `SIMPLEAGENT_HOME` 换个位置；在源码仓库里 `uv run sa` 用的是 `~/.simpleagent-dev/`，见「开发」）：

```
~/.simpleagent/
  config.toml          配置
  .env                 API key（建议 chmod 600）
  AGENTS.md            个人指令，所有项目通用
  memory/              长期记忆：MEMORY.md 索引 + 每条一个 .md
  skills/              个人技能：<名字>/SKILL.md
  sessions/            终端会话历史（JSONL）
  spaces/              工作台的空间和它们的会话
  panel/               控制面板的消息和待办
  tool_outputs/        被截断的完整工具输出，保留 7 天
  mcp_cache/           按需启动的 MCP server 上次的工具清单
  traces/              每次请求和响应的原始记录
```

## 文档

- [架构设计](docs/ARCHITECTURE.md)：选型、设计原则、模块划分
- [路线图](docs/ROADMAP.md)：M0–M9 里程碑与当前进度
- [发布日志](docs/releases/)：每个版本的变化
- [变更记录](docs/changelog/)：每次推送的细节
- [学习笔记](docs/notes/)：每个里程碑学到的东西和踩过的坑
- [给 sa 发消息](docs/send-message.md)：脚本、定时任务、别的 agent 往控制面板投消息（CLI / HTTP）

## 开发

```bash
uv sync              # 安装依赖
uv run pytest        # 测试（不联网，用 FakeLLM）
uv run ruff check    # lint
uv run ruff format   # 格式化
```

同一台机器上既开发又日常用时，两边是隔开的：

| | 日常用 | 开发 |
|---|---|---|
| 命令 | `sa`（从 GitHub 装的快照） | 仓库里 `uv run sa` |
| 代码 | 装的那个提交，本地改动不影响 | 工作区当前代码 |
| 数据目录 | `~/.simpleagent/` | `~/.simpleagent-dev/` |
| `sa serve` 默认端口 | 8384 | 8385，两个可以同时开 |

从源码仓库跑（包文件往上两级有 `pyproject.toml` 和 `.git`，worktree 也算）就是开发模式，
`sa --version`、REPL 和 `sa serve` 启动时都会标出来。开发目录第一次用要初始化，key 可以跟日常共用一份：

```bash
uv run sa init
ln -s ~/.simpleagent/.env ~/.simpleagent-dev/.env
```

优先级是 `SIMPLEAGENT_HOME` > 开发模式 > `~/.simpleagent`。偶尔想拿开发代码读日常数据，
就显式写 `SIMPLEAGENT_HOME=~/.simpleagent uv run sa`。

协作约定（包括用 AI 改这个仓库时的规矩）见 [AGENTS.md](AGENTS.md)。
