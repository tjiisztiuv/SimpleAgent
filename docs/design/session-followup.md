# 追问已有的会话：调度者 followup + 卡片追问 + 会话互斥

> 状态：已实现（2026-09-24）。这份文档包括方案、改动记录和验证结果。变更记录见 [changelog/2026-09-24-command-session-followup.md](../changelog/2026-09-24-command-session-followup.md)。
> 接着 [command-dispatch.md](command-dispatch.md) 做：那边的 dispatch 每次都新开子会话，这里补上「接着问已有的会话」。
> 调研依据见 [research/2026-09-24-agent-to-agent-orchestration.md](../research/2026-09-24-agent-to-agent-orchestration.md) 第 7 节。

## 1. 要解决什么

`dispatch` 每次都新建子会话，两个后果：

- 你说「让 finance 把刚才那份报告再改一下」，调度者只能新开一个会话从头做，之前读过的文件、做过的决定全丢了。
- 指挥台每说一句话都新开一个调度会话，新的调度者不知道以前派过什么。

底层其实早就能续接：内置执行者的历史在 jsonl 里，外部 CLI 记了 `agent_session_id`，下次带 `--resume` / `--session`。
缺的是三样：让调度者**找到**以前的会话、往已有会话里**再发一句**的入口、后端**防止两方同时往一个会话发**。

## 2. 和你确认过的口径

| 问题 | 结论 |
|---|---|
| 调度者能追问哪些会话 | **打开着的空间里的近期会话**，不只是自己派出去的：以前的调度会话派的、`@空间名` 直接下发的、你手动开的都行。指挥台自己的会话不行 |
| 有没有不经过调度者的入口 | 有：指挥台**卡片上的「追问」按钮**，下一句直接发给那个会话 |
| 调度卡要不要也能追问 | 要：在调度卡上追问 = 接着和那次的调度者聊，它记得自己派过什么 |
| 卡片挂在哪张调度卡下面 | 新增 `dispatched_by`（最近一次让它跑的调度者），`parent_session_id` 只记出身、不变 |
| 计划卡里要不要标「这一步追问哪个会话」 | 这次不做：计划按空间校验已经够用 |

## 3. 架构

```
指挥台输入
 ├─ 普通一句话 → 新调度会话
 │     ├─ recent_sessions(space?, limit)  查打开的空间里最近的会话（只读）
 │     ├─ followup(session, message)      在已有会话里接着问，等这一轮跑完
 │     ├─ dispatch(space, task)           新建子会话（不变）
 │     └─ propose_plan(steps)             跨空间先确认（followup 同样受约束）
 ├─ @空间名 任务 → 直接新建会话（不变）
 └─ 卡片「追问」→ 输入框进入追问模式，下一句直接 POST 到那个会话，不经过调度者
```

几个取舍：

- **查会话用工具，不塞进 system prompt**。最近会话列表每次请求都带上要多一两千 token，大多数调度用不上。和 M7 技能一样按需取。
- **追问单独一个工具，不给 dispatch 加 session 参数**。一个按空间派、一个按会话追问，参数语义不同，分开模型更不容易用错。
  两个工具共用同一道关卡 `admit()`（跨空间要计划、计划外不派、同空间不并发），规则只写一份。
- **追问的结果只算这一轮**。追问前记下已有几条消息，只对之后的消息做摘要、取回复。
  不然这一轮没给回复时，会把上一轮的旧回复当成结论交回去。
- **还是星型结构**：追问是「调度者 → 会话」，会话之间不直接说话（调研结论：写操作单线程，额外的 agent 只贡献智力）。

### 3.1 会话互斥：同一个会话同一时间只跑一轮

以前 `POST /api/sessions/{id}/input` 不检查会话是不是在跑，只靠前端把输入框置灰；两个请求撞上，
同一个会话会同时跑两轮，消息交错写进 jsonl。调度者能追问之后，你和调度者可能同时往一个会话里发，必须在后端挡住。

- `Runner.claim(session_id) -> token | None`：加锁的「占用」表（HTTP 线程和事件循环线程都会调）。
  不用现有的 `_tasks`：它要等 `_run_input` 真正开始执行、还要先等 MCP 启动完才登记，中间有空档，两个请求能同时过检查。
