# 开发环境与日常环境隔离，发布 v0.2.0

- 日期：2026-09-22
- 对比基线：`31cfc7f`（Merge pull request #7 from tjiisztiuv/claude/m6-implementation-b5e0a6）
- 对应里程碑：无（工程维护 + 发布）

## 功能变化

- 新增：从源码仓库跑 `uv run sa` 时自动进入「开发模式」——数据目录换成 `~/.simpleagent-dev/`，
  `sa serve` 默认端口从 8384 换成 8385，`sa --version`、REPL 启动、`sa serve` 启动都会打印一行
  提示，方便和日常安装的 `sa` 区分，避免开发时的测试数据混进日常数据目录。
- 升级：日常用的 `sa` 改为从 GitHub 装非 editable 快照（`uv tool install
  "git+https://github.com/tjiisztiuv/SimpleAgent.git@main"`），本地改代码不再影响它；
  README 补充「开发」一节说明两边如何隔离、如何共用 API key。
- 升级：`argparse` 的 `ArgumentParser` 改用 `RawDescriptionHelpFormatter`，避免 `--version`
  里带路径的输出被终端宽度自动折行。
- 发布：v0.2.0，版本号 0.1.0 → 0.2.0，新增 `docs/releases/v0.2.0.md`，汇总 M5（MCP 客户端）、
  M6（上下文工程）、M4 第 1 步（定时任务定义）、debug 模式、工作台 Markdown 渲染和本次的环境隔离。

## 函数级改动

### `src/simpleagent/config.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `dev_checkout(module_file: str \| Path = __file__) -> Path \| None` | 新增 | 判断是否从源码仓库跑：`config.py` 往上两级有 `pyproject.toml` 且有 `.git`（目录或 worktree 的 `.git` 文件都算）就返回仓库根目录，否则（装进 site-packages 的快照）返回 `None`。 |
| `home_dir()` | 修改 | 数据目录优先级从「`SIMPLEAGENT_HOME` > `~/.simpleagent`」改为「`SIMPLEAGENT_HOME` > 开发模式下的 `~/.simpleagent-dev` > `~/.simpleagent`」，靠 `dev_checkout()` 判断是否处于开发模式。 |

### `src/simpleagent/cli.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `SERVE_PORT` / `DEV_SERVE_PORT` | 新增 | 模块级常量，`sa serve` 默认端口：日常 8384，开发模式 8385，两者可同时启动。 |
| `version_text() -> str` | 新增 | 组装 `sa --version` 的输出，开发模式下额外标出仓库路径和数据目录。 |
| `main(argv)` | 修改 | `ArgumentParser` 改用 `RawDescriptionHelpFormatter`；`--version` 的 `version=` 改用 `version_text()`；`serve` 子命令的 `--port` 默认值按 `dev_checkout()` 结果在 `SERVE_PORT`/`DEV_SERVE_PORT` 间切换；`serve` 启动成功后，开发模式下额外打印一行数据目录提示。 |

### `src/simpleagent/ui/repl.py`

| 函数 / 类 | 变化 | 说明 |
|---|---|---|
| `Repl.run()` | 修改 | 启动横幅之后，开发模式下额外打印一行「开发模式：数据目录 ……」提示。 |

## 配置与依赖

- 版本号：`pyproject.toml`、`uv.lock` 的 `simpleagent` 版本 0.1.0 → 0.2.0。
- 无新增第三方依赖。
- 数据目录规则变化：从源码仓库跑 `uv run sa`（含本仓库本身）现在默认写入 `~/.simpleagent-dev/`
  而不是 `~/.simpleagent/`，除非显式设置 `SIMPLEAGENT_HOME`。

### 需要手动处理

- **改用 GitHub 快照安装**：用户已手动执行
  `uv tool install "git+https://github.com/tjiisztiuv/SimpleAgent.git@main"`，把本机日常用的
  `sa` 从旧的 editable 安装换成非 editable 的 GitHub 快照，这一步不属于本次代码改动但需要记录。
  之前用 `uv tool install --editable .` 装过的机器，同样需要执行这条改用快照，否则日常用的 `sa`
  实际跑的是正在改动的源码。
- **发布 tag**：推送后需要打 annotated tag `v0.2.0` 并推到远程（`docs/releases/v0.2.0.md`
  已就绪，供打 tag 时的说明参考）。
- **升级日常版**：以后升级用
  `uv tool install --reinstall "git+https://github.com/tjiisztiuv/SimpleAgent.git@v0.2.0"`
  （或 `@main` 跟最新代码）。
- **开发目录首次初始化**：本仓库里 `uv run sa` 第一次用要单独 `uv run sa init`，`~/.simpleagent-dev/`
  和 `~/.simpleagent/` 是两套独立数据；API key 可以软链共用，不用重复配置：
  `ln -s ~/.simpleagent/.env ~/.simpleagent-dev/.env`。
- **历史遗留数据无法自动分离**：在本次改动之前，开发时用 `uv run sa` 产生的测试数据是直接写进
  `~/.simpleagent/` 的（当时还没有 `~/.simpleagent-dev/` 这个概念），这部分混进日常数据目录的
  旧测试数据没有自动迁移或清理机制，需要用户自行辨认、按需手动清理。

## 测试

- `tests/test_config.py`：新增 8 个测试。覆盖 `dev_checkout()` 对源码仓库、git worktree（`.git`
  是文件）、装进 site-packages 的快照、只有 `pyproject.toml` 没有 `.git` 四种情况的判定；
  `home_dir()` 在开发模式 / 普通模式下的取值，以及 `SIMPLEAGENT_HOME` 优先于开发模式；本仓库自身
  确实被识别为开发模式；`sa --version` 在开发模式下标出提示、非开发模式下不标。
- `tests/test_cli_serve.py`：新增 1 个测试，验证 `sa serve` 在两种模式下分别用 8384/8385 作为
  默认端口，并且只有开发模式打印数据目录提示。
- 测试结果：`uv run pytest -q` 593 passed。
- `uv run ruff check`：All checks passed。
- `uv run ruff format --check`：140 files already formatted，无需改动。
- 手动验证：在仓库目录下 `uv run sa --version` 显示开发模式提示和 `~/.simpleagent-dev` 路径；
  在其他目录执行已安装的 `sa --version` 只显示版本号；`uv run sa serve --help` 显示默认端口 8385。
  没有实际启动 `sa serve` 验证端口占用情况，避免影响用户正在运行的日常 `serve` 进程。

## 相关笔记

- 无
