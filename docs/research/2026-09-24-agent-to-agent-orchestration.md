# Agent-to-Agent 技术与 agent 软件编排调研（2026-09）

> 调研目的：盘清 agent 之间通信的协议有哪些、主流 agent 软件内部的多 agent 编排是怎么实现的，为 M8（`task` 工具、外部 agent、跨空间调度）的后续设计提供依据。
>
> 调研时间：2026-09-24。
>
> 信息来源可靠性说明：协议的方法名、字段名、状态机来自官方规范、proto 文件和官方文档，可信度高。**产品的具体工具名、配置项默认值、版本号、adoption 数字**部分来自第三方整理，变化很快，引用时请当参考而不是事实。可靠性分级见文末。

---

## 0. 三个核心结论

**结论一：「Agent to Agent」不是一个技术，而是按通信边界分成几层，每层各有一个事实标准。**
agent ↔ 远程 agent 用 **A2A**，agent ↔ 工具用 **MCP**，agent ↔ 编辑器 / 宿主程序用 **ACP**（Agent Client Protocol），agent ↔ 前端界面用 **AG-UI**。这些标准大多已经归到 Linux 基金会的 Agentic AI Foundation（AAIF）下。

**结论二：主流 agent 软件内部的多 agent 编排，基本都是自己写的，不走上面这些协议。**
常见做法是主 agent 把子 agent 当工具调用：子 agent 有自己的上下文，只把结论交回来。再复杂一点，会加一个共享任务表和一个邮箱。协议只用在对外的边界上（Gemini CLI 用 A2A 接远程子 agent、编辑器用 ACP 接 Claude Code / Codex）。

**结论三：业界经验收敛成一条——写操作只让一个 agent 做，其余 agent 只贡献「智力」。**
调研、评审、第二意见适合多 agent；让多个 agent 并行写代码的 swarm 做法，在实际使用中基本没跑通。

---

## 1. 协议分层

| 边界 | 协议 | 传输方式 | 核心概念 | 现状（2026-09） |
|---|---|---|---|---|
| agent ↔ 远程 agent | **A2A** | HTTP 上的 JSON-RPC、gRPC 或 REST；流式用 SSE；另有 webhook 推送 | Agent Card（能力描述）+ Task（任务） | 2026-03 发布 v1.0，150 多个组织支持，2026-08 加入 AAIF |
| agent ↔ 工具和数据 | **MCP** | stdio 或 Streamable HTTP | 工具 / 资源 / 提示词，外加 Tasks 扩展 | 2026-07-28 版改为无状态，Tasks 转正为扩展 |
| agent ↔ 编辑器或宿主 | **ACP**（Agent Client Protocol，Zed 发起） | stdio 上的 JSON-RPC，agent 作为子进程运行 | 会话 + 流式更新 + 反向权限请求 | 2026-01 上线注册表，已登记 50 多个 agent；JetBrains、Zed、Neovim、Emacs 支持 |
| agent ↔ 前端 | **AG-UI** | 一次 HTTP POST，然后 SSE 事件流 | 带类型的事件（从 RunStarted 到 RunFinished） | CopilotKit 主推 |
| 发现、寻址、身份 | AGNTCY（SLIM 消息、OASF 目录）、ANP（基于 DID） | — | 相当于「agent 的 DNS」 | 偏基础设施，实际采用少 |

注意重名：IBM 另有一个 ACP（Agent Communication Protocol），2025-08 已经并入 A2A，和 Zed 的 ACP 不是一回事。

---

## 2. A2A：真正意义上的 agent 间协议

A2A 的设计前提是对方 agent **不透明**：双方不共享记忆、工具和上下文，只交换消息和产物。

- **发现**：对方在 well-known 路径上放一张 Agent Card（JSON），写明名字、技能、端点、传输方式和认证方式。v1.0 起卡片可以签名，接收方能验证卡片确实来自那个域名。
- **工作单位是 Task**，它有一套状态：
  - 进行中：`SUBMITTED`、`WORKING`
  - 终态：`COMPLETED`、`FAILED`、`CANCELED`、`REJECTED`
  - 中断态：`INPUT_REQUIRED`（需要对方澄清）、`AUTH_REQUIRED`（需要凭证）
