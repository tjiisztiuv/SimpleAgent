# 路线图

排序思路：先把核心机制（loop、工具、权限、会话）打扎实，然后**尽早**做出“无人值守定时跑任务”，再逐步补 MCP、上下文工程、记忆、Skills、子 agent。

每个里程碑的交付物 = 能跑的代码 + 测试 + `docs/notes/` 下一篇学习笔记。

| 状态 | 里程碑 | 内容 | 学习重点 |
|---|---|---|---|
| ✅ | **M0 脚手架** | uv 项目、配置加载、trace 落盘、FakeLLM、pytest + ruff | 项目骨架、可测试性 |
| ✅ | **M1 流式对话** | `sa` REPL；OpenAI 兼容流式客户端；`/model` 切换 profile；思考内容显示；usage 统计；Ctrl+C 中断 | Chat Completions 协议、SSE、各家兼容差异 |
| ✅ | **M2 工具调用 + Agent Loop** | Tool 抽象和注册表；`read_file / write_file / edit_file / list_dir / glob / grep / bash`（带超时）；只读并行、含写串行；错误回传；输出截断落盘；中断时补结果；`max_steps` | Function calling、ReAct 循环、错误自愈 |
| ✅ | **M3 权限 + 会话 + Headless** | allow/ask/deny 规则、工作目录边界、危险命令识别；JSONL 会话和 `sa --resume`；`sa run "..."` 单次无人值守执行 | 人在回路、状态持久化、无人值守的安全边界 |
| 🚧 | **M4 个人自动化 v1** ⭐ | `sa daemon`（croniter）；`schedules.toml` 定义任务（prompt / profile / allowed_tools / notify）；`web_fetch`、`notify`（macOS 通知 + 飞书/Telegram webhook）、`schedule_add/list/remove` 工具；运行日志；可选 launchd 常驻 | 事件驱动 agent、无人值守的权限策略 |
| ✅ | **M5 MCP 客户端** | 手写 stdio JSON-RPC（`server/discover` 探测，新协议每个请求带 `_meta`，旧 server 退回 `initialize`；`tools/list` → `tools/call`）；工具名 `mcp__<server>__<tool>`；子进程生命周期管理；先接 `@modelcontextprotocol/server-filesystem` 验证，再接日历/邮件/IM；远程 HTTP 和 OAuth 以后引入官方 `mcp` SDK | MCP 协议、工具生态接入 |
| ✅ | **M6 上下文工程** | token 预算（上次 usage + 字符估算）；三级策略：写入时截断 → 清理旧 tool 结果 → LLM 摘要压缩（不拆开 tool_call 和它的结果）；`/compact`；保持 system prompt 和工具列表稳定以命中前缀缓存 | 上下文窗口管理、缓存友好的 prompt 设计 |
| ✅ | **M7 记忆 + Skills + 项目指令** | `memory/` 文件记忆 + `MEMORY.md` 索引注入 prompt，配 memory 工具；Skills 只把 frontmatter 描述放进 prompt，正文由 `load_skill` 按需加载；自动注入 cwd 下的 `AGENTS.md` | 长期记忆、渐进式披露 |
| 🚧 | **M8 子 agent + 外部 agent + Hooks** | `task` 工具启动子 agent（独立上下文、受限工具集、只返回结论）；Claude Code / OpenCode 包装成工具（无头 CLI 流式输出，保存 session id 以便追问）；`todo` 工具；pre/post tool hooks（外部命令） | 上下文隔离、任务分解、多 agent 协作 |
| | **M9 实验区（持续）** | `evals/`：真实任务加校验脚本，对比不同模型和 feature 开关；新 feature 通过开关接入，结论写进 notes | 用评测驱动迭代 |
| | **W 个人 AI 工作台** | 桌面客户端 + 常驻后台引擎（本地 API、通知路由、客户端审批）。功能清单和客户端技术栈**待设计** | 客户端/服务端分离、事件推送 |