- 谁占：`run_input()`（HTTP）调度前先占，占不到抛 `SessionBusy` → 409；`run_child()`（调度者）开跑前再占一次，
  占不到抛 `ToolError` 给模型（工具查过没在跑，到真正开跑之间，你可能刚在界面上发了一句）。
- 谁放：
  - `_finalize` 在**广播终态帧之前**放。客户端收到「完成」马上再发一句，不能撞上还没放开的占用。
    这时占用一定还是这一轮自己的；从这里到 `_run_turn` 退出都没有 `await`，下一轮要等这段同步代码跑完才开始。
  - 执行者被锁住的提前返回：报错之前放。
  - `_run_input` 的 `finally` 兜底，**带 token 只放自己那一份**：收口时已经放过、被别人占上的，不能误放。

## 4. 怎么用

1. **让调度者追问**：在指挥台直接说「刚才 work 那份报告，改成按周汇总」。调度者会先 `recent_sessions` 找到会话，
   再 `followup`。找不到或者有好几个都像，它会列出候选问你。
2. **卡片上追问**：指挥台里跑完的卡片（调度卡、子任务卡都有）下面有「追问」按钮。点了之后输入框上方出现
   「追问 → work · 9 月开销报告 ✕」，回车直接发给那个会话；✕ 或 Esc 退出。会话正在跑、被锁住时，原因显示在输入框下面，原话留在输入框里。
3. **找旧会话的卡片**：只输 `@空间名`（不带任务）会出一张这个空间最近会话的状态卡，点它的「追问」就能接着问。

**需要你手动做的**：没有。

## 5. 改动记录

### 后端

| 位置 | 改动 |
|---|---|
| `spaces/models.py` | `SessionMeta.dispatched_by`：最近一次让它跑的调度会话，派发、追问都会更新；老数据缺省 `None` |
| `spaces/store.py` | `update_meta` 白名单加 `dispatched_by` |
| `serve/runner.py` | `SessionBusy`；`claim()` / `_release(session_id, token)` / `session_busy()`；`run_input()` 先占再调度；`_run_input()` 拆成薄包装 + `_run_turn()`（原来的主体）；`_finalize()` 在终态帧之前放开；`sessions()` / `find_session()` / `_brief()` 实现 Dispatcher 的新方法（先按 meta 排序截断，最后几条才读 jsonl 取最后一句）；`run_child(..., session_id=None)` 支持追问：占用、记下起点、更新 `dispatched_by`、只统计这一轮 |
| `serve/app.py` | `/input`、`/rerun` 捕获 `SessionBusy` 返回 409；面板 summary 的条目带 `dispatched_by`；会话 summary 带 `dispatched_by`、`locked` |
| `command/tools.py` | `SessionBrief`（带 `render()`，「3 分钟前」「调度派发 / 直接发起」「正在跑（现在不能追问）」「已锁定」）；`Dispatcher` 加 `sessions()`、`find_session()`，`run_child` 加 `session_id`；关卡抽成 `admit()`；新工具 `recent_sessions`（只读，limit 上限 30）和 `followup`（依次查：存在 → 不是调度会话 → 空间开着 → 没锁 → 没在跑 → `admit()`）；`ChildResult.followup` 渲染成「完成（追问）」 |
| `command/prompt.py` | 工具从两个变四个；新增「追问已有的会话」一节 |

### 前端

| 位置 | 改动 |
|---|---|
| `renderDispatch()` | 跑完、没在等审批、没被锁的卡片显示「追问」；正在追问的卡片描边 |
| `setReplyTo()` / `clearReplyTo()` / `renderReplyTo()` / `sendReply()` | 追问模式：输入框上方的标签、占位符、Esc 退出；发送直接 POST 到会话，409 时显示原因、原话留在输入框 |
| `restoreDispatch()` | 已有卡片按后台的 `updated_at` 刷新：变了就说明又跑过一轮，换到 `dispatched_by` 那张调度卡下面、刷新状态和最后一句。**只看「在不在 running 列表里」不够**：浏览器实测时追问在两次轮询（3 秒）之间就跑完了，卡片一直停在上一轮 |
| `cardParent()` | 挂在哪张调度卡下：`dispatched_by`，没有就看 `parent_session_id` |
| `pollDispatch()` | 轮询摘要时顺带更新挂靠 |
| `index.html` / `styles.css` | `#dispatch-reply`；`.dispatch.replying`、`.reply-chip` |