- **数据**：Message 用于对话，由若干 Part 组成（文本 / 文件 / 结构化数据）；Artifact 用于交付结果，两者刻意分开。`contextId` 把多轮对话串起来。
- **方法**：`SendMessage`、`SendStreamingMessage`、`GetTask`、`ListTasks`、`CancelTask`、`SubscribeToTask`，以及推送通知配置的增删查、`GetExtendedAgentCard`。
- **长任务拿结果有三种方式**：轮询 `GetTask`；SSE 流式接收状态和产物更新事件；webhook 推送。
- **v1.0 新增**：
  - 卡片签名
  - 多租户：一个端点可以挂多个 agent
  - 同一个 agent 可以同时暴露 JSON-RPC 和 gRPC
  - 版本协商：请求带 `A2A-Version` 头，不带时按 0.3 处理

一次完整交互大致是这样：

```
调用方 agent                                   远程 agent
GET /.well-known/agent-card.json      ────▶    返回技能、端点、认证方式
SendStreamingMessage(message)         ────▶    创建 Task
                 ◀──── SSE: WORKING
                 ◀──── SSE: INPUT_REQUIRED（需要澄清）
SendMessage(taskId, 补充信息)          ────▶
                 ◀──── SSE: 产物更新 …
                 ◀──── SSE: COMPLETED
```

**谁在用**：

- Google ADK 原生支持，可以把远程 A2A agent 当作子 agent。
- Gemini CLI 的「远程子 agent」直接走 A2A。
- AWS、Microsoft、Salesforce、SAP 等云平台已经接入。

2026-09 刚出了一篇对 A2A 的系统性安全分析（A2ABreak）。

### 2.1 SSE 流式能不能改已经发出的内容？

不只是流式展示。服务端可以修改已经发出的部分内容，但能不能改、怎么改，要看是哪种事件（字段定义见 `a2a.proto`）。

**任务状态（`TaskStatusUpdateEvent`）：只能覆盖。**
每条事件都带一份完整的新 `TaskStatus`（`state`、可选的 `message`、`timestamp`），客户端用最新一条替换旧的。这是整条覆盖，不是局部编辑。

**产物（`TaskArtifactUpdateEvent`）：可以追加，也可以整体替换。**
客户端靠 `artifact_id` 认出是同一个产物：

- `append = true`：新内容接在同一个产物后面，就是逐字输出的效果。
- `append = false`，且这个 id 已经存在：用新内容**整个替换**旧产物。
- `last_chunk = true`：这个产物写完了。

```
artifact-update  id=report  append=false  "# 草稿……"
artifact-update  id=report  append=true   "第二段……"          ← 追加
artifact-update  id=report  append=false  "# 终稿……"  last_chunk=true   ← 整体替换
status-update    state=COMPLETED
```

所以「先推草稿，最后换成终稿」是可以做到的，但替换的单位是整个产物，协议里没有「只改第 3 段」这种局部修改。
`a2a.proto` 只写了 `append = true` 的含义；`false` 时整体替换，是官方 Python SDK（`append_artifact_to_task`）的实现方式，规范正文没有明确写。

**消息（Message）：发出去就不能改。**
消息有 `messageId`，一旦发出就成为历史的一部分，协议没有编辑或撤回消息的操作。

**反方向完全改不了。** SSE 只从服务端推向客户端。客户端想补充信息，只能在对方处于 `INPUT_REQUIRED` 状态时再发一条新消息；想推翻重来，只能 `CancelTask` 后重新发起。

如果需要改已经显示在界面上的局部内容，AG-UI 更合适：它有 `STATE_DELTA`（用 JSON Patch 做局部修改）和 `MESSAGES_SNAPSHOT`（整体替换消息列表），本来就是为前端界面设计的。A2A 面向 agent 之间交付结果，所以修改只做到「整个产物替换」这一级。

---

## 3. MCP：agent 调 agent 最短的一条路

很多场景根本用不到 A2A，把下游 agent 包装成一个 MCP 工具就够了。2026-07-28 版让这种用法更顺手：

