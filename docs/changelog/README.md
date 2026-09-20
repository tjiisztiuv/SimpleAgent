# 变更记录

每次推送到 GitHub 前，写一份本次推送相对上次推送的变更记录：升级了哪些功能、改了哪些函数。

## 规则

- **时机**：推送前写好，和代码放在同一批提交里一起推送
- **对比基线**：上次推送到 GitHub 的提交（`origin/main`），覆盖这之间的所有本地提交
- **文件名**：`YYYY-MM-DD-<简短英文描述>.md`，比如 `2026-09-17-m0-m1-streaming-chat.md`
- **函数级改动**：`src/` 下的代码列到函数/类级别；测试只列到文件级别
- **新记录加到下面索引的最上面**

## 模板

```markdown
# <一句话概括本次推送>

- 日期：YYYY-MM-DD
- 对比基线：`<hash>`（<那次提交的标题>）
- 对应里程碑：M?

## 功能变化

- 新增：……
- 升级：……
- 修复：……

## 函数级改动

### `src/simpleagent/<文件>.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Foo.bar()` | 新增 / 修改 / 删除 | 做了什么、为什么 |

## 配置与依赖

- 配置项、依赖、数据目录的变化
- 需要手动处理的地方（比如要改 `~/.simpleagent/config.toml`）

## 测试

- 新增或修改的测试文件
- 测试结果：N passed

## 相关笔记

- `docs/notes/` 里对应的学习笔记（没有就省略）
```

## 索引

- [2026-09-20 新增 debug 模式：交互时看清 API 调用与工具调用的过程（三档开关，输出走 stderr）](2026-09-20-debug-mode.md)
- [2026-09-19 空间会话里模型回复改为 Markdown 渲染（标题/列表/表格/引用/链接，零依赖手写，图片不加载）](2026-09-19-space-session-markdown-render.md)
- [2026-09-18 README 按「怎么用 sa」重写，补 v0.1.0 发布日志](2026-09-18-readme-rewrite-v0.1.0-release.md)
- [2026-09-18 任务终态分流：只有失败进消息，完成/取消只在指挥台显示；指挥台刷新后从后台恢复](2026-09-18-task-final-status-routing.md)
- [2026-09-18 sa 支持装成全局命令（uv tool install），补 --version 和撞名兜底](2026-09-18-global-cli-install.md)
- [2026-09-18 控制面板消息升级为中控：详情弹层、自动归档、`sa inbox push` 外部投递](2026-09-18-panel-inbox-detail-archive-push.md)
- [2026-09-17 修复新建空间「创建」按钮点了没反应（浏览器 SSE 连接被占满）](2026-09-17-wizard-create-button-sse-connection-limit-fix.md)
- [2026-09-17 修复新建空间向导的「执行者」「权限」下拉](2026-09-17-wizard-executor-permission-dropdown-fix.md)
- [2026-09-17 外部 CLI 执行者（Claude Code / OpenCode）接入空间 + 合并近期活动流分支](2026-09-17-cli-executors-and-panel-merge.md)
- [2026-09-17 M3（权限 / 会话 / headless）+ 工作台 W3~W5 + 新建空间拆成两维](2026-09-17-m3-workbench-w3-w5-and-space-wizard.md)
- [2026-09-17 控制面板改为「近期活动流」，任务完成首次广播状态帧](2026-09-17-panel-recent-activity-and-done-frame.md)
- [2026-09-17 客户端 UI 静态原型去除本机用户名](2026-09-17-client-ui-mockup-redact-username.md)
- [2026-09-17 客户端 UI 设计文档去除本机用户名](2026-09-17-client-ui-doc-redact-username.md)
- [2026-09-17 工作台 W1 + W2：Space/会话持久化 + 本地 HTTP + SSE API](2026-09-17-workbench-spaces-and-local-api.md)
- [2026-09-17 M2 完成：其余 6 个内置工具 + 输出截断落盘 + 只读并行/含写串行](2026-09-17-m2-tools-truncation-and-fixes.md)
- [2026-09-17 M2 第一段：工具抽象 + list_dir + 带工具调用的 Agent loop](2026-09-17-m2-tool-calling-list-dir.md)
- [2026-09-17 新增 changelog-writer 子 agent，用于推送前写变更记录](2026-09-17-changelog-writer-agent.md)
- [2026-09-17 M0 + M1：项目脚手架与流式对话 REPL](2026-09-17-m0-m1-streaming-chat.md)
