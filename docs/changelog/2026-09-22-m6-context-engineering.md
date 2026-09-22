# M6 完成：上下文工程（预算、清理旧工具结果、摘要压缩、`/compact`、超长兜底）

- 日期：2026-09-22
- 对比基线：`3ed5343`（Merge pull request #6，M5 完成）
- 对应里程碑：M6

## 功能变化

- 新增：token 预算。下一次请求大概会发多少 token = 上次请求的实际用量（`usage.prompt_tokens`）+
  之后新增消息的字符估算；再用一个校准系数（实际用量 ÷ 同一段的字符估算）修正「实际用量作废后」
  的全量估算。REPL 新增 `/context` 显示总量、精确/估算各占多少、构成、估算误差。
- 新增：第二级策略——清理旧工具结果。占用超过 `clear_at`（默认 60%）时，把较早的工具结果原文
  换成占位符，原文落盘到 `tool_outputs/cleared-<内容摘要>.txt`；不清最近几个、模型还没看过的、
  已清过的、太短的，以及模型主动读回过的（避免清理—读回—再清理的拉锯）；一次触发就清完、能省下
  的不到输入上限的 5% 就不清，为的是少打破前缀缓存。
- 新增：第三级策略——LLM 摘要压缩。占用超过 `compact_at`（默认 80%，按清完之后的样子算）时，
  调模型把早期对话压成摘要，替换原文，保留最近约 1/4。切点只落在 user 消息或紧跟工具结果的
  assistant 消息上，不拆开 tool_call 和它的结果。摘要请求复用上一次请求的前缀以命中缓存：
  `LLM.stream` / `build_request` / `prepare_messages` 新增 `continue_turn` 参数，让摘要请求的
  「压缩指令」不被当成新的一轮，从而不打断思考内容回传规则、不打断前缀缓存。同一次请求里清理和
  压缩都要做时，先压缩（复用原历史前缀）再清理，避免白清。
- 新增：REPL `/compact [重点]`，切在最后一条 user 消息上，完整保留最后一轮；可以附带说明要保留的重点。
- 新增：上下文超长报错兜底。服务端返回 400/413 且报错信息像是「上下文超长」时（各家措辞不同，按
  关键字认），且这一步还没输出任何内容，强制压缩一次（只留最后一步）后重试；每步只重试一次。
- 新增：事件 `ContextEdited`。清理/压缩发生时产出，REPL 和 `sa run` 显示一行提示；工作台转成
  `context_edited` 帧。
- 升级：工作台（`sa serve`）内置 agent 的会话历史拆成两份——`sessions/<sid>.jsonl` 仍是展示用、
  按事件镜像的历史；新增 `sessions/model/<sid>.jsonl`，走 `SessionStore` 的读写方法，记录清理、
  压缩、中断修复等只有 Session 方法才产生的改动。老会话第一次被用到时自动从展示用的 jsonl 迁移，
  并补上迁移前遗留的悬空 `tool_call`（没有对应结果的工具调用）。
- 修复：REPL 的 `/clear` 之前直接清空内存里的消息列表，没写进 JSONL，`--resume` 后清掉的历史又
  会回来；改成走 `Session.truncate(0)`。
- 修复：工作台里中断正在执行的工具后，留下没有结果的 `tool_call`，下一条输入会被 API 直接拒绝。
  根因是工作台每次从按事件镜像的 jsonl 重建历史，`Agent._repair` 补的中断结果不产生事件、没落盘；
  拆成两份历史后，修复走 Session 的方法随手落盘。已经坏掉的老会话在迁移时由 `fill_missing_results` 补齐。

## 函数级改动

