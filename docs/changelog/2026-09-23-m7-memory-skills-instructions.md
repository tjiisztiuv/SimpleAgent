# M7 完成：项目指令（AGENTS.md）+ 长期记忆（memory/）+ 技能（SKILL.md）

- 日期：2026-09-23
- 对比基线：`8cfd090`（重打 v0.2.0：发布日志补全 v0.1.0 以来的全部改动）
- 对应里程碑：M7

## 功能变化

- 新增：新包 `src/simpleagent/knowledge/`，会话开始时读三样「会话之外」的知识，统一由
  `Knowledge.load(config, cwd)` 拼进 system prompt 和工具列表：
  - **项目指令**：自动读 `AGENTS.md`（个人一份放数据目录，项目从 git 根目录一路到 cwd 每层找一份，
    越具体越靠后）；某层没有 `AGENTS.md` 就退回读 `CLAUDE.md`；总字数超过上限的部分截断，并告诉模型
    用 `read_file` 读原文。
  - **长期记忆**：`memory/` 目录下每条记忆一个 Markdown 文件，`MEMORY.md` 是索引（会话开始时索引进
    system prompt，正文按需用 `memory_read` 读）；新增 `memory_read` / `memory_write` / `memory_delete`
    三个工具；写和删默认要终端确认。
  - **技能**：按 `SKILL.md` 开放标准（agentskills.io，和 Claude Code 的技能目录兼容）发现技能，
    system prompt 里只放名字和一行描述，模型判断任务对得上再用新增的 `load_skill` 工具读正文；
    个人技能（数据目录 `skills/`）优先于项目里配置的技能目录（默认 `.agents/skills/`、`.claude/skills/`）。
  - REPL 新增 `/memory`（记忆索引、来源、和会话开始时是否有变化、索引与文件对不上的地方）、
    `/skills`（可用技能列表、被同名覆盖的、写坏的）、`/prompt`（打印本会话完整 system prompt）；
    未知命令先按 `/技能名 [补充说明]` 尝试当作技能调用（`_run_skill`）。
  - `sa run` 支持 `/技能名 [补充说明]` 作为 prompt，会先展开成技能全文再执行；启动时的摘要行
    （项目指令 / 记忆条数 / 技能个数）走 stderr，不混进正文输出。
  - `sa serve`（工作台）按会话缓存 `(Knowledge, system prompt)`，第一次用到时读一次，之后每轮复用；
    工作台会话里也支持 `/技能名` 展开。
- 升级：三样东西都遵循「渐进式披露」和「会话内绝对不变」两条原则，目的是不破坏 M6 打下的 system
  prompt / 工具列表前缀缓存；会话中途改了 `AGENTS.md`、记忆文件、技能目录，都是下一个会话才生效。
- 修复：工作台 Runner 每一轮输入都新建 Agent、重新拼 system prompt，跨过午夜日期一变（M7 之后改一条记忆、
  加一个技能也一样）整段前缀缓存就作废；现在按会话缓存，连日期一起冻住。

## 函数级改动

