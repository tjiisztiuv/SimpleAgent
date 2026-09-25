# 新增 GitHub Actions：main 每次更新自动把版本号最后一位 +1

- 日期：2026-09-25
- 对比基线：`b471811`（Merge pull request #15：控制面板消息可以引用到指挥台，照着一条消息拆任务派空间）
- 对应里程碑：无

## 功能变化

- 新增：`.github/workflows/bump-version.yml`。main 分支每次更新（合并 PR 或直接推送）后，用
  `uv version --bump patch --no-sync` 把 `pyproject.toml` 和 `uv.lock` 里的版本号最后一位 +1
  （0.2.0 → 0.2.1），由 `github-actions[bot]` 提交回 main，不打 tag。
  - 如果这次推送本身已经改过版本号（比如手动发 0.3.0 正式版），就跳过：比较 `github.event.before`
    和 `github.sha` 两个提交里 `pyproject.toml` 的版本号是否一致。
  - bot 用默认的 `GITHUB_TOKEN` 推送，不会再触发本 workflow，不会自己循环。
  - `concurrency` 按 `bump-version` 分组排队（`cancel-in-progress: false`）；checkout 时取的是
    最新的 main（`ref: main`）而不是触发这次运行的那个提交，避免排队期间基于旧版本号重复 +1；
    推送被拒时 `git pull --rebase` 后重推。
  - `uv` 固定在 `0.12.x`（和 `pyproject.toml` `build-system` 里的 `uv_build` 同一代，避免新版 uv
    改写 `uv.lock` 格式），`setup-uv` action 按 commit SHA 固定在 `v10.2.0`。
  - 已知限制：一次运行还没结束又连续推了两次以上时，GitHub 同一 concurrency 分组只保留一个排队中的
    运行，中间那次的 +1 会被跳过（版本号比 main 的更新次数少加一次，不会重号）。
- 升级：`.claude/skills/ship/SKILL.md`
  - 第 8 步（合并前检查）加一句：不要在分支里改版本号，合并后 workflow 会自动 +1 并提交回 main；
    顺手把「仓库目前没配 CI」的措辞改成「PR 上目前没有检查」，避免和新增的 workflow 说法冲突。
  - 第 9 步（收尾）改为先用 `gh run list --workflow bump-version.yml` 找到对应 `databaseId`、
    `gh run watch <databaseId> --exit-status` 等 bump workflow 跑完，再 `git switch main && git pull`；
    运行失败时不要自己在 main 上补提交改版本号，把失败链接写进汇报交给用户处理；汇报清单里加一条
    「合并后的版本号（`uv version --short`）」。
- 升级：`AGENTS.md` 2.3 节补充版本号规则——main 每次更新自动 +1、平时分支里不要手动改版本号、
  直接推 main 前先 `git pull`（本地会少 bot 刚推的那个提交）、正式发版（0.x.0）时手动
  `uv version --bump minor` + 写 `docs/releases/` + 打 tag，workflow 检测到这次推送已改过版本号就不会再 +1。
- 文档：`README.md` 安装一节补一句，main 每次更新版本号最后一位会自动 +1，`sa --version` 能看出装的
  是哪一版。

## 函数级改动

- 无（`src/` 未改动）

## 配置与依赖

- **需要手动处理**：这个改动合并进 main 并触发 workflow 第一次运行后，main 上的版本号会从 0.2.0
  变成 0.2.1。之后本地 main 每次都会比远端少 bot 自动提交的那个「版本号 +1」提交，直接推 main 前
  要先 `git pull`；`ship` 技能合并 PR 后也会等这个 workflow 跑完再 pull。
- 仓库新增 `permissions: contents: write` 的 workflow，需要仓库 Actions 已启用且没有阻止 bot 推送
  的规则集（分支保护、rulesets）；按汇报里的核对，目前仓库满足这个前提，但 GitHub 上真实运行的效果
  要等这次改动合并到 main 才能验证到。

## 测试

- 无新增/修改的测试文件（改动集中在 workflow YAML 和三份 Markdown 文档）。
- `uv run pytest -q`：759 passed。
- `uv run ruff check`：All checks passed。
- `uv run ruff format --check`：182 files already formatted。
- 手动验证：在临时克隆里模拟了 workflow 里的 shell 逻辑，覆盖三种情况——普通推送 0.2.0 → 0.2.1
  且只改 `pyproject.toml`、`uv.lock` 各一行；推送本身已改过版本号时跳过；`before` 为全 0（首次推送场景）
  时照常 +1。另外确认了 YAML 能正常解析、仓库 Actions 已启用且没有规则集会挡住 bot 推送。没有起服务
  做端到端验证（这次改动不涉及界面、API、CLI 行为），GitHub 上 workflow 的真实运行要合并后才能确认。

## 相关笔记

- 无
