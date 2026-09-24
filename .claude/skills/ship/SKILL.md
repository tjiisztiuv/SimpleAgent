---
name: ship
description: 把当前分支的改动走完上线流程：审查 diff、跑 ruff 和 pytest、必要时在临时数据目录里手动验证、写变更记录、提交推送、开 PR 并合并到 main，最后切回 main 拉取。只在用户敲 /ship 时运行。
disable-model-invocation: true
---

# /ship：审查 → 验证 → 开 PR → 合并到 main

用户敲 `/ship` 就是明确要求提交、推送并合并（AGENTS.md 2.3），不用再逐步确认。
但遇到下面「停下来问」的情况必须停，不能为了走完流程硬合并。

`/ship` 后面的补充说明要照办，比如「只开 PR 不合并」就在第 7 步开完 PR 后停下。

## 停下来问用户的情况

- 审查发现的不是小 bug，而是设计层面的问题，或者要大改才能修（按 AGENTS.md 2.1，大改先讲清楚再动手）
- 测试失败，而且不是这次改动引起的，或者一两处小修改修不好
- diff 里出现疑似 API key（`sk-` 开头的长串、`.env` 内容）
- 和 main 有冲突、远端分支上有你没见过的新提交、PR 的检查没通过
- 当前在 main 上且没有任何改动：没东西可发

## 1. 摸清现状

```bash
git fetch origin
git status -sb
git log --oneline origin/main..HEAD
git diff --stat origin/main
git ls-files --others --exclude-standard
gh pr list --head "$(git branch --show-current)" --state all
```

- 在 main 上有改动：先建分支 `feat/<简短英文描述>` 再继续。
- 这个分支已经有 PR：沿用它，推送后 PR 自动更新，不要重复开。
- 可能有别的会话在同一个分支上干活：提交和推送前都要再看一次 `git status` 和
  `git log origin/<分支>..HEAD`，别人刚提交的东西不要覆盖，也不要 force push。

## 2. 审查

读完整的 diff（`git diff origin/main` 加上未跟踪的新文件），重点找：

- 正确性：边界条件、错误路径、并发（runner 在事件循环线程里跑）、半新半旧的落盘状态
- 前后端是否一致：API 返回码、字段名，前端 `web/app.js` 有没有跟上
- 有没有违反 [ARCHITECTURE.md 里的接口约定](../../../docs/ARCHITECTURE.md)（状态归 Agent / Session、审批器异步、事件可序列化……）
- 测试覆盖的是不是真正的风险点

小 bug 直接修，同时补一条测试，最后在汇报和 PR 描述里单独说明。
只是风格偏好、以后再说也行的点，记下来汇报，不要顺手改。

## 3. 自动检查

```bash
uv run ruff format
uv run ruff check
uv run pytest -q
```

三项都要过。`ruff format` 改了文件的话，这些改动也要一起提交。

## 4. 手动验证（改了界面、API 或 CLI 行为时做）

测试不覆盖 `web/` 下的前端，也不覆盖真实的服务进程。只要这次改了用户能看到的东西，就起一个
**临时数据目录**的服务亲手点一遍：

```bash
H=<临时目录>/sa_home          # 放在会话的 scratchpad 里，别放进仓库
mkdir -p "$H"
cat > "$H/config.toml" <<'EOF'
default_profile = "a"

[profiles.a]
base_url = "http://a.invalid/v1"
model = "model-a"

[profiles.b]
base_url = "http://b.invalid/v1"
model = "model-b"
EOF
SIMPLEAGENT_HOME="$H" uv run sa serve --port 8399   # 后台运行
```

- 用浏览器面板打开 `http://127.0.0.1:8399/`，把改动涉及的操作走一遍；API 用 `curl` 验证返回码。
- 假 profile 连不上模型，所以要调模型的路径只能靠测试。这部分要在汇报里写明「没有手动验证」。
- 只改了 REPL / CLI 的话，用 `SIMPLEAGENT_HOME="$H" uv run sa ...` 试。
- 不要碰 `~/.simpleagent`、`~/.simpleagent-dev`，不要读 `.env`，不要用全局的 `sa`；
  不要占用 8385（用户开发时用的端口）。
- 验证完关掉服务：`kill $(lsof -tiTCP:8399 -sTCP:LISTEN)`。

纯重构、纯文档、只改测试的推送可以跳过这一步，汇报里说一声。

## 5. 变更记录

按 AGENTS.md 2.2，推送前要在 `docs/changelog/` 写记录。

- 这个分支已经有覆盖本次改动的记录：直接更新它，不要再写一份。
- 没有：交给子 agent `changelog-writer` 写。没有子 agent 的环境（OpenCode、SimpleAgent 自己）
  就照 `.claude/agents/changelog-writer.md` 的步骤自己写。
- 写完主会话要核对：和实际 diff 对得上；第 2 步修的 bug、第 4 步的验证结果都写进去了；
  `docs/changelog/README.md` 的索引最上面加了一条。

## 6. 提交、推送

- 用 `git add <具体文件>`，不要把 scratchpad、临时文件带进去。
- 提交信息沿用仓库风格：中文一句话概括，空一行后列要点；末尾按当前环境的要求加署名。
- `git push -u origin <分支>`。被拒就先 `git fetch` 看远端多了什么，不要 force push。

## 7. 开 PR

```bash
gh pr create --base main --head <分支> --title "<中文一句话>" --body-file -
```

PR 描述分三段：**做了什么**（要点 + 变更记录链接）、**review 时修的问题**（没有就写无）、
**验证**（ruff、pytest 的结果，手动验证走了哪些操作）。

## 8. 合并

```bash
gh pr view <编号> --json mergeable,mergeStateStatus,statusCheckRollup
```

- `mergeable` 必须是 `MERGEABLE`；有检查的话要全部通过，还在跑就等它跑完（仓库目前没配 CI，`statusCheckRollup` 为空）。
- `gh pr merge <编号> --merge`：保留合并提交，和历史上的 PR 一致。
- 不要用 `--admin` 绕过检查，不要开 auto-merge，不删远端分支（用户要求才删），不打 tag、不发版本。

## 9. 收尾和汇报

```bash
git switch main && git pull
```

汇报要简短，包括：

- PR 链接和合并提交的 hash
- 审查发现了什么、修了什么（写上文件和行号）
- 自动检查结果：pytest 通过的数量
- 手动验证走了哪些操作；哪些没法验证
- 留给用户决定的事：发现了但没修的问题、本地和远端还留着的特性分支
