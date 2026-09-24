# README：安装前提醒 sa 和 macOS 自带命令撞名

- 日期：2026-09-24
- 对比基线：`3e780ae`（Merge pull request #9 from tjiisztiuv/claude/cross-space-task-dispatch-33385a）
- 对应里程碑：无（纯文档改动）

## 功能变化

- 修复：把「`sa` 和 macOS 自带 `/usr/sbin/sa`（系统记账统计）撞名」的提醒，从「安装」一节列表*之后*挪到列表*之前*，改成一段醒目的引用块（blockquote），让读者在动手装之前就先看到，而不是装完才在下面看到说明。新增了怎么认出撞名的症状：`sa --version` 输出 `illegal option` 和 `usage: sa [-abcd...]`；cron、launchd 这类 PATH 很短的环境同样会踩到；解法不变，用等价的长名字 `simple_agent`，或写绝对路径 `~/.local/bin/sa`。原来列表后面讲同一件事的那段删掉，不重复。

## 函数级改动

无（本次只改了 `README.md`，`src/` 下没有改动）。

## 配置与依赖

无，用户不需要手动处理任何东西。

## 测试

- 未新增或修改测试文件（纯文档改动）。
- `uv run pytest -q`：718 passed。
- `uv run ruff check`：All checks passed!
- `uv run ruff format --check`：170 files already formatted。

## 相关笔记

无。
