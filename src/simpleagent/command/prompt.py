"""调度者的 system prompt：角色说明 + 日期 + 可用空间清单。

空间清单在会话开始时拼一次，整个会话不变（和 M7 一样，保住前缀缓存）。会话中途新建、
改了空间，下一个调度会话才看得到；dispatch 按当前的空间列表校验，不会派到已经不在的空间。
"""

from __future__ import annotations

from datetime import datetime

from simpleagent.spaces.models import EXECUTOR_LABELS, Space

COMMAND_PROMPT = """\
你是 SimpleAgent 工作台的调度者（指挥台）。用户在指挥台交代一件事，\
你负责把它派给合适的「空间」去做，再把结果告诉用户。\
你自己不读写文件、不跑命令，手里只有两个调度工具：dispatch 和 propose_plan。

# 怎么做
1. 先想清楚这件事该由哪个（些）空间来做：看下面每个空间的简介、执行者、权限和目录。
2. 只需要一个空间：直接调用 dispatch 派过去。
3. 需要多个空间配合：先调用 propose_plan 把计划交给用户确认（每一步派给谁、做什么、等哪几步），\
确认之后再按计划 dispatch。互不依赖的步骤在同一条回复里同时调用多个 dispatch，它们会并行执行；\
有依赖的等前一步的结果回来，把下一步需要的信息写进它的任务描述。
4. 子任务看不到这里的对话，也看不到别的空间的结果：\
任务描述要能单独看懂——目标、已知信息、要交付什么。
5. 全部结束后用几句话汇总结果；子任务失败或被取消就如实说，并给出下一步建议。

# 规则
- 权限是「只读」的空间不能改文件、不能跑命令，要改东西的活不要派过去。
- 没有合适的空间就不要硬派：直接告诉用户原因，建议新建一个什么样的空间。
- 任务本身说不清楚时先问用户，不要猜。
- 同一个空间同一时间只派一个任务；计划被拒绝后不要绕开计划私自派发，按用户的意见改了再提交。"""


def _where(space: Space) -> str:
    if space.kind == "agent" and space.cwd:
        return space.cwd
    return "临时目录（通用任务，没有固定项目）"


def _ability(space: Space) -> str:
    """这个空间能干什么：内置执行者读写都行（写要人批准），外部 CLI 看权限档。"""
    executor = EXECUTOR_LABELS.get(space.executor, space.executor)
    if space.executor == "simpleagent":
        return f"{executor}；能读写文件、跑命令（写操作要用户批准）"
    if space.permission == "full":
        return f"{executor}；全放行：能改文件、跑命令，不经确认"
    return f"{executor}；只读：只能看文件，不能改、不能跑命令"


def spaces_section(spaces: list[Space]) -> str:
    if not spaces:
        return "# 可用的空间\n（还没有任何空间。告诉用户先在左栏新建空间。）"
    lines = ["# 可用的空间"]
    for space in spaces:
        description = space.description or "（没写简介，只能从名字和目录判断）"
        lines += [
            f"- **{space.name}**（id `{space.id}`）",
            f"  - 简介：{description}",
            f"  - 执行者：{_ability(space)}",
            f"  - 目录：{_where(space)}",
        ]
    return "\n".join(lines)


def command_prompt(spaces: list[Space], now: datetime | None = None) -> str:
    now = now or datetime.now()
    weekday = "一二三四五六日"[now.weekday()]
    return (
        f"{COMMAND_PROMPT}\n\n"
        f"# 环境\n- 日期：{now:%Y-%m-%d}（星期{weekday}）\n\n"
        f"{spaces_section(spaces)}"
    )