- **无状态核心**：去掉了 `initialize` 握手和 `Mcp-Session-Id`，每个请求在 `_meta` 里自带协议版本和能力。SimpleAgent 的 M5 已经跟进。
- **Tasks 扩展**（`io.modelcontextprotocol/tasks`）：`tools/call` 可以返回一个任务句柄，调用方再用 `tasks/get`、`tasks/update` 等方法轮询和驱动。「调一个要跑十分钟的 agent」需要的正是这种形状。
- **MRTR**（多轮往返请求）：工具执行到一半需要用户确认或补参数时，服务端返回 `resultType: "input_required"`，调用方带上 `inputResponses` 重新发起调用。这和 A2A 的 `INPUT_REQUIRED` 是同一个思路。
- **Sampling 被废弃**：服务端不能再借用调用方的模型。MCP 回到「工具协议」的定位，agent 之间的协作交给 A2A。

两个协议的 Task 越来越像。区别在于：MCP 里下游是**工具**，由调用方主导；A2A 里下游是**对等方**，有自己的身份和认证，可以进行多轮对话。

---

## 4. 进程内编排的六种模式

| 模式 | 机制 | 上下文 | 代表实现 |
|---|---|---|---|
| ① Agent 作为工具 | 调用一个工具，工具内部起一个新的 agent loop，跑完只把结论作为工具结果返回 | 子 agent 独立，父 agent 只看到结论 | Claude Code 的 Agent 工具、OpenAI 的 `Agent.as_tool()`、ADK 的 `AgentTool`、OpenCode 的 `task`、Deep Agents |
| ② Handoff（移交） | 暴露成 `transfer_to_<agent>` 工具，调用后对话控制权转给另一个 agent | 默认带上完整历史，可用 `input_filter` 裁剪 | OpenAI Agents SDK、LangGraph Swarm、MAF handoff |
| ③ 图 / 工作流 | 开发者用代码定义节点和边（顺序、并行、条件分支），agent 是其中的节点 | 共享一个 state 对象 | LangGraph StateGraph、MAF Workflows、ADK 2.0 图引擎、CrewAI Flows |
| ④ 管理者 + 账本或群聊 | 一个 manager 维护计划和进度账本，动态决定下一个由谁发言 | 共享同一段对话 | MAF 的 Magentic 和 Group Chat（源自 AutoGen） |
| ⑤ 任务板 + 邮箱 | 共享任务列表（带依赖，认领时加锁）+ 点对点消息 | 各自独立，靠消息同步 | Claude Code agent teams、Codex 的 `send_message` |
| ⑥ 模型原生编排 | 用强化学习训练一个会自己拆分并行子任务的 orchestrator | 子 agent 参数冻结，并行执行 | Kimi Agent Swarm（PARL 训练方法） |

有一篇源码研究论文分析了 11 个 coding agent（Claude Code、Codex、Gemini CLI、OpenCode、OpenHands、Aider 等），结论是**它们都没有引入通用 agent 框架，全部是手写的异步 loop**。LangGraph、MAF、ADK 这类框架主要用在企业应用里，coding agent 产品都自己写编排。

---

## 5. 主流 agent 软件的具体做法

### 5.1 Claude Code：三层机制，公开设计最细

1. **Subagents**：用 Agent 工具启动，有独立上下文，结果交回调用方。角色可以在 `.claude/agents/*.md` 里定义（工具、模型、提示词）。起了名字的子 agent 之后还能用 `SendMessage` 继续追问。
2. **Agent teams**（实验功能，`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`）：
   - 结构：一个 lead 加若干 teammate，每个都是完整的 Claude Code 实例。
   - 邮箱：每个 agent 一个 JSON 文件，路径是 `~/.claude/teams/{team}/inboxes/{agent}.json`。只有写入成功才算发出。
   - 任务表：放在 `~/.claude/tasks/{team}/`，有 pending / in progress / completed 三种状态，任务之间可以有依赖。认领时用**文件锁**防止两人抢同一个任务；上游完成后，下游自动解锁。
   - 状态同步：teammate 空闲时自动通知 lead，并附上最终答复。
   - 质量关卡：hooks 有 `TeammateIdle`、`TaskCreated`、`TaskCompleted`，脚本以 exit code 2 退出就能把工作打回。
   - 限制：权限请求统一冒泡到 lead 那里审批；不能嵌套团队；lead 身份固定不能转交。