### `src/simpleagent/agent/context.py`（新文件）

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `estimate_text()` | 新增 | 按字符估 token：ASCII 约 3 字符 1 token，非 ASCII 1 字 1 token，宁可高估 |
| `estimate_message()` / `estimate_messages()` | 新增 | 单条 / 多条消息的字符估算，含 `tool_calls`、思考内容等字段 |
| `estimate_tools()` | 新增 | 工具 schema 列表按 JSON 估算 |
| `output_reserve()` | 新增 | 给模型输出留的余量：`max_tokens` 或 `min(16000, context_window/4)` |
| `ContextAnchor` | 新增 dataclass | 上次请求的实际用量：`prompt_tokens`、覆盖到第几条消息、模型名、当时的字符估算 |
| `Calibration` / `Calibration.of()` | 新增 dataclass | 校准系数 = 实际用量 ÷ 字符估算，限制在 0.5～2 之间 |
| `ContextUsage` | 新增 dataclass | 预算结果：token 数、窗口、余量、精确/估算部分、校准系数；`limit`/`ratio` 属性 |
| `measure()` | 新增 | 综合预算：有 anchor 就「实际 + 之后新增部分估算」，没有就全量估算（可乘校准系数） |
| `breakdown()` | 新增 | 按 system/tools/user/assistant/tool 拆分字符估算，给 `/context` 用 |
| `select_clearable()` | 新增 | 挑出可以清理的工具结果下标和工具名，排除最近、未读、已清、太短、被读回过的 |
| `cleared_result()` | 新增 | 生成清理后的占位符消息（保留 `tool_call_id`） |
| `save_cleared()` | 新增 | 原文按内容哈希落盘到 `cleared-<摘要>.txt`，同内容同路径，便于命中缓存和防误删 |
| `_head_len()` | 新增 | 识别历史开头是否已经是上一次压缩留下的摘要（及其 ack） |
| `find_cut()` | 新增 | 自动压缩的切点：只落在 user 或紧跟工具结果的 assistant 上，按 `keep_tokens` 倒着找 |
| `last_turn_cut()` | 新增 | `/compact` 的切点：最后一条 user 消息，保留完整最后一轮 |
| `compact_prompt()` | 新增 | 生成追加在历史末尾的压缩指令文本，可带用户交代的重点 |
| `compacted_head()` | 新增 | 生成替换掉前 N 条的摘要消息（必要时补一条固定 assistant 回复保持交替） |
| `is_context_overflow()` | 新增 | 按状态码 400/413 + 关键字判断异常是不是「上下文超长」 |

### `src/simpleagent/agent/loop.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `CompactError` | 新增 | 摘要请求失败（API 报错/模型没给摘要）时抛出，历史不动 |
| `fill_missing_results()` | 新增 | 给悬空 `tool_call` 补一条「执行被中断」的结果，供工作台迁移旧会话用 |
| `Agent.__init__()` | 修改 | 新增 `context: ContextConfig | None` 参数 |
| `Agent.context_usage()` | 新增 | 对外暴露当前会话的预算（`measure` 的封装） |
| `Agent._estimate()` | 新增 | 全量字符估算（不用实际用量、不校准），用于算校准系数 |
| `Agent._factor()` | 新增 | 当前模型的校准系数，没有则为 1 |
| `Agent.context_breakdown()` | 新增 | 对外暴露上下文构成（`breakdown` 的封装），供 `/context` 用 |
| `Agent.run()` | 修改 | 每次请求前先判断是否需要压缩/清理并执行；记录本次请求覆盖的消息数和实际用量（`mark_sent`）；捕获上下文超长异常，强制压缩一次后重试一次 |
| `Agent.compact()` | 新增 | 执行一次摘要压缩：找切点、发摘要请求（复用前缀）、写回 `Session.compact` |
| `Agent._continues_turn()` | 新增（静态方法） | 判断摘要请求要不要把压缩指令当作本轮延续，以匹配上一次请求的思考内容回传规则 |
| `Agent._compact_in_turn()` | 新增 | 一轮进行中触发压缩的公共逻辑（自动触发、超长兜底共用），失败转成带 `error` 的事件 |
| `Agent._needs_compact()` | 新增 | 按「清理之后的样子」判断是否需要压缩，避免清理已够用时白跑一次摘要请求 |
| `Agent._clear_plan()` | 新增 | 计算这次该清哪些工具结果、大约能省多少 token，收益不够不清 |
| `Agent._clear_tool_results()` | 新增 | 执行一次清理：占位符 + 原文落盘，写回 `Session.replace` |

