# 权限控制调研：pi、dsh、Claude Code 与 SimpleAgent（2026-09）

> 调研目的：看清 pi、dsh、Claude Code 这三个 agent 是怎么控制「模型能做什么」的，再对照梳理 SimpleAgent 当前的权限实现，找出值得借鉴的做法和现有的缺口。
>
> 调研时间：2026-09-25。
>
> 信息来源：pi 和 dsh 来自本机源码和随包文档；Claude Code 来自官方文档。没有用第三方整理。
> - **pi**：`~/dev_code/pi`（fork 自 earendil-works/pi，最新提交 2026-09-18，`pi-coding-agent` 0.85.1）。
> - **dsh**（DeepSeek Harness）：本机全局安装的 `@deepseek-ai/dsh` 0.1.5-rc.3（`/opt/homebrew/lib/node_modules/@deepseek-ai/dsh`），加上源码仓库 `~/dev_code/deepseek-harness`（0.1.7-rc.2，含 `.agents/notes/` 设计笔记）。两个版本在权限相关的默认组合上一致；Auto review 只在源码里有，而且是实验包，默认不装。
> - **Claude Code**：官方文档 [Configure permissions](https://code.claude.com/docs/en/permissions)、[Choose a permission mode](https://code.claude.com/docs/en/permission-modes)、[Configure the sandboxed Bash tool](https://code.claude.com/docs/en/sandboxing)，2026-09-25 抓取，对应 v2.1.2xx。Claude Code 不开源，这部分只能以文档为准。
> - **SimpleAgent**：当前 `main`（b471811），文中的判断都对照过代码，部分用脚本实测过。

---

## 0. 三个核心结论

**结论一：四家落在同一条光谱的不同位置上。**

- **pi：核心里没有权限系统。** 官方立场是「No permission popups」：内置工具直接执行，要确认流程就自己写扩展，要真正隔离就把 pi 放进容器或 micro-VM。核心只提供一个能拦截、能改参数的 `tool_call` 钩子。
- **dsh：边界交给操作系统，审批只管「越界那一次」。** 默认把 bash 放进内核级文件沙箱（macOS Seatbelt / Linux bwrap、Landlock / Windows ACL），工作区里随便写，工作区外的写入被内核拒绝。被拒之后模型可以带理由申请一次更宽的权限，人批一次只放行这一次调用。它**完全不解析命令字符串**。
- **Claude Code：规则、模式、沙箱三层叠在一起，是四家里最全的。** 规则（allow / ask / deny）按命令前缀和路径精确到单条命令；模式（Manual / acceptEdits / plan / auto / dontAsk / bypassPermissions）决定默认问不问；沙箱（Seatbelt / bubblewrap）在内核里限制 bash 的文件**和网络**访问。它同时做了 dsh 刻意不做的两件事：解析命令文本，以及持久化的「不再询问」。
- **SimpleAgent：在进程里逐次判定，拿不准就问人。** `Policy` 按「工具等级 + 改动路径 + 命令字符串」判出 allow / ask / deny，ask 交给前端各自的审批器。没有 OS 级边界。

**结论二：分歧的根源是「谁来判断这次操作危不危险」。**
pi 认为 harness 判断不了，所以不假装能判断，交给容器。dsh 认为看命令字符串不可信，它的设计笔记原话是「无法理解展开/子进程/符号链接；严格尝试（运行它，让内核决定）是唯一可信的拒绝信号」。Claude Code 两头都做：命令文本规则用来减少提问，文档明说它「不是安全边界」（`bash -c 'rm …'` 挡不住），真正的边界交给沙箱。SimpleAgent 用规则加人来判断，好处是轻，代价是规则能被绕过（实测见 §5.6）。

**结论三：对 SimpleAgent 最有价值的借鉴是四件事。**
一是 dsh 把「沙箱模式 + 审批策略」打包成一个预设（只读 / 工作区可写 / 全放行），用户只拨一个旋钮，Claude Code 的 acceptEdits 模式也是同一个思路；二是 dsh 刻意**不做**「始终允许」，因为说不清它的作用范围，Claude Code 则把它做成按命令前缀记的规则，只在能把放行范围完整给人看时才提供。SimpleAgent 的「a」按工具名放行，两边都没这么粗；三是 dsh 的审批结果词汇封闭、默认拒绝、全程留审计；四是 pi 的可拦截钩子和 Claude Code 的 PreToolUse hook，正好契合工作台「逻辑用户自己用 Python 控制」的方向。

---

## 1. 总表

| 维度 | pi | dsh | Claude Code | SimpleAgent |
|---|---|---|---|---|
| 基本立场 | 核心不管，隔离交给容器/VM | OS 沙箱划写边界，越界时一次性升权 | 规则 + 模式 + 沙箱三层 | 进程内逐次判定，拿不准问人 |
| 开箱默认 | 所有工具直接执行 | `workspace-write` + `ask`：工作区内不用问，工作区外的写被内核拒 | API 账号是 Manual（只有读不问）；Pro / Max / Team 是 auto（分类器代替人） | 读类放行；写文件、bash、记忆写入、没标只读的 MCP 每次问 |
| 靠什么判断 | 扩展自己定（示例用正则） | 内核说了算，不解析命令 | 规则按命令文本和路径匹配；沙箱由内核执行；auto 模式另有分类器模型 | 工具等级 + 改动路径 + 命令字符串黑名单 |
| 文件写边界 | 无（靠扩展或容器） | bash 内核级；write/edit 进程内围栏；两者共用同一份可写根目录 | 开沙箱时 bash 内核级；Edit/Write 走权限规则；两边的路径合并成一份沙箱配置 | write/edit 解析软链接后必须在 cwd 内；**bash 的命令内容不受约束** |
| 读限制 | 无 | 无（沙箱只管写） | 默认能读整台机器；可用 `Read` deny 规则、`denyRead`、`blockReadsOutsideWorkingDirectories` 收紧 | 无（`allow_read_outside=True`） |
| 网络限制 | 无（sandbox 示例扩展能按域名白名单） | 无（出站代理不是限制） | 沙箱内走代理，按域名白名单，新域名先问 | 无（bash 本身要 ask） |
| 危险命令识别 | 示例扩展里的正则 | 刻意不做 | 受保护路径（`.git`、`.claude`、shell 配置等）和关键路径删除（`rm -rf ~` 之类），任何 allow 规则都放不过 | 黑名单：格式化/关机等程序、管道喂 shell、递归删关键目录 |
| 审批粒度 | 扩展自己定 | 只有「允许这一次」，刻意不做「始终允许」 | 一次；文件改动「本会话」；bash「不再询问」存成命令前缀规则（复合命令每段一条） | 「y」这一次；「a」本会话内**这个工具名**全部放行 |
| 无人值守 | 扩展自己看 `ctx.hasUI` | 策略 `never` 或没有应答者都算拒绝 | `dontAsk` 模式：要问的一律拒绝，只跑预先放行的 | 没有审批器或不在白名单都算拒绝，原因回给模型 |
| 子 agent | 核心没有子 agent | 进程内子 agent 审批钉死为 `never`，权限就是委派时的沙箱快照 | 共用父会话的沙箱；auto 模式下分类器在派出、每步动作、交回报告三处检查 | 外部执行者只有只读 / 全放行两档；派发出去的空间用各自的审批器 |
| 模式切换 | `--tools` 白名单、`setActiveTools` | 三个预设 + `/permission` 命令（另有实验性 Auto） | Shift+Tab 循环切换；`defaultMode`；项目级配置不能把默认设成 auto 或 bypass | 无，等级写死在工具上 |
| 扩展点 | `tool_call`（可拦截、可改参数）、`tool_result`、`user_bash`、可替换的执行后端 | `tools/pre-execute`（allow/deny/ask）、单调 guard、审批应答者、Claude Code hooks 桥 | PreToolUse hook（可拦、可问、可放行，但压不过 deny / ask 规则）、PermissionRequest hook | 没有公开钩子（Policy 和 Approver 可以整体替换） |
| 审计 | 会话里有工具结果 | `approval/asked` + `approval/decided` 成对落日志，不进模型上下文 | `/permissions` 里能看到最近被拒的动作并重试 | `ToolResult.decision` 记了判定；谁批的、是不是「始终允许」没记 |
| 模型知道什么 | 看扩展 | 每次请求前追加一份「当前策略」快照；被拒时附标记和升权提示 | 被拒时拿到原因；沙箱违规会写明被拦的路径或域名 | 被拒时拒绝原因作为工具错误回给模型 |
| 项目配置信任 | 加载项目 `.pi/` 下的设置和扩展前先问 | 项目 `.env` 不许设代理变量 | 工作区信任：没信任的目录不跑项目里的 hooks 等；项目配置放不宽 bypass / auto | 项目 AGENTS.md 和技能直接读（都是文本，没有项目级代码或配置） |

---

## 2. pi：核心不管，只留钩子

### 2.1 立场

README 的 Philosophy 一节（`packages/coding-agent/README.md:503`）：

> **No permission popups.** Run in a container, or build your own confirmation flow with extensions.

`docs/security.md` 讲得更直白：pi 用启动它的用户的权限运行，没有内置沙箱，而且是**故意**的。理由是 pi 要调用项目里的整套工具链，一个半吊子的进程内沙箱容易被误当成安全边界，实际上它还是依赖宿主的 shell、文件系统、包管理器和凭据。真正的隔离要靠操作系统或虚拟化。来自仓库文件、注释、构建输出的 prompt injection 被明确列为「本地 agent 的预期风险」，pi 不承诺能防住。

### 2.2 核心里真正存在的四样东西

**① 项目信任（project trust）：只管「加载什么」，不管「模型做什么」。**
在带 `.pi/settings.json`、`.pi/extensions` 等项目资源的目录启动时，按 `defaultProjectTrust`（默认 `ask`）问一句要不要信任这个项目，决定存到 `~/.pi/agent/trust.json`，按目录就近生效。不信任就不加载项目里的设置和扩展。`AGENTS.md` / `CLAUDE.md` 这些上下文文件不受它管，照样加载。非交互模式不弹窗，`--approve` / `--no-approve` 可以单次覆盖。这是 0.85 前后才加的（CHANGELOG #5332）。

**② 工具开关。**
`--tools read,grep,find,ls` 是工具白名单，`--no-tools` 全关，扩展里用 `pi.setActiveTools()` 运行时切换。这是 pi 唯一内置的「限制模型能力」的手段，粒度是整个工具。

**③ `beforeToolCall` → 扩展的 `tool_call` 事件。**
这是所有权限扩展的基础。链路是 `agent-loop.ts:681` 的 `prepareToolCall()`：先校验参数，再调 `config.beforeToolCall`；返回 `{ block: true }` 就生成一条错误结果，工具不执行。`agent-session.ts:490` 把它接到扩展系统，`extensions/runner.ts:1007` 的 `emitToolCall()` 按加载顺序逐个跑 handler，**第一个 block 就返回**。几条值得注意的语义：

- `event.input` 可以直接改，改完不再校验，后面的 handler 看到的是改过的参数。可以借此把 `rm` 改写成 `trash`，或者给命令包一层沙箱。
- handler 抛异常等于拦截（fail-safe）。
- `terminate: true` 表示拦截后让 agent 停下，但只有同一批结果全都要求停才真停。
- 同一条消息里的多个工具调用是**先逐个过 `tool_call`，再并发执行**。

**④ 可替换的执行后端。**
内置工具都接受一个 `operations` 参数（`BashOperations`、`EditOperations` 等），默认是本地实现。换掉它就能把执行路由到 SSH 远端或 micro-VM，工具本身不用改。

### 2.3 官方示例扩展

| 示例 | 做法 |
|---|---|
| `permission-gate.ts` | 正则匹配 `rm -rf`、`sudo`、`chmod 777`，命中就弹 Yes/No；没有 UI 时直接拦 |
| `protected-paths.ts` | write/edit 的路径含 `.env`、`.git/`、`node_modules/` 就拦（用 `includes` 做子串匹配，很粗） |
| `plan-mode/` | 切到只读工具集；bash 走一份「破坏性命令黑名单 + 安全命令白名单」，不在白名单里的都拦 |
| `sandbox/` | 用 `@anthropic-ai/sandbox-runtime` 替换 bash 工具：文件读写黑白名单 + **网络域名白名单**，macOS 用 sandbox-exec，Linux 用 bubblewrap |
| `gondolin/` | 把全部内置工具路由进 QEMU micro-VM，宿主 cwd 挂到 `/workspace` |

另外 `docs/containerization.md` 给了 Gondolin、Docker、OpenShell、Docker Sandboxes 四种跑法。

### 2.4 取舍

好处是核心极简，不给人虚假的安全感，想要什么策略都能自己写，而且钩子能改参数，表达力比「允许/拒绝」强。代价是开箱即裸奔，安全完全靠用户自觉；示例扩展都是字符串匹配，和 SimpleAgent 的黑名单一样能绕过。

---

## 3. dsh：内核划边界，越界才问人

### 3.1 组成

dsh 按插件拆得很细。和权限相关的包：

| 包 | 职责 |
|---|---|
| `dsh-sandbox` | 沙箱接口：模式词汇、升权规则、拒绝标记文本、可写根目录的唯一来源 |
| `dsh-sandbox-local` | 各平台后端：macOS Seatbelt、Linux bwrap（不行再用 Landlock）、Windows ACL 受限令牌 |
| `dsh-sandbox-policy` | 每个会话当前是什么模式；给模型的策略说明 |
| `dsh-bash-sandbox` | 在沙箱里跑 bash 的执行器 |
| `dsh-fs-sandbox` | write/edit 工具的进程内围栏 |
| `dsh-fs-observation-policy` | 没读过的文件不许改；读完之后文件又变了也不许改 |
| `dsh-user-approval` | 审批服务：`ask` / `never` 两种策略，四种封闭结果 |
| `dsh-permission-presets` | 把「沙箱模式 + 审批策略」打包成预设，提供 `/permission` 命令 |
| `dsh-tools` | 工具执行流水线，`tools/pre-execute` 能返回 allow / deny / ask |
| `dsh-hooks-claude-code` | 直接复用 Claude Code 的 hooks.json（只支持部分事件） |
| `dsh-plan-mode` | 计划模式，**只靠提示词约束，不限制工具** |
| `dsh-experimental-auto-review` | 实验：每次工具调用前让另一个模型审查 |

### 3.2 三个预设和默认值

`dsh-base` 的 `cordis.patch.yml`（本机安装版第 211–241 行）：

| 预设 | 沙箱模式 | 审批策略 | 含义 |
|---|---|---|---|
| `read-only` | 不许写（`/dev/null` 这类除外） | `ask` | 想写就得申请升权 |
| `workspace-write`（**默认**） | 工作区 + 系统临时目录可写 | `ask` | 工作区里自由干活，越界时问 |
| `danger-full-access` | 不隔离 | `never` | 全放行，也不再问 |

默认值可以用环境变量 `DSH_PERMISSION_MODE` 覆盖。你本机 `~/.dsh/profiles/{web,desktop}/cordis.patch.yml` 都是空的，所以用的就是默认的 `workspace-write` + `ask`。预设只是拨两个旋钮的快捷方式，旋钮值各自记在会话日志里；两个旋钮拨出预设表里没有的组合时，界面显示推导出来的 `custom`。

### 3.3 核心流程：先跑，被拒，再申请一次

```
模型调用 bash（不带任何权限参数）
  → 在当前会话模式的沙箱里执行
  → 内核拒绝了工作区外的写
  → 结果里带标记：[sandbox: file access denied under workspace-write mode]
    和提示：[sandbox: escalation available — retry this exact command once
            with sandbox_permissions + justification; the approval prompt asks the user]
  → 模型在同一轮里原样重试，加上
      sandbox_permissions: "danger-full-access"   ← 够用的最窄更宽模式
      justification: "一句话说明为什么要"
  → approveEscalation()：检查确实更宽 → 问审批服务
  → allowed-once：只给这一次调用盖上更宽模式并执行；其他结果都拒绝，工具体不执行
```

几个刻意的设计（出自 `.agents/notes/implemented/feature/2026-07-06-sandbox.zh.md` 的「曾考虑的替代方案」）：

- **不做命令字符串预检**，理由见结论二。
- **不自动重试**：重试必须是一次新的、有记录的工具调用，日志里能看到两次调用各用了什么策略。
- **不强制重试命令和被拒命令一字不差**：引号、workdir、环境变量前缀都会让比较失效，真正的防线是人看到命令和理由。
- **升权字段只在挂了沙箱执行器时才出现在 schema 里**：不提供 harness 兑现不了的选项。
- **拒绝标记和升权提示放在被拒的那一刻，而不是写进 system prompt**。他们先试过在 system prompt 里写「bash 在 read-only 沙箱里跑」，结果模型不敢动手，首批人工测试 12 轮里有 5 轮一个工具都没调就结束了。现在的做法是每次请求前在历史末尾追加一份当前策略快照（system prompt 不变，KV cache 不失效），只读模式的那段措辞还特意加了「不要光凭这条策略就拒绝做修改，先正常尝试」。

### 3.4 审批服务：窄、封闭、默认拒绝

来自 `dsh-user-approval` 的 README 和 `2026-07-06-approval-seam.zh.md`：

- **结果只有四种**：`allowed-once` / `rejected` / `cancelled` / `unavailable`。后三种各自给模型不同的拒绝原因，模型能分清「人说不」「人没回就取消了」「根本没人能问」。
- **默认拒绝**：没有应答者、应答者出错、返回了不认识的值，都算 `unavailable`，按拒绝处理。服务自己永远不会去问人。
- **`never` 在服务内部、所有应答者之前生效**，后注册的应答者也绕不过去。
- **只能在进行中的轮次里发起审批**，因为轮次是日志的提交边界。
- **每次审批落一对审计事件**（`approval/asked` / `approval/decided`），只进日志，不进模型上下文。
- **请求里不带工具参数**，只带工具名、调用 id、理由；需要展示参数的界面自己按调用 id 去查。
- **刻意不做 `allow_always`**。笔记的原话是：兑现持久授权需要设计授权存储、作用域和撤销，作用域可以是「这次调用、这个路径、这个命令前缀、这个会话还是这个时间窗」，这个问题没想清楚之前不展示，否则「只会制造注定失败的授权」。

### 3.5 其他层

- **`tools/pre-execute` 流水线**（`dsh-tools/lib/index.js:3116`）：waterfall 事件先给出 allow / deny / ask，ask 转给审批服务；然后跑「单调 guard」，任何一个 guard 都能拒绝，已被拒绝的调用没有 guard 能改回允许。默认组合里其实没有插件会返回 ask，它主要留给 Claude Code hooks 桥（`PreToolUse` 的 `ask`）和以后的策略插件用。另外它**故意不允许改参数**，否则日志里记的参数和实际执行的会对不上，这点和 pi 相反。
- **写前必读**（`fs-observation-policy`）：没读过的现有文件不能覆盖或编辑，读完后文件变了（版本不一致）也不行。这是防「覆盖掉别人刚改的内容」，不是安全边界，但默认开着。
- **子 agent 钉死 `never`**（`2026-08-10-subagent-approval-pinned-never.zh.md`）：子 agent 在后台跑，审批请求没有界面能看到，以前的效果是子 agent 悄悄卡住。现在子 agent 的权限就是委派时父会话的沙箱模式，所有 ask 直接拒绝，同时在它的上下文里说明「需要更宽权限就报告限制，不要重试」。要放宽只能先放宽父会话再重新委派。
- **Auto review**（实验包，默认不装，`2026-08-28-auto-review.zh.md`）：沙箱是全放行，但每次工具调用前让另一个模型审查。风险分三档：low（项目内读写、测试构建）放行；medium（删已有数据、force push、部署、对外发送）只有当前用户或直接父级**明确**授权才放行；high（敏感数据外泄）一律拒。审查模型只看用户和父级的原话、项目指令和待执行的动作，**不看主 agent 自己的推理和工具结果**，以此降低 prompt injection 的影响。9 月 24 日又改成：审查拒绝后如果会话策略是 `ask`，再回落到问人，而不是直接判死。

### 3.6 边界和局限

- 沙箱**只管文件写**，不管读，不管网络。Seatbelt profile 是「默认允许 + 禁止写入 + 可写白名单」，bwrap 是「只读挂载整个根目录」，所以工作区模式下 bash 照样能读 `~/.ssh`、能 `curl` 到任何地方。
- write/edit 的围栏是进程内检查，不是内核边界。解析路径到实际写入之间有 TOCTOU 窗口，他们接受这个风险。
- plan mode 只是一段提示词，工具全部可用。
- Claude Code hooks 桥只支持 30 个事件里的 7 个，`PreToolUse` 的 `allow` 不会预批准，`updatedInput` 不会被应用。

---

## 4. Claude Code：规则、模式、沙箱三层

Claude Code 不开源，这一节全部来自官方文档（见开头的链接）。它把权限拆成三层，各管一件事：

| 层 | 管什么 | 由谁执行 |
|---|---|---|
| 权限规则 | 某次工具调用放行、问还是拒 | Claude Code 进程，看命令文本和路径 |
| 权限模式 | 没有规则命中时默认问不问 | Claude Code 进程 |
| Bash 沙箱 | 命令跑起来以后能碰哪些文件、连哪些域名 | 操作系统内核（Seatbelt / bubblewrap） |

文档里一句话点明了规则和沙箱的分工：规则在命令运行前按命令文本判断；沙箱由操作系统对运行中的进程执行，「不管模型选择跑什么都成立，哪怕一条被放行的命令做的事比它名字暗示的多」。

### 4.1 权限模式

| 模式 | 不问就能做的 | 适合 |
|---|---|---|
| `default`（界面上叫 Manual） | 只有读 | 敏感工作，每步自己看 |
| `acceptEdits` | 读 + 工作目录里的文件改动 + 常见文件命令（`mkdir` `touch` `rm` `mv` `cp` `sed`，也限工作目录内） | 事后用 git diff 审改动 |
| `plan` | 读 + 只读命令；不改源码，直到计划被批准 | 先摸清再动手 |
| `auto` | 几乎全部，每个动作由分类器模型在后台审查 | 长任务，减少打扰 |
| `dontAsk` | 读 + 预先放行的规则；**要问的一律拒绝** | CI、脚本 |
| `bypassPermissions` | 全部 | 只在容器 / VM 里用 |

- CLI 里按 Shift+Tab 循环切换，`permissions.defaultMode` 设默认。**项目里的 `.claude/settings.json` 不能把默认设成 `auto` 或 `bypassPermissions`**，否则 clone 一个仓库就可能被放宽权限。
- `bypassPermissions` 拒绝以 root 运行，首次开启要确认一次风险提示。
- 模式只是底线，规则叠在上面：deny 规则在所有模式下都生效，包括 bypass。

**所有模式（含 bypass）都不自动放行的东西：**

- **受保护路径的写入**：`.git`、`.claude`、`.vscode`、`.idea`、`.husky` 等目录，`.bashrc` / `.zshrc` 等 shell 配置，`.gitconfig`、`.npmrc`、`.mcp.json` 等文件。allow 规则也放不过（bypass 除外）。理由是防止弄坏仓库状态和 Claude 自己的配置。
- **关键路径删除**：`rm` / `rmdir` 的目标是根、根下一级（`/usr`、`/etc`）、家目录、工作目录及其父级，或者 `"$DIR"/*` 这种变量为空就变成删根的写法。藏在子 shell、命令替换里也能认出来。任何 allow 规则和 hook 都放不过，连 bypass 模式也要问。

### 4.2 权限规则

格式是 `Tool` 或 `Tool(specifier)`，分 allow / ask / deny 三类，**按 deny → ask → allow 的顺序匹配，先命中的生效，写得再具体也改变不了这个顺序**。任何一层配置的 deny 都能挡住其他层的 allow。只写工具名的 deny（如 `Bash`）会把工具从模型的上下文里整个拿掉。

**Bash 规则怎么匹配**

- `Bash(npm run *)` 按前缀匹配。`*` 要放在子命令后面：`Bash(git *)` 放行所有 git 命令，`Bash(git log *)` 只放行 `git log`。
- **复合命令拆开逐段匹配**：`&&` `||` `;` `|` `&` 和换行都算分隔符，allow 必须每段都命中；deny / ask 只要任一段命中就生效，嵌在子 shell、命令替换、`for` 循环里的也算。
- 匹配前会剥掉一组固定的包装：`timeout`、`time`、`nice`、`nohup`、`stdbuf`、`command`、`builtin`、裸 `xargs`，以及已知安全的环境变量赋值。`npx`、`docker exec`、`devbox run` 这类**不剥**，所以 `Bash(devbox run *)` 等于放行 `devbox run` 后面的任何命令。
- 内置一组只读命令（`ls` `cat` `grep` `find` `git` 的只读形式等），所有模式都不问。但带未加引号通配符的可写命令（`find *` 可能展开出 `-delete`）、解析不了的命令、超过 1 万字符的命令都要问。
- 重定向目标当成文件写入检查：`> file` 要过 Edit 规则、受保护路径和工作目录；`tee` 写的文件也查。

**文档明说规则不是安全边界。** `Bash(rm *)` 挡得住 `rm -rf build/`，挡不住 `/bin/rm -rf build/` 和 `bash -c 'rm -rf build/'`；`Bash(git push *)` 挡不住 `git -C . push`。要挡死得用沙箱，或者用 PreToolUse hook 自己写逻辑。

**「不再询问」存到哪、管多宽**

| 工具 | 「Yes, and don't ask again」的效果 |
|---|---|
| Bash | 按命令存成规则，写进仓库根目录的 `.claude/settings.local.json`，之后所有会话都生效。复合命令每段存一条，最多 5 条 |
| 文件修改 | 只到本会话结束，不落盘 |
| WebFetch | 按域名存规则 |

还有一条原则：**只有当提示能把这个选项放行的全部范围展示给人看时，才提供「不再询问」**；展示不全就只给「允许这一次」。

**和 hook 的关系**：PreToolUse hook 在权限提示之前运行，可以拒绝、强制提问或放行，但放行压不过 deny 和 ask 规则。hook 以退出码 2 拦截时优先于 allow 规则。

### 4.3 Bash 沙箱

- **文件**：可写范围是工作目录、`--add-dir` 加的目录和会话临时目录；**可读范围默认是整台机器**（文档特意提醒 `~/.ssh`、`~/.aws/credentials` 默认读得到，要自己加 `denyRead`）。可写目录里的受保护文件（`.claude` 配置、shell 配置、`.git/hooks` 和 `.git/config`）仍然不许写，因为改了它们就能给自己加权限或塞进在沙箱外运行的 hook。
- **网络**：走沙箱外的代理，按域名白名单放行；默认一个域名都不放行，新域名第一次访问时问人。
- **两种沙箱模式**：auto-allow 模式下，能在沙箱里跑的命令**直接执行，不再提问**，因为沙箱边界替代了逐条确认；regular 模式下照常逐条问。deny 规则、ask 规则和关键路径删除在 auto-allow 下依然生效。
- **逃生口**：沙箱拦下一条命令时，结果里写明被拦的路径或域名；模型可以带 `dangerouslyDisableSandbox` 参数重试，这次在沙箱外运行，走正常的权限流程（Manual 模式下问人）。可以用 `allowUnsandboxedCommands: false` 关掉逃生口（严格沙箱）。这和 dsh 的「被拒后带理由申请一次」是同一个思路。
- 底层能力开源成了独立包 `@anthropic-ai/sandbox-runtime`，pi 的 sandbox 示例扩展用的就是它。

### 4.4 auto 模式：用分类器代替人

- **判定顺序**：先看规则；再自动放行只读动作和工作目录里的文件改动（受保护路径除外，第一次读工作目录外的文件要问）；其余全部交给分类器。
- **进入 auto 时丢掉过宽的 allow 规则**：`Bash(*)`、`Bash(python*)` 这类解释器通配、包管理器的 run 命令、`Agent`。离开 auto 时恢复。
- **分类器看得到什么**：用户消息、工具调用（只读查询除外）、CLAUDE.md。**工具结果被剥掉**，这样文件或网页里的恶意内容没法直接操纵分类器。dsh 的 Auto review 采用了同样的过滤思路。
- **默认拦截**：`curl | bash`、把敏感数据发到外部、生产部署、云存储批量删除、授予 IAM 或仓库权限、改共享基础设施、不可逆地销毁会话开始前就存在的文件、force push。
- **对话里说的边界也算**：用户说「别 push」，分类器就拦 push，直到用户明确撤销；但压缩上下文可能把这句话丢掉，要硬保证还得写 deny 规则。
- **兜底**：连续拦 3 次或累计 20 次，auto 暂停，改回逐条问人。
- **子 agent** 在三处检查：派出时审任务描述，运行时审每个动作，交回时审最终报告。

### 4.5 和 dsh 比

- **相同**：两家都用 Seatbelt / bubblewrap 做 bash 沙箱，都把「沙箱里自动放行，越界时带理由申请一次」当主路径（Claude Code 的 auto-allow + `dangerouslyDisableSandbox`，对应 dsh 的 `workspace-write` + `sandbox_permissions`）；都有「用模型审查动作」的模式，而且审查模型都看不到工具结果。
- **不同**：
  - Claude Code 另有一整套按命令文本匹配的规则系统，外加持久的「不再询问」；dsh 刻意两样都不做。
  - Claude Code 的沙箱也管网络，dsh 不管。
  - Claude Code 的「模式」和「沙箱」是两个独立开关（文档原话：`/sandbox` 不是权限模式）；dsh 把两者打包成预设。
  - Claude Code 有写死的受保护路径和关键路径删除保护；dsh 没有这层。

---

## 5. SimpleAgent 现状

### 5.1 两层结构

`src/simpleagent/permissions.py` 把权限拆成**判定**和**询问**两层：

```
tool_call
  → 解析 JSON、pydantic 校验（失败直接回错误，不进判定）
  → ToolRegistry.judge()：tool.scope(args, ctx) 算出 Scope(paths, command)
  → Policy.decide(permission, scope)，顺序固定：
       工具等级是 deny          → DENY「这个工具已被配置为禁用」
       任一改动路径不在 cwd 内   → DENY「目标路径在工作目录之外」
       command 命中危险命令      → DENY「这条命令请用户自己执行」
       工具等级是 allow         → ALLOW
       其余                     → ASK
  → ASK：没有审批器 → 拒绝（无人值守）
         有审批器   → approver.request() → 允许 / 拒绝
  → 执行 → 截断 → ToolResult(decision=...)
```

`Policy` 是纯函数，不做 IO，三个前端共用，所以危险命令这条底线谁也绕不过去。越界和危险命令排在工具等级前面，工具声明 `allow` 也穿不透边界（`permissions.py:262`）。

### 5.2 各工具的默认等级

| 工具 | 等级 | Scope |
|---|---|---|
| `list_dir` / `read_file` / `glob` / `grep` / `memory_read` | allow | 不报路径（读不受目录边界限制） |
| `write_file` / `edit_file` | ask | 目标路径（`ctx.resolve()` 会解析软链接） |
| `bash` | ask | 只报 `cwd` 参数 + 命令文本（`tools/bash.py:67`） |
| `memory_write` / `memory_delete` | `confirm_writes=true` 时 ask，否则 allow | 无 |
| MCP 工具 | server 标了 `readOnlyHint` 且 `trust_annotations=true` → allow，否则 ask；`permissions = {tool = "allow"}` 可按工具覆盖；`enabled_tools` / `disabled_tools` 控制可见性（`mcp/tools.py:145`） | 无 |
| 指挥台 `propose_plan` | 工具体里直接调审批器，计划卡每次都要人看，而且用独立的「始终允许」记录，不会被别处的「a」放过 | — |

### 5.3 三个审批器

| 审批器 | 用在哪 | 行为 |
|---|---|---|
| `ConsoleApprover`（`ui/approve.py`） | REPL | `y` 这一次；`a` 本会话内这个**工具名**不再问；其他键或 Ctrl+C 都拒绝 |
| `APIApprover`（`serve/approval.py`） | `sa serve` / 桌面客户端 | 推一帧 `approval_request`，然后 `await` 一个 Future；「始终允许」按 `session_id + 工具名` 记在 Runner 的共享字典里 |
| `WhitelistApprover`（`permissions.py:82`） | `sa run`、定时任务 | 只放行 `--allow` / `allowed_tools` 里的工具名，其余一律拒绝 |

### 5.4 外部执行者

Claude Code 和 OpenCode 作为空间执行者时，它们的无头模式没法把审批问回给我们，所以只能预先定档（`agents/base.py:18`）：

- **safe**：Claude Code 用 `--tools` 只留三个只读工具，再加 `--permission-mode dontAsk --permission-prompts none` 兜底；OpenCode 用 `OPENCODE_CONFIG_CONTENT` 注入 `"*": "deny"`，只放行 read / glob / grep / lsp。
- **full**：Claude Code 加 `--dangerously-skip-permissions`，OpenCode 用 `--auto`。

### 5.5 其他相关防护

- bash 子进程的环境变量会去掉各 profile 的 `api_key_env`（`tools/base.py:48`），`env` 一下不会把 key 带进上下文和 trace。
- 同一批里全是只读工具才并行，有写就按顺序逐个执行。

### 5.6 缺口（都已对照代码或实测确认）

**① bash 的命令内容不受工作目录边界约束。**
`bash_scope()` 只把 `cwd` 参数算进 `Scope.paths`，命令里写到哪里 Policy 不管。所以边界只对 write_file / edit_file 是硬的，对 bash 只是「每次问人」。

**② 危险命令黑名单能被绕过。** 用 `inspect_command()` 实测：

| 命令 | 结果 |
|---|---|
| `rm -rf ~`、`rm -rf ~/Downloads`、`rm -rf .`、`curl … \| sh` | 拦住了 |
| `bash -c "rm -rf ~"` | 放过（第一个词是 bash） |
| `find ~ -delete` | 放过 |
| `cd ~ && rm -rf Documents` | 放过（`Documents` 按项目 cwd 解析，不算关键目录） |
| `python3 -c "import shutil; shutil.rmtree(...)"` | 放过 |
| `echo hi > ~/.zshrc` | 放过 |

这正是 dsh 否决字符串预检的理由。单独看问题不大，因为 bash 默认是 ask，人会看到命令。问题出在和 ③ 叠加。

**③ 「a / 始终允许」的粒度是工具名。**
对 bash 选一次 `a`，本会话后面所有 bash 命令都不再问，只剩 ② 那张能绕过的黑名单。客户端的「本次会话始终允许」也一样。dsh 刻意不做 allow-always，正是因为这个作用域问题没想清楚。

**④ 读不设限。**
`read_file` 能读 `~/.ssh/id_rsa`、`~/.simpleagent/.env`，内容会进上下文、发给模型提供商。pi 和 dsh 也都不限读，这是行业普遍做法，但值得知道。

**⑤ 客户端的实时审批卡看不到判定理由（bug，2026-09-25 已修，见 [design/permission-mode.md](../design/permission-mode.md)）。**
`serve/frames.py:137` 的 `approval_request_frame()` 只带 `approval_id / tool_name / arguments`，没带 `reason`；前端 `web/app.js:639` 读的是 `p.reason`，所以是 undefined。只有刷新后从 `GET /api/approvals` 补出来的卡片（`app.js:698`，走 `PendingApprovals.details()`）才有理由。MCP 工具的「会访问外部系统」、Policy 的「无法判断影响范围」这些说明，实时卡片上都看不到。

**⑥ 文档说审批超时会拒绝，代码没做。**
`serve/approval.py` 的模块注释和 `docs/design/client-ui.md:348` 都写「客户端不在线 / 超时则按无人值守策略拒绝」，但 `APIApprover.request()` 只是无限期 `await future`，既不检查客户端在不在线，也没有超时。现在靠 `PendingApprovals` 补发卡片来缓解，但没人点的话任务会一直挂在「运行中」。

**⑦ 权限还没进配置文件（ROADMAP 里的已知项）。**
等级写死在工具上，`config.toml` 里写不了「这个项目 bash 全放行」；`--allow` 只认工具名，不支持 `bash:git *` 这类模式。

---

## 6. 对 SimpleAgent 的建议

按「成本从低到高」排。第 1 条是 bug，可以直接修；其余都是新功能，按 AGENTS.md 的约定要先讲方案、确认后再做。

1. **修 ⑤ 和 ⑥。** 在审批帧里补上 `reason`。审批超时则二选一：真做一个超时（比如跟着任务的最长运行时间走），或者把文档改成「一直等，直到有人处理或任务被取消」。
2. **收紧「始终允许」的粒度。** 这是 ②③ 叠加的解法，性价比最高。可以只对 bash 取消「a」；或者学 Claude Code，把 bash 的「始终允许」记成命令前缀规则（`git status`、`uv run pytest`），复合命令拆开每段一条，并且只在能把放行范围完整给人看时才提供（§4.2）。
3. **权限进配置，做成预设。**（2026-09-25 已做：只读 / 工作区 / 全放行，见 [design/permission-mode.md](../design/permission-mode.md)。） 学 dsh：一个 `mode = "read-only" | "workspace" | "full"` 旋钮，展开成 Policy 参数加审批策略，按空间或会话记。它顺带解决了 ⑦，M4 的定时任务也用得上（比如「这个任务是 workspace 模式，但不许问人」）。
4. **在 `ToolRegistry.execute` 前留一个 Python 钩子。** 对应 pi 的 `tool_call` 和 dsh 的 `tools/pre-execute`：用户写一个函数，拿到工具名和参数，返回 allow / deny / ask。这和工作台「业务逻辑在用户自己的 Python 代码里」的方向一致。要决定的一点是**允不允许改参数**：pi 允许，表达力强；dsh 不允许，保证日志如实。
5. **把审批记进会话。** 现在 `ToolResult.decision` 只记了判定。可以加上「谁批的、是一次还是始终、审批花了多久」，和 dsh 的 asked/decided 成对事件对应，事后复盘和桌面客户端都用得上。
6. **可选：给 bash 加一层 macOS Seatbelt。** 这是学习价值最高的一项。`sandbox-exec -p '<profile>' /bin/sh -c ...`，profile 用 dsh 那套「默认允许 + `(deny file-write*)` + 可写 cwd 和临时目录」。加上以后 bash 的写入边界从「靠人看」变成「靠内核」，也就能照搬 dsh 的「先跑，被拒，再申请一次」。注意 `sandbox-exec` 已被 Apple 标为弃用，但系统还在提供（dsh 也在用）。
7. **别在 system prompt 里宣布「你是只读的」。** dsh 的实测教训：写了模型就不敢动手。被拒时再告诉它原因和出路，效果更好。SimpleAgent 现在就是被拒时才回原因，以后做模式切换时要保持这一点。

---

## 附：关键源码位置

**pi**（`~/dev_code/pi`）
- `packages/coding-agent/docs/security.md`：官方安全立场
- `packages/agent/src/agent-loop.ts:681`：`beforeToolCall` 的调用点
- `packages/coding-agent/src/core/agent-session.ts:490`：接到扩展的 `tool_call` 事件
- `packages/coding-agent/src/core/extensions/runner.ts:1007`：`emitToolCall()`，第一个 block 返回
- `packages/coding-agent/examples/extensions/{permission-gate.ts,protected-paths.ts,plan-mode/,sandbox/,gondolin/}`

**dsh**（`~/dev_code/deepseek-harness`，安装版在 `/opt/homebrew/lib/node_modules/@deepseek-ai/dsh`）
- `packages/bundle/base/cordis.patch.yml`：默认组合（沙箱、审批、预设）
- `docs/subsystems/{sandbox,approval,permission-presets,tools}.zh.md`
- `.agents/notes/implemented/feature/2026-07-06-sandbox.zh.md`：沙箱和升权设计，含被否决的方案
- `.agents/notes/implemented/feature/2026-07-06-approval-seam.zh.md`：审批服务设计，含「为什么不做 allow_always」
- `.agents/notes/implemented/feature/2026-08-10-subagent-approval-pinned-never.zh.md`
- `.agents/notes/implemented/feature/2026-08-28-auto-review.zh.md`、`2026-09-24-auto-review-user-approval-fallback.zh.md`

**Claude Code**（官方文档）
- [permissions](https://code.claude.com/docs/en/permissions)：规则语法、Bash 匹配、「不再询问」的范围、和沙箱 / hook 的关系
- [permission-modes](https://code.claude.com/docs/en/permission-modes)：六种模式、受保护路径、关键路径、auto 模式分类器
- [sandboxing](https://code.claude.com/docs/en/sandboxing)：文件 / 网络隔离、auto-allow、逃生口、局限

**SimpleAgent**
- `src/simpleagent/permissions.py`：Policy、危险命令识别、WhitelistApprover
- `src/simpleagent/tools/registry.py:77-145`：判定和询问的接线
- `src/simpleagent/ui/approve.py`、`src/simpleagent/serve/approval.py`：终端和客户端审批器
- `src/simpleagent/mcp/tools.py:145`：MCP 工具的等级推导
- `src/simpleagent/agents/{base,claude,opencode}.py`：外部执行者的两档权限
- `docs/notes/M3-permissions-sessions.md`：M3 学习笔记