### `src/simpleagent/knowledge/__init__.py`（新增）

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Knowledge`（dataclass） | 新增 | 持有 `instructions`、`memory`（`MemoryStore \| None`）、`memory_index`（会话开始时的索引快照）、`confirm_memory_writes`、`skills` |
| `Knowledge.load(config, cwd, home=None)` | 新增 | 按各自的 `enabled` 开关分别读项目指令、记忆索引快照、技能目录，一次性拼出一个 `Knowledge` |
| `Knowledge.tools()` | 新增 | 返回记忆三个工具（若记忆开启）+ `load_skill`（若有可用技能），顺序固定 |
| `Knowledge.prompt_section()` | 新增 | 拼出追加到 system prompt 的内容：项目指令 → 长期记忆 → 技能，都没有则为空串 |
| `Knowledge.summary()` | 新增 | 启动时打印的一行摘要，如「项目指令 ~/AGENTS.md · 记忆 3 条 · 技能 2 个」 |
| `_short(path)` | 新增 | 展示用：把家目录路径缩写成 `~/...` |

### `src/simpleagent/knowledge/frontmatter.py`（新增）

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `FrontmatterError` | 新增 | frontmatter 没有结束的 `---` 等写坏的情况 |
| `split_frontmatter(text)` | 新增 | 拆出 Markdown 文件开头 `---` 包起来的字段和正文；没有 frontmatter 时原样返回 |
| `format_frontmatter(fields, body)` | 新增 | 把字段和正文拼回文件内容，需要时给值加双引号 |
| `is_true(value)` | 新增 | 判断字符串是否表示真（`true` / `yes` / `on`，大小写不敏感） |
| `_parse` / `_value` / `_block` / `_fold` / `_quoted` / `_strip_comment` | 新增 | 手写的 YAML 子集解析内部实现：支持普通值、单双引号、`\|`/`>` 块、折行、注释；不支持的嵌套结构原样跳过 |

### `src/simpleagent/knowledge/instructions.py`（新增）

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `InstructionFile`（dataclass） | 新增 | 一份指令文件：路径、`scope`（`user`/`project`）、进 prompt 的内容、原文字数、是否被截断 |
| `project_root(cwd)` | 新增 | 从 cwd 往上找第一个含 `.git` 的目录，不在仓库里返回 `None` |
| `project_chain(cwd)` | 新增 | 项目根目录到 cwd 的每一层（从外到内）；不在仓库里只有 cwd 本身 |
| `find_instruction_files(cwd, home, filenames)` | 新增 | 按「个人 → 项目根 → … → cwd」顺序列出存在的指令文件，去重 |
| `load_instructions(cwd, home, filenames, max_chars)` | 新增 | 读出所有指令文件，按顺序（通用优先）分配字数预算，超出的截断并提示用 `read_file` 读原文 |
| `instructions_section(files)` | 新增 | 拼出 system prompt 里的「项目指令」一节 |
| `_first_file(directory, filenames)` | 新增 | 一个目录里按 `filenames` 顺序取第一个存在的文件 |

### `src/simpleagent/knowledge/memory.py`（新增）

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `MemoryStoreError` | 新增 | 记忆操作失败（名字不合法、记忆不存在、写盘失败），消息可直接展示给模型 |
| `MemoryStore`（类：`path_for` / `names` / `read_index` / `read` / `write` / `delete` / `check` / `_update_index`） | 新增 | 记忆文件的读写；`write` 新建或覆盖一条记忆并同步索引；`delete` 删除并更新索引；`check` 找索引与实际文件对不上的地方；索引更新只替换链接到目标文件的那一行，保留人工加的其他内容 |
| `index_line(name, description)` | 新增 | 生成索引里的一行 `- [名字](文件) — 说明`，超长的说明截断 |
| `memory_section(store, index)` | 新增 | 拼出 system prompt 里的「长期记忆」一节，含索引快照和什么时候该写记忆的说明 |
| `MemoryNameArgs` / `MemoryWriteArgs`（pydantic） | 新增 | `memory_read`/`memory_delete` 与 `memory_write` 的参数模型 |
| `memory_tools(store, confirm_writes=True)` | 新增 | 生成 `memory_read`（只读免确认）、`memory_write`、`memory_delete`（默认需确认）三个 `Tool` |
| `_atomic_write(path, text)` | 新增 | 先写临时文件再 `os.replace` 改名，避免写到一半进程被杀导致文件损坏 |

### `src/simpleagent/knowledge/skills.py`（新增）

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `SkillError` | 新增 | 技能写坏了或读不出来 |
| `Skill`（dataclass：`body` / `resources` / `render`） | 新增 | 一个技能：`body()` 每次现读正文（改了技能不用重开会话）；`resources()` 列出目录下除 `SKILL.md` 外的附带文件；`render()` 拼出交给模型的技能全文 + 附带文件清单 |
| `SkillCatalog`（dataclass：`listed` / `prompt_section` / `tool` / `expand_command`） | 新增 | 已发现的技能集合；`listed` 是按名字排序、可被模型调用的技能；`prompt_section()` 拼技能列表节；`tool()` 生成 `load_skill` 工具（无技能返回 `None`）；`expand_command(text)` 把 `/技能名 补充说明` 展开成完整请求（正文 `$ARGUMENTS` 替换成补充说明） |
| `LoadSkillArgs`（pydantic） | 新增 | `load_skill` 工具的参数模型 |
| `skill_roots(dirs, home, cwd)` | 新增 | 汇总要扫描的技能目录：个人目录优先，配置里的相对路径按项目根到 cwd 每层展开 |
| `discover_skills(roots)` | 新增 | 扫描各目录的直接子目录，找 `SKILL.md`；同名技能先到先得，后来者记入 `shadowed` |
| `load_skill_file(path, root)` | 新增 | 读一个 `SKILL.md` 的 frontmatter，校验 `name`/`description`，生成 `Skill`（正文不预读） |

### `src/simpleagent/agent/prompt.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `build_system_prompt(base, cwd=None, now=None, knowledge=None)` | 修改 | 新增 `knowledge` 参数（`Knowledge \| None`），非空时把 `knowledge.prompt_section()` 追加到基础提示词 + 环境信息之后 |