3. **跨会话消息**，靠 `ListAgents` 和 `SendMessage` 两个工具：
   - **本机**：每个会话绑定一个 Unix domain socket，并在磁盘上登记自己；其他会话读这些登记文件来发现它。
   - **跨机器或云端**：消息经 Anthropic 服务器，借 Remote Control 连接送达。
   - **送达时机**：收到的消息在两次工具调用之间读入；如果接收方空闲，就唤醒它开始新的一轮。
   - **安全规则**：
     - **来自其他 agent 的消息不算用户授权**：不能批准权限请求，不能改配置，消息里的斜杠命令也不会执行。
     - 接收方可以设置接受、暂扣或拒收（accept / hold / refuse）。
     - 有限流、去重和队列上限，防止两个 agent 互发消息陷入死循环。

### 5.2 OpenAI Codex

- **Codex app**（2026-02 发布）定位为「agent 指挥中心」，多个 agent 各自在 worktree 或云端并行运行。
- **CLI 的多 agent 功能**：orchestrator 用 `spawn_agent`、`send_message`、`wait_agent`、`close_agent` 等工具管理子 agent。
  - 子 agent 按路径寻址，比如 `/root/researcher`。
  - 角色定义放在 `.codex/agents/<role>.md`。
  - 并发数和嵌套深度都有上限。
  - 子 agent 继承父 agent 的沙箱策略，不能提升权限。
- 以上工具名来自第三方整理的指南，不同版本之间可能有变化。

### 5.3 Gemini CLI

- 本地子 agent 定义在 `.gemini/agents/*.md`。
- **远程子 agent 直接走 A2A**，并保留 `contextId` 和 `taskId` 以支持多轮对话。这是少数真正把 A2A 用进命令行工具的例子。

### 5.4 OpenCode

- primary agent（build / plan）加 subagent（general / explore 等）。primary 用 `task` 工具委派，用户也可以 @ 提及子 agent。
- 2026-03 时，`task` 工具还只接受内置的子 agent 类型（issue #20059）。

### 5.5 Cursor 3.x

- Agents Window 统一显示本地、云端、worktree、SSH 上的所有 agent。
- `/worktree` 用来隔离改动；`/multitask` 把请求拆给一组异步子 agent。
- 编排的重心是**隔离执行环境加统一视图**，而不是 agent 之间对话。

### 5.6 Devin（Cognition）

2026-04 的更新文章总结了实际跑通的三种做法：

1. **写代码的 Devin + 上下文干净的 Devin Review**：评审方平均每个 PR 能找出约 2 个 bug，其中 58% 是严重问题。
2. **「smart friend」**：较弱的模型遇到难题时去问更强的模型。
3. **manager Devin 拆任务、派出 child Devins**：子 agent 通过内部 MCP 汇报进度。

失败的做法是并行写代码的 swarm：每个 agent 都会在代码风格和边界情况上做隐式决定，这些决定彼此冲突。

### 5.7 Kimi Agent Swarm

- K2.5 用 PARL 方法训练：一个可训练的 orchestrator，配合动态创建、参数冻结的子 agent。最多 100 个子 agent、1,500 步；K2.6 扩展到 300 个子 agent、4,000 步。
- 训练的主要难点叫 serial collapse：模型学着学着退回单 agent 串行执行。解决办法是分阶段设计奖励，前期鼓励并行，后期转向看任务成功率。
- 意义在于：**编排策略从代码搬进了模型权重**。

### 5.8 框架

- **OpenAI Agents SDK**：
  - 两种原语：handoff 和 `as_tool`。
  - 2026-04 加入 SandboxAgent，把控制面（agent loop、审批、状态）和执行面（沙箱）分开。
  - 和 Temporal 的集成在 2026-03 正式发布：agent loop 跑在 Workflow 里，每次模型调用是一个 Activity，进程重启不会丢进度。
- **Microsoft Agent Framework 1.0**（2026-04 正式发布，AutoGen 和 Semantic Kernel 的继任者）：内置顺序、并发、handoff、群聊、Magentic 五种编排方式，支持暂停等待人工审批。
- **LangGraph + Deep Agents**：LangGraph 是运行时（图 + 检查点），Deep Agents 是 harness（规划、文件系统、`task` 子 agent）。
- **Google ADK 2.0**：统一的图引擎，既支持确定性工作流，也支持模型主导的动态编排。子 agent 协作分三种模式：
  - chat：整段对话移交给子 agent。
  - task：子 agent 可以向用户澄清，完成后自动回到父 agent。
  - single-turn：并行执行，不和用户交互。