M2 已完成（2026-09-17）：7 个内置工具、输出截断落盘、只读并行/含写串行都齐了；学习笔记见 [notes/M2-tools-and-agent-loop.md](notes/M2-tools-and-agent-loop.md)。
M3 已完成（2026-09-17）：权限拆成「判定 + 询问」两层，写操作默认询问、目录越界和危险命令直接拒绝；
会话落 `sessions/*.jsonl` 并能 `--resume` 恢复；`sa run` 走白名单审批，没人确认时一律拒绝而不是卡住。
学习笔记见 [notes/M3-permissions-sessions.md](notes/M3-permissions-sessions.md)。

M3 剩下的两件事（不挡 M4 的路，到时候按需补）：权限等级目前写死在工具上，**还没进配置文件**
（`config.toml` 里不能写「这个项目 bash 全部放行」），M4 的定时任务要用到这套配置；`--allow` 只支持工具名，不支持模式匹配。
2026-09-25 补了一半：加了权限模式（只读 / 工作区 / 全放行，照 dsh 的预设），`config.toml` 的
`[permissions] mode`、`sa --mode`、REPL 的 `/mode`、客户端每个空间单独设、定时任务的 `mode` 都能指定，
见 [design/permission-mode.md](design/permission-mode.md)。按工具、按命令写的规则还没做；
bash 的系统级沙箱（macOS Seatbelt）先搁置，以后做成一个可开关的选项。