## 6. 验证

**自动测试**：`uv run pytest` 734 个全部通过（改动前 718 个，新增 16 个）；`ruff format`、`ruff check` 通过。

| 文件 | 覆盖 |
|---|---|
| `tests/test_command_tools.py`（+10） | 追问已有会话不新建；追问也受跨空间关卡约束（先派 A 再追问 B、同一批追问 A 又派 B）；同一批 dispatch + followup 同一空间被拒；批准的计划里并行追问两个空间；不存在 / 调度会话 / 空间已关 / 被锁 / 正在跑五种拒绝各自给出原因；开跑时被抢占后空间不再算「在跑」；`recent_sessions` 的渲染、按空间过滤、limit 上限、空列表；`SessionBrief.render()` 的相对时间和状态文案；`ChildResult` 的追问标记 |
| `tests/serve/test_command.py`（+4） | Runner + FakeLLM 端到端：调度会话 A 派出子会话，新调度会话 B 查到并追问它，还是同一个会话、子会话第二轮带着第一轮历史、B 只拿到第二轮回复、`dispatched_by` 变成 B；取消调度者时追问的这一轮跟着取消；会话已被占时 `run_child` 报错且不改 meta；`sessions()` 的排序、过滤、busy / locked 标记和 `find_session()` |
| `tests/serve/test_serve.py`（+2） | 会话被占时 `/input`、`/rerun` 返回 409 且不落消息；正常完成（收到 done 帧立刻再发必须成功）、取消、起不来、执行者被锁四种退出路径都会放开占用 |

**手动验证**（浏览器）：临时 `SIMPLEAGENT_HOME` + 按规则应答的假模型（调度者：句子里有「接着 / 改」就 `recent_sessions` → `followup`，否则 `dispatch`），不联网，不碰 `~/.simpleagent*`：

- 指挥台说「写一份 9 月开销报告」：调度卡 + 子任务卡，两张都有「追问」；
- 新说一句「接着把报告改成按周汇总」：新的调度会话查到并追问同一个子会话（汇总里是「[work] 完成（追问）· 子会话 同一个 id」），
  work 空间没有多出会话；子任务卡挂到新调度卡下面，显示第 2 轮的回复（这一步发现了上面 `restoreDispatch` 的问题，修完后刷新页面、不刷新两种情况都对）；
- 子任务卡点「追问」：输入框上方出现「追问 → work · 写一份 9 月开销报告 ✕」，卡片描边；回车后卡片变运行中，跑完显示第 4 轮；Esc 退出追问模式；
- 调度卡点「追问」：接着那次调度会话聊，它再次追问同一个子会话，没有新建调度会话；
- 会话正在跑时连发两次：第一次 202、第二次 409「这个会话正在跑，等它这一轮结束再发」；追问模式下撞上 409：原因显示在输入框下面，原话还在，追问模式不退出；
- 服务端日志没有报错，浏览器控制台只有那次预期的 409。

**没做的**：没有接真实模型实测。真实模型能不能在「刚才那个」这类说法下先 `recent_sessions` 再 `followup`、
有歧义时会不会问你，要用 `uv run sa serve`（开发模式，端口 8385）加真实 key 试一下。

## 7. 已知限制和后续可以做的

- **只防同一个会话并发**：同一个空间里两个**不同**会话同时跑（你手动开一个、调度者追问另一个）还是允许的，和以前的 dispatch 一样。
- **`recent_sessions` 每个空间只看最近 N 个**（N = limit）：更老的会话列不出来，但调度者手里有 id（比如以前派发结果里的）时 `followup` 照样能找到。
- **还没开跑就取消是空操作**（以前就有）：`cancel()` 要等 `_run_turn` 造好 Agent 或起了子进程才找得到对象。界面上「停止」按钮要收到 running 帧才可点，一般碰不到。
- **调度卡运行中那一行**显示的是上一轮的汇总（会话摘要按整个会话算），跑完才换成这一轮的。
- **追问的话在子会话里显示成用户消息**，和 dispatch 的任务描述一样，看不出是调度者转述的。
- 计划卡不显示「这一步追问哪个会话」，只显示空间。
