"""指挥台的调度者：一句任务进来，派给合适的空间；跨空间时先出计划等用户确认。

    command_prompt(spaces)        调度者的 system prompt：角色 + 可用空间清单（会话内不变）
    command_tools(dispatcher, …)  propose_plan / dispatch 两个工具，每轮对话造一份
    Dispatcher                    工具需要的调度能力（Runner 实现：建子会话、等它跑完）

调度者本身是保留空间 sp_command 里的一个普通会话，只有这两个工具，不读写文件；
派出去的子会话是目标空间里的普通会话，拿不到 dispatch，从机制上杜绝递归派发。
设计见 docs/design/command-dispatch.md。
"""

from simpleagent.command.prompt import command_prompt, spaces_section
from simpleagent.command.tools import ChildResult, Dispatcher, command_tools, resolve_space

__all__ = [
    "ChildResult",
    "Dispatcher",
    "command_prompt",
    "command_tools",
    "resolve_space",
    "spaces_section",
]