### `src/simpleagent/agent/session.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Session.context_anchor` / `Session.context_calibration` | 新增字段 | 上次请求实际用量与校准系数，仅存内存，不落盘 |
| `Session.truncate()` | 修改 | 删到实际用量覆盖范围之内时作废 `context_anchor` |
| `Session.replace()` | 新增 | 原地替换若干条消息（清理旧工具结果用），一条 `replace` 记录写盘，全部生效或都不生效 |
| `Session.compact()` | 新增 | 前 `cut` 条消息换成摘要 `head`，一条 `compact` 记录写盘，作废 `context_anchor` |
| `Session.mark_sent()` | 新增 | 记录上次请求的实际用量，生成/更新 `ContextAnchor` 和 `Calibration` |
| `SessionStore.load()` | 修改 | 重放 JSONL 时新增对 `replace`、`compact` 两种记录的处理 |

### `src/simpleagent/config.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `ContextConfig` | 新增 | `[context]` 配置段：`clear_at`（默认 0.6）、`keep_tool_results`（默认 3）、`compact_at`（默认 0.8） |
| `Config.context` | 新增字段 | 挂载 `ContextConfig`，默认值生效，无需用户手动配置 |

### `src/simpleagent/events.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `ContextEdited` | 新增 dataclass | 清理/压缩事件：种类、腾地方前后的 token 数与上限、条数、摘要请求用量、失败原因；`summary()` 生成一行文字 |
| `Event` | 修改 | 联合类型加入 `ContextEdited` |

### `src/simpleagent/llm/client.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `LLM.stream()`（协议）/ `LLMClient.stream()` / `LLMClient.build_request()` | 修改 | 新增 `continue_turn: bool = False` 参数并透传 |
| `prepare_messages()` | 修改 | 新增 `continue_turn` 参数：为真时最后一条 user 不算新一轮，思考内容按它之前那一轮的回传起点算，让摘要请求命中前缀缓存 |

### `src/simpleagent/llm/fake.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `FakeLLM.stream()` | 修改 | 新增并记录 `continue_turn` 参数，供测试断言 |

### `src/simpleagent/serve/frames.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `event_to_frame()` | 修改 | 新增对 `ContextEdited` 的分支，转成 `context_edited` 帧（含 `summary` 文本） |

### `src/simpleagent/serve/runner.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Runner._build_agent()` | 修改 | 构造 `Agent` 时传入 `context=self.config.context` |
| `Runner._run_input()` | 修改 | 读取会话历史时，内置 agent（`space.executor == "simpleagent"`）改用 `store.load_model_session()`，外部 CLI 执行者仍用 `load_session()` |

### `src/simpleagent/spaces/store.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `SpaceStore.model_sessions()` | 新增 | 返回指向 `sessions/model/` 目录的 `SessionStore` |
| `SpaceStore.load_model_session()` | 新增 | 加载「模型看到的历史」；不存在时从展示用 jsonl 迁移，调用 `fill_missing_results` 补悬空 `tool_call`，用量从旧 meta 接续 |

### `src/simpleagent/ui/headless.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Headless.__init__()` | 修改 | 构造 `Agent` 时传入 `context=config.context` |
| `Headless._run()` | 修改 | 事件处理循环新增对 `ContextEdited` 的分支，输出 `[事件摘要]` |

### `src/simpleagent/ui/repl.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `format_context()` | 新增 | 生成 `/context` 的多行输出：总量/上限、精确与估算构成、误差、校准信息 |
| `Repl.__init__()` | 修改 | 构造 `Agent` 时传入 `context=config.context` |
| `Repl.chat()` | 修改 | 事件处理新增对 `ContextEdited` 的分支，打印一行提示 |
| `Repl.command()` 里 `/clear` 分支 | 修改 | 改为调用 `self.session.truncate(0)`，确保写入 JSONL |
| `Repl.command()` 里新增 `/context`、`/compact` 分支 | 新增 | `/context` 打印预算信息；`/compact` 调用 `self._compact()` |
| `Repl._compact()` | 新增 | 手动压缩：用 `last_turn_cut` 找切点，调用 `Agent.compact()`，失败打印错误 |