M8 的「外部 agent」提前做掉了一半（2026-09-17，随 W 里程碑）：Claude Code / OpenCode 已经能作为
**空间执行者**无头跑起来（`src/simpleagent/agents/`，事件流翻译成我们自己的帧，支持 resume 和取消），
但**还没有**做成 `task` 工具、也没有实时审批（两家的无头模式都接不到我们的审批卡，现在只有
只读 / 全放行两档，见 [design/client-ui.md 10.10](design/client-ui.md#1010-外部-cli-执行者claude-code--opencode-真正跑起来了)）。

M8 第一部分「跨空间调度」已完成（2026-09-24）：指挥台里直接说任务，调度者（保留空间 `sp_command` 里的
一个内置 loop 会话，只有 `propose_plan` / `dispatch` 两个工具）按空间简介挑空间派发，子任务是目标空间里的
普通会话、跑完把结论交回来；跨空间先出计划卡等确认（代码层面强制），互不依赖的步骤并行。空间之间的信息只经
调度者中转。空间设置里新增「简介」，可让模型自动摘要。方案与改动见 [design/command-dispatch.md](design/command-dispatch.md)。
M8 第二部分「追问已有会话」已完成（2026-09-24）：调度者多了 `recent_sessions` / `followup`，能查到近期会话（以前派的、
`@` 直接下发的、手动开的）并接着问，沿用它的上下文；指挥台卡片上也能点「追问」直接对那个会话说。后端加了会话互斥，
同一个会话同时只跑一轮，被占着时再发返回 409。方案与改动见 [design/session-followup.md](design/session-followup.md)。
`task` 工具（会话内起子 agent）、`todo`、hooks 还没做。

M4 进行中（2026-09-21 起），拆成 8 步：① 任务定义（`schedules.toml` + croniter + `sa schedule list`）✅
→ ② 跑一次任务（运行日志、`sa schedule run`）→ ③ 通知（inbox / macOS / 飞书 / Telegram + `notify` 工具）
→ ④ `web_fetch` → ⑤ `sa daemon` → ⑥ `schedule_add/list/remove` 工具 → ⑦ launchd 常驻 → ⑧ 真实验证与笔记。
心跳不单独做机制：`notify_on = "error"` 的任务 + 模型按需调 `notify` 就是心跳。白名单模式匹配（M3 遗留）暂不做。

M5 已完成（2026-09-21）：手写的 MCP 客户端分四层（stdio 传输 → 协议会话 → 包装成 `Tool` → 多 server 管理），
`[mcp_servers.<名字>]` 配置的 server 在 REPL、`sa run`、`sa serve` 里都能用，另有 `/mcp` 和 `sa mcp list`。
MCP 规范 2026-07-28 版取消了 `initialize` 握手（改成每个请求在 `_meta` 里带版本和能力），但实测官方
filesystem server 还只讲旧协议，所以按规范建议先用 `server/discover` 探测、不行再退回 `initialize`，两代都支持。
用 deepseek-flash 实测：模型能调用 `mcp__filesystem__*` 工具，内置 `write_file` 被工作目录边界拒绝后会自己改用
MCP 的写文件工具。学习笔记见 [notes/M5-mcp-client.md](notes/M5-mcp-client.md)。
M5 没有接日历 / 邮件 / IM：它们大多是远程 HTTP + OAuth 的 server，等引入官方 `mcp` SDK 时再做。
M5 不依赖 M4 的剩余步骤，M4 的 `sa daemon` 以后直接复用 M5 的 `McpManager`。

M6 已完成（2026-09-22，排在 M4 剩余步骤之前：daemon 无人值守、一轮里可能连调几十次工具，最需要这层保护）。
预算用「上次的实际用量 + 之后新增部分的字符估算」，再用校准系数修正；三级策略是写入时截断（M2 已有）→
清理旧工具结果（免费、确定）→ LLM 摘要压缩（切点不拆 tool_call 和结果）；另有 `/context`、`/compact`、
上下文超长报错兜底。重点是和前缀缓存的取舍：摘要请求复用上一次请求的前缀，实测 DeepSeek 命中从 30%～58%
修到 95%～97%（思考内容回传规则、先清后压都会破坏前缀）。工作台改成两份历史，顺带修掉了中断后历史不合法的 bug。
学习笔记见 [notes/M6-context-engineering.md](notes/M6-context-engineering.md)。

M7 已完成（2026-09-23）：新的 `knowledge/` 包在会话开始时读三样东西——项目指令（个人 `AGENTS.md` +
git 根到 cwd 每层一份，没有就读 `CLAUDE.md`）、长期记忆（`memory/` 每条一个文件，`MEMORY.md` 索引进
system prompt，`memory_read / write / delete` 三个工具，写入默认要确认）、技能（SKILL.md 开放标准，
名字和描述进 prompt，`load_skill` 按需读正文，`/技能名` 手动调用）。三样都「会话内不变」，工作台按会话
缓存 system prompt，保住 M6 的前缀缓存。REPL 加了 `/memory`、`/skills`、`/prompt`。deepseek-flash 实测：
说「记住」第一步就调 `memory_write`、新会话只看索引那一行就答对、任务对上描述就先 `load_skill`；
三样合计每次请求多约 1,000 token。学习笔记见 [notes/M7-memory-skills-instructions.md](notes/M7-memory-skills-instructions.md)。
工作台左栏的「知识库」页面还没做（能在对话里用记忆和技能，但还不能在界面上浏览、编辑）。

M4 完成后就是一个每天能真正用上的自动化 agent；M5–M8 在此基础上逐步增强。工作台的前置条件是 M2 的接口约定（见 [ARCHITECTURE.md](ARCHITECTURE.md#m2-起就要守住的接口约定)）和 M4 的后台常驻进程，具体时间点等设计完再定。

## 各里程碑验证方式

- **M2**：用 FakeLLM 测多轮 tool_calls、并行调用、非法 JSON、中断补结果；用真实模型实测“统计当前目录下 .py 文件的总行数”
- **M3**：拒绝权限后模型能收到拒绝原因；`sa --resume` 恢复后能接着聊；`sa run` 碰到需要 ask 的工具自动拒绝
- **M4**：添加一个每分钟触发的任务，启动 `sa daemon`，确认收到 macOS 通知并生成运行日志
- **M5**：接上 filesystem MCP server，模型能调用 `mcp__filesystem__*` 工具
- **M6**：用 FakeLLM 构造超出预算的历史，断言压缩后 tool_call 和结果配对完整、总 token 数在预算内
- **M7**：FakeLLM 走通「记住 → memory_write → 下个会话索引里有」和「prompt 里只有描述 → load_skill → 正文进上下文」；
  断言一个会话里（含工作台跨轮）system prompt 不变；真实模型实测记忆写入、跨会话读取、按描述加载技能