### `src/simpleagent/config.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `InstructionsConfig` | 新增 | 对应 `[instructions]`：`enabled`（默认 `true`）、`filenames`（默认 `["AGENTS.md", "CLAUDE.md"]`）、`max_chars`（默认 32000） |
| `MemoryConfig` | 新增 | 对应 `[memory]`：`enabled`（默认 `true`）、`confirm_writes`（默认 `true`） |
| `SkillsConfig` | 新增 | 对应 `[skills]`：`enabled`（默认 `true`）、`dirs`（默认 `[".agents/skills", ".claude/skills"]`） |
| `Config` | 修改 | 新增 `instructions` / `memory` / `skills` 三个字段，各自默认工厂对应上面三个 Config 类 |

### `src/simpleagent/ui/repl.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Repl.__init__` | 修改 | 新增加载 `self.knowledge = Knowledge.load(config, cwd)`；工具列表加上 `knowledge.tools()`；`build_system_prompt` 传入 `knowledge` |
| `Repl.run`（启动打印部分） | 修改 | 有摘要时打印 `self.knowledge.summary()` |
| `Repl.command` | 修改 | 新增 `case "memory"`、`case "skills"`、`case "prompt"`；未知命令从直接报错改为调用 `_run_skill(line)` |
| `Repl._run_skill(line)` | 新增 | 把 `/技能名 补充说明` 用 `knowledge.skills.expand_command` 展开后当作一轮对话发出；技能不存在则提示未知命令 |
| `Repl._show_memory()` | 新增 | 打印记忆目录、条数、索引内容、索引是否在会话开始后被改过、索引与文件对不上的地方 |
| `Repl._show_skills()` | 新增 | 打印技能目录、每个技能的名字/描述/路径（含只能手动调用的标记）、被覆盖的技能、写坏的技能 |
| `HELP` 文本 | 修改 | 加上 `/memory`、`/skills`、`/prompt` 的说明 |

### `src/simpleagent/ui/headless.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Headless.__init__` | 修改 | 新增加载 `self.knowledge = Knowledge.load(config, self.cwd)`；工具列表加上 `knowledge.tools()`；`build_system_prompt` 传入 `knowledge` |
| `Headless.run(prompt)` | 修改 | 先用 `knowledge.skills.expand_command(prompt)` 尝试展开 `/技能名`（读取失败返回退出码 1）；有摘要时写到 stderr |

### `src/simpleagent/serve/runner.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Runner.__init__` | 修改 | 新增 `self._prompts: dict[str, tuple[Knowledge, str]]`，按会话 id 缓存 `Knowledge` 和拼好的 system prompt |
| `Runner._session_prompt(session_id, cwd)` | 新增 | 会话第一次用到时调用 `Knowledge.load` + `build_system_prompt` 并缓存（连同 `mcp.prompt_section()`），之后同一会话直接复用 |
| `Runner._build_agent`（原逻辑） | 修改 | 改为调用 `_session_prompt` 取 `(knowledge, system_prompt)`；`ToolRegistry` 加上 `knowledge.tools()`；不再每次重新调用 `build_system_prompt` / `mcp.prompt_section()` |
| `Runner._run_input` | 修改 | 构造好 Agent 后用 `knowledge.skills.expand_command(user_input)` 展开 `/技能名`（不是技能就用原话；技能读不出来按启动失败处理，推 error 帧）；界面上显示的还是用户原话 |