## 配置与依赖

- 新增配置段 `[context]`：`clear_at = 0.6`、`keep_tool_results = 3`、`compact_at = 0.8`；
  `config.example.toml` 里已加注释示例。**不需要手动改配置**，默认值直接生效；老的
  `~/.simpleagent/config.toml` 不加这一段也能正常运行。
- `config.example.toml` 的 `[profiles.local]`（Ollama）新增一行提醒：`context_window` 要和
  Ollama 实际的 `num_ctx` 一致，否则本地模型的清理/压缩预算会算错（Ollama 超过 `num_ctx` 是
  悄悄截断而不是报错）。用本地 Ollama 的话，建议对照检查一下自己 `config.toml` 里的这个值。
- 数据目录新增：`spaces/<space-id>/sessions/model/<sid>.jsonl`（工作台内置 agent 的模型历史，
  首次使用对应会话时自动生成）、`tool_outputs/cleared-<摘要>.txt`（清理旧工具结果时落盘的原文）。
- 会话 JSONL 格式新增两种操作记录：`{"op": "replace", ...}`、`{"op": "compact", ...}`。旧版本
  代码会忽略这两种记录，恢复出来的是清理 / 压缩之前的原文（可能超长）；新版本兼容旧文件。
- 没有新增第三方依赖（没有改 `pyproject.toml` / `uv.lock`）。

## 测试

- 新增 `tests/test_context.py`（53 个测试函数，参数化后 54 个用例）：覆盖预算与估算、校准系数、清理挑选规则与落盘、
  摘要压缩的切点/指令/请求内容、前缀缓存相关的回归（system prompt/工具列表稳定、
  `continue_turn`、摘要请求前缀一致）、`/compact` 的切点、超长兜底重试与放弃、`fill_missing_results`、
  中断时压缩后历史仍合法等。
- 新增 `tests/serve/test_model_history.py`（3 个测试）：工作台里中断后带悬空 `tool_call` 继续输入、
  清理结果跨输入保留（不重复清理）、旧会话历史迁移并修复悬空 `tool_call`。
- 修改 `tests/serve/test_serve.py`：新增 1 个测试，`ContextEdited` 转 `context_edited` 帧的字段正确性。
- 修改 `tests/test_headless.py`：新增 1 个测试，`sa run` 触发清理时打印提示行。
- 修改 `tests/test_repl.py`：新增 8 个测试，覆盖 `/clear` 落盘、`/context` 输出、清理/压缩提示、
  `/compact`（含保留最后一轮工具调用的情况）、压缩失败提示等。
- 测试结果：`uv run pytest -q` 584 passed。`tests/serve/test_web.py::test_pinned_session_stays_on_top`
  是既有的偶发失败：在没有改动的 `origin/main` 上单独重复跑 30 次失败 4 次，和 M6 无关。原因大概率是
  同一毫秒内创建的几个会话 `updated_at` 相同，排序退回按随机 id，本次未修，另开任务处理。
- 关键测试做过改坏验证：去掉 `turn_start` 调整、切点允许落在 tool 上、`continue_turn` 写死、去掉校准系数、
  先清后压、工作台改回旧读法，对应测试都会失败。
- `uv run ruff check`：All checks passed。
- `uv run ruff format --check`：138 files already formatted。

## 相关笔记

- `docs/notes/M6-context-engineering.md`：预算与校准的取舍、三级策略细节、和前缀缓存的博弈
  （思考内容回传规则、先压后清）、工作台两份历史的设计、真实模型（deepseek-flash）实测数据
  （摘要请求缓存命中从 30%～58% 修到 95%～97% 等）、踩过的坑与现在的已知缺口。
