# AGENTS.md

本文件给在本仓库工作的 AI agent（Claude Code、OpenCode 等）提供协作约定。

如果本文件与代码或文档现状不一致，以实际代码为准，并在相关改动中顺手修正本文件。

## 1. 项目速览

SimpleAgent 是一个自用的本地 agent，有两个目标：一是学习 agent 的核心机制，方便测试业界新 feature；二是在本地解决真实问题。技术选型：Python 3.12 + uv，只接 OpenAI 兼容协议，agent loop 从零手写。

- [README.md](README.md)：目标、快速开始
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：架构、设计原则、代码结构
- [docs/ROADMAP.md](docs/ROADMAP.md)：里程碑和当前进度（看“状态”列）
- [docs/notes/](docs/notes/)：每个里程碑的学习笔记
- [docs/changelog/](docs/changelog/)：每次推送的变更记录

## 2. 协作流程（硬规则）

### 2.1 教程式推进：先讲清楚，确认后再改

实现新功能（M2 及之后的里程碑）时，**不要一次性全部写完**。

1. 把里程碑拆成几个小步骤。例如 M2：抽出 `Agent` / `Session` → 工具抽象和注册表 → 带工具调用的 loop → 内置工具 → 截断、并行、中断补结果。
2. 每一步先以教程方式讲解，然后**停下等用户确认**。讲解包括：
   - 要解决什么问题，在 agent 里起什么作用
   - 怎么实现、为什么这样设计，放弃了哪些方案
   - 新增和修改哪些文件、函数，附函数签名和关键代码片段
   - 怎么验证：写哪些测试，怎么手动试
3. 用户确认后再批量修改代码、跑测试，然后汇报实际改动；和讲解有出入的地方要单独说明。

修 bug 这类小改动可以直接做，做完说明即可。

原因：学习是项目的首要目标之一，用户要理解并掌控每一步，而不是一次收到一大堆写好的代码。

### 2.2 推送到 GitHub 前写变更记录

每次推送前，在 `docs/changelog/` 写一份本次推送相对上次推送（`origin/main`）的变更记录，和代码一起提交推送。

- 内容：功能变化（新增、升级、修复）；`src/` 下按函数/类列出改动；配置与依赖变化，尤其是需要用户手动处理的地方；测试情况
- 规则和模板见 [docs/changelog/README.md](docs/changelog/README.md)
- 在 Claude Code 里用子 agent [`changelog-writer`](.claude/agents/changelog-writer.md) 来写：它只负责写记录，主会话检查后再提交推送

### 2.3 提交与推送

- 只在用户明确要求时才 `git commit` / `git push`
- 用户敲 `/ship` 就是要求走完整个上线流程：审查、检查、验证、变更记录、开 PR、合并到 main，步骤见 [`.claude/skills/ship/SKILL.md`](.claude/skills/ship/SKILL.md)

## 3. 长期方向：个人 AI 工作台

- 项目长期要做成**桌面客户端**形式的个人 AI 工作台（不是网页）。客户端负责界面和消息通知，业务逻辑全部在用户自己写的 Python 代码里，不用低代码或平台化方案。
- 工作台的具体功能和客户端技术栈**还没设计**，不要提前替用户选定。
- 外部 agent（Claude Code / OpenCode）作为工具接入，规划在 M8。
- 从 M2 开始，设计要遵守 [ARCHITECTURE.md 里的接口约定](docs/ARCHITECTURE.md#m2-起就要守住的接口约定)：状态归 `Agent` / `Session`、审批器是异步接口、工具能上报进度、取消是显式调用、事件可序列化。这样终端、桌面客户端、定时任务才能共用同一个核心。M4 的 `sa daemon` 往“常驻后台引擎”的方向设计。

## 4. 常用命令

```bash
uv sync              # 安装依赖
uv run sa            # 启动 REPL（首次先 uv run sa init）
uv run pytest        # 测试，不联网
uv run ruff check    # lint
uv run ruff format   # 格式化（也会格式化 Markdown 里的 Python 代码块）
```

改完代码至少跑一遍 `ruff format`、`ruff check` 和 `pytest`。

用户本机同时装了日常用的 `sa`（GitHub `main` 的快照，数据在 `~/.simpleagent/`）。在仓库里 `uv run sa`
是开发模式，数据目录自动换成 `~/.simpleagent-dev/`，`sa serve` 默认端口 8385（`config.dev_checkout()`）。
手动试功能用 `uv run sa`，不要碰全局的 `sa` 和 `~/.simpleagent/`。

## 5. 安全约定

- API key 只放在环境变量或 `~/.simpleagent/.env` 里，不要写进 `config.toml`、代码、trace 或日志，也不要在输出中打印 key。
- 没有用户要求时，不要读取或修改 `~/.simpleagent/.env`。
- 测试通过 `SIMPLEAGENT_HOME` 把数据目录指向临时目录（`tests/conftest.py` 里的 `sa_home` fixture），不要写入真实的 `~/.simpleagent` 或 `~/.simpleagent-dev`。
