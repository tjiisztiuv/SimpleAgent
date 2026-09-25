"""外部 CLI agent 的适配层。

每家的无头模式都吐 NDJSON，但字段名和事件划分各不相同（有的把工具调用和结果拆成两条，
有的塞在同一个事件里）。这一层负责翻译成我们自己的 `Event`，
这样 Runner、总线、存储、前端都不用知道对面是谁。

约定：**适配器实例是一次运行一份**（`adapter_for()` 每次新建），
所以它可以放心把「对面的 session id」「累计用量」这些跨事件的状态挂在自己身上。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from simpleagent.events import Event, Usage
from simpleagent.permissions import Mode

# 权限档位。两家的无头模式都没法把「要不要批准」实时问回给我们（claude 要外接 MCP 的
# --permission-prompt-tool，opencode 只能预置 allow/deny），所以只能预先定档。
# 取值和内置执行者的权限模式（permissions.Mode）是同一套，空间设置里一个下拉管两种执行者：
SAFE = Mode.READ_ONLY.value  # 只读：只放行读类工具，改文件 / 跑命令一律拒绝
FULL = Mode.FULL.value  # 全放行：不经确认就能改文件、跑命令（界面上要标红）
# 外部 CLI 暂时没有「工作区」档：claude 的 acceptEdits、opencode 的 external_directory
# 都还没接，接上之前不提供兑现不了的选项
PERMISSIONS = (SAFE, FULL)
PERMISSION_LABELS = {
    SAFE: "只读（只放行读类工具）",
    FULL: "全放行（可改文件、跑命令）",
}


@dataclass
class CliTurn:
    """一行 NDJSON 翻译出来的结果。

    `finished` / `ok` 只有对面明确说「这一轮结束了」的那条事件才置位
    （claude 的 `result`、opencode 的 `step_finish`）；进程退出码两边都不完全可靠。
    """

    events: list[Event] = field(default_factory=list)
    finished: bool = False
    ok: bool | None = None
    error: str | None = None


class CliAdapter(Protocol):
    """把某个外部 CLI 的 NDJSON 接进我们的运行循环。"""

    name: str

    # 跨事件状态（实例级，一次运行一份）
    session_id: str | None
    usage: Usage
    cost_usd: float

    def command(
        self,
        prompt: str,
        *,
        command: str | None = None,
        model: str | None = None,
        resume: str | None = None,
        mode: str = SAFE,
    ) -> list[str]:
        """拼出这次要跑的命令行。`command` 是 space.toml 里可以覆盖的可执行文件。"""
        ...

    def env(self, mode: str = SAFE) -> dict[str, str]:
        """这次运行额外注入的环境变量。

        **本机默认 = 空**：什么都不注入，让 CLI 完全吃它自己的配置（等于你在那个目录
        手动敲 `claude` / `opencode`）。只有需要在命令行之外传配置时才用它。
        """
        ...

    def parse(self, line: str) -> CliTurn:
        """翻译一行 NDJSON；不认识的行返回空的 CliTurn。"""
        ...