## 配置与依赖

- 新增三段可选配置，默认全部开启：`[instructions]`、`[memory]`、`[skills]`，模板见
  `src/simpleagent/config.example.toml`；不写任何一段时行为等同于各自的默认值（已有 `test_example_m7_blocks_load_when_uncommented` 覆盖）。
- 数据目录（`~/.simpleagent/`，开发模式是 `~/.simpleagent-dev/`）新增 `AGENTS.md`（个人指令，所有项目通用，需要用户自己创建，没有则不生效）、`memory/`（长期记忆，`memory_write` 首次调用时自动创建）、`skills/`（个人技能目录）。
- 工具列表从固定 7 个内置工具变成 7 + 3 个记忆工具（`memory_read`/`memory_write`/`memory_delete`），再加上有技能时的 `load_skill`；`tests/test_repl.py::test_context_command` 相应从「工具 7 个」改为「工具 10 个」。
- **需要用户手动处理**：
  - 记忆写入（`memory_write`/`memory_delete`）默认需要终端 `y` 确认；`sa run`（无人值守）里默认直接拒绝这两个工具，定时任务要允许写记忆需显式加 `--allow memory_write`。嫌确认烦可以在 `config.toml` 里 `[memory] confirm_writes = false`。
  - 想用项目指令、个人技能，需要用户自己创建 `~/.simpleagent/AGENTS.md`、`~/.simpleagent/skills/<名字>/SKILL.md`（或项目里的 `.agents/skills/`、`.claude/skills/`）；不创建则该功能无实际内容，不影响其他行为。
  - 在有 `AGENTS.md` / `CLAUDE.md` 的目录（包括本仓库自身，因为有 `AGENTS.md`）里跑 `sa`，system prompt 会自动带上其内容，属于预期行为，无需额外操作。
- 无新增第三方依赖（frontmatter 手写解析，未引入 PyYAML）；无需 `uv sync`。

## 测试

- 新增 `tests/knowledge/`：`test_frontmatter.py`、`test_instructions.py`、`test_knowledge.py`、`test_memory.py`、`test_skills.py`，共 5 个文件、72 个测试（frontmatter 解析/生成、指令查找与截断、`Knowledge.load`/`prompt_section`/`tools`/`summary`、记忆读写删与索引维护、技能发现/遮蔽/展开，以及用 FakeLLM 走通「记住 → memory_write」「按描述 load_skill」等场景）。
- `tests/test_config.py` 新增 `test_example_m7_blocks_load_when_uncommented`：验证 `config.example.toml` 里 M7 三段注释去掉后能正常加载，且默认值与不写时一致。
- `tests/test_repl.py::test_context_command` 改为先 `monkeypatch.chdir` 到临时目录（避免本仓库自己的 `AGENTS.md`/技能目录影响断言），并将工具数量断言从 7 个改为 10 个。
- 测试结果：`uv run pytest -q` → **677 passed**。
- `uv run ruff check`：All checks passed。
- `uv run ruff format --check`：155 files already formatted（无需改动）。
- 开发时用 deepseek-flash 在隔离的临时数据目录做过真实模型验证（非自动化测试，细节见学习笔记第 8 节）：说「记住」时第一步就调用 `memory_write`；新会话仅凭索引摘要即可正确回答；任务描述与技能描述匹配时会先 `load_skill`；M7 三项全开比全关每次请求多约 1,014 token（1,847 → 2,861）。

## 相关笔记

- `docs/notes/M7-memory-skills-instructions.md`：渐进式披露的设计原理、三项各自的取舍（为何不用向量检索/RAG、索引由工具而非模型直接维护、写入默认需确认等）、与 M6 前缀缓存的相处方式，以及真实模型的实测记录。