---

## 6. 收敛出来的规律

1. **同进程里的多 agent，本质上就是上下文隔离的函数调用。** 最大的收益是子 agent 替主 agent 消化大量中间 token，只交回结论；agent 之间的「对话」不是重点。
2. **读可以并行，写要单线程。** 评审、调研、第二意见适合多 agent；写代码要么只用一个 agent，要么用 worktree 或文件归属做隔离。
3. **本地通信用的介质都很朴素**：JSON 文件加文件锁、Unix socket、stdio 上的 JSON-RPC。本地场景用不到消息队列。
4. **信任边界要落在代码里，而不是写在提示词里。** 别的 agent 发来的消息是数据，不是授权；远程 agent 要验证签名卡片。
5. **成本很高。**
   - Anthropic 的调研系统里，多 agent 版本比单 agent 效果好 90.2%，但 token 用量约为普通对话的 15 倍。
   - Claude Code 文档建议从 3 到 5 个 teammate 起步。
6. **长时间运行的任务需要持久化执行**：Temporal、Restate、DBOS、Inngest 这类工具在企业框架里已经成了标配。

---

## 7. 对 SimpleAgent M8 的启发

1. **`task` 工具按「agent 作为工具」的形状做就对了**：新建一个 Session，给受限的工具集，只返回结论。Claude Code、OpenAI、ADK、OpenCode 的做法都是这样。现在跨空间调度「信息只经调度者中转」的星型结构（见 [design/command-dispatch.md](../design/command-dispatch.md)），也正好符合 Cognition 的经验。**不急着做 agent teams 那种 agent 之间直接互发消息的邮箱。**
2. **外部 agent 接不到审批的问题，ACP 有现成的解法。** [ROADMAP](../ROADMAP.md) 里写着，Claude Code 和 OpenCode 的无头模式接不到我们的审批卡，所以现在只有「只读」和「全放行」两档。
   - ACP 里 `session/request_permission` 是 agent 发给客户端的**必选**方法：agent 要执行工具时，反过来请求宿主审批。SimpleAgent 作为 ACP 客户端，可以直接把它接到自己的异步审批器上。
   - `session/update`（流式进度）、`session/cancel`（取消）、`session/load`（恢复会话）也正好对应 [ARCHITECTURE](../ARCHITECTURE.md#m2-起就要守住的接口约定) 里的几条接口约定。
   - OpenCode 和 Gemini CLI 原生支持 ACP；Claude Code 和 Codex 通过 Zed 维护的适配器接入（具体以 ACP 注册表为准）。
   - ARCHITECTURE 里本来写的是「以后再考虑 ACP」，调研下来**建议把这一步提前**。
3. **A2A 暂时不用。** 单用户本地使用，没有跨组织、认证、服务发现这些需求。以后如果想让别的 agent 调用 `sa daemon`，再加一个 A2A 服务端点就行。它的 `INPUT_REQUIRED` / `AUTH_REQUIRED` 两个中断状态，可以先借鉴到无人值守任务里。
4. **子 agent 和外部 agent 的输出进入上下文时要标明来源，审批只认用户本人**，和 Claude Code 的规则一致。

---

## 附：本报告的可靠性分级

| 结论 | 置信度 | 依据 |
|---|---|---|
| 协议分层（A2A / MCP / ACP / AG-UI） | 高 | 各协议官方规范，多个独立来源一致 |
| A2A 方法名、Task 状态机、`append` / `last_chunk` 字段 | 高 | 官方规范和 `a2a.proto` 原文 |
| `append = false` 时整体替换产物 | 中高 | 官方 Python SDK 的实现，规范正文未明确写 |
| MCP 2026-07-28 版改动 | 高 | MCP 官方博客 |
| ACP 的 `session/request_permission` 是必选方法 | 高 | ACP 官方协议概览 |
| Claude Code subagents / agent teams / 跨会话消息的机制 | 高 | 官方文档原文 |
| Codex CLI 多 agent 的工具名、配置项和默认值 | 中低 | 第三方整理的指南，版本间有出入 |
| 「写单线程，读并行」的经验 | 中高 | Cognition 实践总结，与 Claude Code 文档的建议一致 |
| 11 个 coding agent 都不用通用框架 | 中 | 单篇论文，但和各产品的开源代码现状一致 |
| 具体 adoption 数字、版本发布日期 | 中 | 厂商新闻稿和二手汇总，仅作量级参考 |

---

## 附：来源

- A2A：[v1.0 发布与 2026 博客存档](https://a2a-protocol.org/latest/blog/archive/2026/) · [A2A 规范](https://a2a-protocol.org/latest/specification/) · [a2a.proto](https://github.com/a2aproject/A2A/blob/main/specification/a2a.proto) · [官方 Python SDK](https://github.com/a2aproject/a2a-python) · [LF：A2A 超过 150 个组织](https://www.linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year) · [ACP（IBM）并入 A2A](https://lfaidata.foundation/communityblog/2025/08/29/acp-joins-forces-with-a2a-under-the-linux-foundations-lf-ai-data/) · [A2ABreak 安全分析](https://arxiv.org/pdf/2609.10871)
- MCP：[2026-07-28 规范](https://blog.modelcontextprotocol.io/posts/2026-07-28/) · [无状态改动解读](https://flaviocopes.com/mcp-2026-07-28-stateless/) · [AAIF 成立](https://www.linuxfoundation.org/press/linux-foundation-announces-the-formation-of-the-agentic-ai-foundation)
- ACP / AG-UI / AGNTCY：[ACP 协议概览](https://agentclientprotocol.com/protocol/overview) · [ACP Registry](https://zed.dev/blog/acp-registry) · [AG-UI 文档](https://docs.ag-ui.com/) · [AGNTCY 加入 LF](https://www.linuxfoundation.org/press/linux-foundation-welcomes-the-agntcy-project-to-standardize-open-multi-agent-system-infrastructure-and-break-down-ai-agent-silos)
- Claude Code：[Agent teams](https://code.claude.com/docs/en/agent-teams) · [跨会话消息](https://code.claude.com/docs/en/cross-session-messaging) · [Anthropic 多 agent 调研系统](https://www.anthropic.com/engineering/multi-agent-research-system)
- Codex / Gemini CLI / OpenCode / Cursor：[Codex CLI 多 agent 指南（第三方）](https://codex.danielvaughan.com/2026/04/11/codex-cli-multi-agent-orchestration-v2-complete-guide/) · [Codex app](https://intuitionlabs.ai/articles/openai-codex-app-ai-coding-agents) · [Gemini CLI 远程子 agent](https://geminicli.com/docs/core/remote-agents/) · [OpenCode Agents](https://opencode.ai/docs/agents/) · [OpenCode issue #20059](https://github.com/anomalyco/opencode/issues/20059) · [Cursor 多 agent](https://cursor.com/help/ai-features/multi-agent)
- Devin / Kimi：[Cognition: Multi-Agents: What's Actually Working](https://cognition.com/blog/multi-agents-working) · [Don't Build Multi-Agents](https://cognition.com/blog/dont-build-multi-agents) · [Kimi K2.5 技术博客](https://www.kimi.com/blog/kimi-k2-5.html)
- 框架：[OpenAI Agents SDK Handoffs](https://openai.github.io/openai-agents-python/handoffs/) · [Agents SDK 沙箱更新](https://devops.com/openai-upgrades-its-agents-sdk-with-sandboxing-and-a-new-model-harness/) · [Temporal × Agents SDK 正式发布](https://temporal.io/blog/announcing-openai-agents-sdk-integration) · [MAF 编排模式](https://learn.microsoft.com/en-us/agent-framework/workflows/orchestrations/) · [MAF 1.0](https://techcommunity.microsoft.com/blog/azuredevcommunityblog/the-future-of-agentic-ai-inside-microsoft-agent-framework-1-0/4510698) · [Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview) · [ADK Go 2.0](https://developers.googleblog.com/announcing-adk-go-20/) · [ADK 多 agent 模式](https://developers.googleblog.com/developers-guide-to-multi-agent-patterns-in-adk/)
- 源码研究：[Harness Engineering：11 个 coding agent 的源码研究](https://arxiv.org/abs/2609.00006)
