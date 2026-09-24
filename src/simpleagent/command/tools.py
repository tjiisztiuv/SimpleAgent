"""调度者的 4 个工具：

    propose_plan    跨空间先把计划交给用户确认
    dispatch        派给空间：新建子会话，等它跑完
    recent_sessions 列出打开的空间里最近的会话（只读），追问前靠它找会话
    followup        在已有会话里接着问（沿用它的上下文），等这一轮跑完

    command_tools(dispatcher, parent=..., approver=...)  每轮对话造一份，状态只活在这一轮

「跨空间先确认」由这里的代码保证，不只靠 prompt。dispatch 和 followup 走同一道关（admit）：
- 本轮第一个空间随便派；要派到第二个不同的空间，而本轮还没有批准过的计划，直接报错；
- 计划批准之后，派到计划外的空间也报错；
- 同一个空间上一个子任务还没跑完，不许再派（同一个目录里两个 agent 同时写会互相踩）。

一轮 = 用户说一句话到调度者回完。计划的审批会挂起 propose_plan 这次调用，用户点了才继续，
所以「提计划 → 确认 → 按计划派」都在同一轮里，状态不用跨轮保存。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, Field

from simpleagent.permissions import ApprovalRequest, Approver
from simpleagent.spaces.models import COMMAND_SPACE_ID, Space
from simpleagent.tools.base import Tool, ToolContext, ToolError

# 子任务最后一条回复回给调度者时最多带多少字：全文在子会话里，调度者只要结论
REPLY_LIMIT = 4000

# recent_sessions 最多列几个：每行都进调度者的上下文
RECENT_MAX = 30

STATUS_WORDS = {"done": "完成", "error": "失败", "cancelled": "已取消"}
VERIFY_WORDS = {"passed": "通过", "failed": "未通过", "stale": "已失效", "running": "进行中"}


@dataclass
class ChildResult:
    """一个子任务的结果：调度者据此决定下一步，也照原样汇总给用户。"""

    space_id: str
    space_name: str
    session_id: str
    status: str  # done | error | cancelled
    reply: str = ""  # 子会话最后一条助手消息
    reason: str | None = None  # 失败原因
    files: list[str] = field(default_factory=list)  # 改动过的文件
    verification: str = "unknown"
    followup: bool = False  # 这一轮是追问已有的会话，不是新派的

    def render(self) -> str:
        head = STATUS_WORDS.get(self.status, self.status)
        if self.followup:
            head += "（追问）"
        lines = [f"[{self.space_name}] {head} · 子会话 {self.session_id}"]
        if self.reason:
            lines.append(f"原因：{self.reason}")
        if self.files:
            lines.append(f"改动文件：{'、'.join(self.files)}")
        if self.verification in VERIFY_WORDS:
            lines.append(f"验证：{VERIFY_WORDS[self.verification]}")
        reply = self.reply.strip()
        if reply:
            if len(reply) > REPLY_LIMIT:
                reply = (
                    reply[:REPLY_LIMIT].rstrip()
                    + f"\n…（回复太长，只给了开头；全文在子会话 {self.session_id}）"
                )
            lines += ["回复：", reply]
        elif self.status == "done":
            lines.append("（子任务没有给出文字回复）")
        return "\n".join(lines)


@dataclass
class SessionBrief:
    """recent_sessions 列出来的一个会话，也是 followup 追问前检查用的。"""

    space_id: str
    space_name: str
    session_id: str
    title: str
    status: str  # meta 里的状态：idle | running | done | error | cancelled
    updated_at: str
    dispatched: bool = False  # 是调度者派出来的（parent_session_id 不为空）
    locked: bool = False  # 空间切过执行者，历史接不上，不能追问
    busy: bool = False  # 正在跑一轮
    last_text: str = ""  # 最后一条助手回复的开头

    def render(self, now: datetime | None = None) -> str:
        if self.busy:
            state = "正在跑（现在不能追问）"
        elif self.status == "running":
            state = "上次没跑完"  # meta 停在 running 但没人在跑：serve 中途重启过
        elif self.status == "idle":
            state = "还没跑过"
        else:
            state = STATUS_WORDS.get(self.status, self.status)
        parts = [f"`{self.session_id}` [{self.space_name}] {self.title}", state]
        if ago := _ago(self.updated_at, now):
            parts.append(ago)
        parts.append("调度派发" if self.dispatched else "直接发起")
        if self.locked:
            parts.append("已锁定（空间切过执行者，不能追问）")
        lines = ["- " + " · ".join(parts)]
        if self.last_text:
            lines.append(f"  最后回复：{self.last_text}")
        return "\n".join(lines)


def _ago(ts: str, now: datetime | None = None) -> str:
    """「3 分钟前」这种说法：用户说「刚才那个」时，调度者要靠它对上号。"""
    try:
        then = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return ""
    now = now or datetime.now(then.tzinfo)
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86400} 天前"


class Dispatcher(Protocol):
    """工具需要的调度能力。Runner 实现它；测试里可以换成假的。"""

    def targets(self) -> list[Space]:
        """现在能派的空间（不含指挥台自己）。"""
        ...

    def sessions(self, space_id: str | None = None, limit: int = 10) -> list[SessionBrief]:
        """打开着的空间里最近的会话，新的在前（不含指挥台）；给了 space_id 就只看那个空间。"""
        ...

    def find_session(self, session_id: str) -> SessionBrief | None:
        """按 id 找会话，不管在哪个空间；找不到返回 None。"""
        ...

    async def run_child(
        self, space_id: str, task: str, *, parent: str, session_id: str | None = None
    ) -> ChildResult:
        """在目标空间跑 task，等它跑完（或被取消）再返回。

        session_id 为空就新建子会话；不为空就在这个已有会话里接着跑，结果只统计这一轮。
        会话正在跑（刚被别人占上）时抛 ToolError。
        """
        ...


def resolve_space(targets: list[Space], ref: str) -> Space:
    """按 id 或名字找空间：先 id，再名字精确匹配，再忽略大小写。重名要求用 id。"""
    ref = ref.strip().lstrip("@")
    if ref == COMMAND_SPACE_ID:
        raise ToolError("不能派给指挥台自己")
    for space in targets:
        if space.id == ref:
            return space
    named = [s for s in targets if s.name == ref] or [
        s for s in targets if s.name.lower() == ref.lower()
    ]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        ids = "、".join(s.id for s in named)
        raise ToolError(f"有 {len(named)} 个空间都叫「{ref}」，请改用 id：{ids}")
    names = "、".join(s.name for s in targets) or "（没有可用的空间）"
    raise ToolError(f"没有叫「{ref}」的空间。现在能派的：{names}")


class DispatchArgs(BaseModel):
    space: str = Field(description="派给哪个空间：空间名（重名时用 id）")
    task: str = Field(
        min_length=1,
        description=(
            "交给这个空间的完整任务描述。子任务看不到指挥台的对话和别的空间的结果，"
            "要写清楚目标、已知信息和要交付什么"
        ),
    )


class RecentArgs(BaseModel):
    space: str | None = Field(
        None, description="只看这个空间：空间名（重名时用 id）；不填看所有打开的空间"
    )
    limit: int = Field(10, ge=1, le=RECENT_MAX, description="最多列几个，按最近更新排序")


class FollowupArgs(BaseModel):
    session: str = Field(
        min_length=1, description="要追问的会话 id：从 recent_sessions 或之前派发的结果里拿"
    )
    message: str = Field(
        min_length=1,
        description=(
            "接着对这个会话说的话。它记得之前的上下文，只写新的要求；"
            "但它看不到指挥台的对话，用户在这边补充的信息要写进来"
        ),
    )


class PlanStep(BaseModel):
    space: str = Field(description="这一步派给哪个空间：空间名（重名时用 id）")
    task: str = Field(min_length=1, description="这一步要做什么，一两句话")
    after: list[int] = Field(
        default_factory=list,
        description="要等哪几步做完才能开始（步骤号从 1 开始）；空表示可以立刻开始",
    )


class PlanArgs(BaseModel):
    steps: list[PlanStep] = Field(min_length=1, description="按顺序列出的步骤")
    summary: str = Field("", description="一句话说明整体思路，给用户看")


@dataclass
class _TurnState:
    used: dict[str, str] = field(default_factory=dict)  # 本轮派过的空间 id → 名字
    plan: set[str] | None = None  # 本轮批准过的计划涉及的空间 id；None = 还没有
    running: set[str] = field(default_factory=set)  # 正在跑的空间 id


def command_tools(dispatcher: Dispatcher, *, parent: str, approver: Approver | None) -> list[Tool]:
    """调度者这一轮用的工具。parent 是调度者自己的会话 id，子会话记着它。

    approver 为 None（无人值守）时计划一律视为未批准：跨空间的事没人确认就不做。
    """
    state = _TurnState()

    async def propose_plan(args: PlanArgs, ctx: ToolContext) -> str:
        targets = dispatcher.targets()
        steps = []
        for n, step in enumerate(args.steps, start=1):
            space = resolve_space(targets, step.space)
            bad = [a for a in step.after if not 1 <= a < n]
            if bad:
                raise ToolError(
                    f"第 {n} 步的 after 只能填它前面的步骤号（1～{n - 1}），不能是 {bad}"
                )
            steps.append(
                {
                    "n": n,
                    "space": space.name,
                    "space_id": space.id,
                    "task": step.task,
                    "after": sorted(set(step.after)),
                }
            )
        # 先校验再问人：让用户批准一份派不出去的计划没有意义
        plan = {"summary": args.summary, "steps": steps}
        if approver is None:
            raise ToolError("无人值守：跨空间的计划没有人确认，不执行")
        answer = await approver.request(
            ApprovalRequest(
                session_id=parent,
                tool_name="propose_plan",
                arguments=json.dumps(plan, ensure_ascii=False),
                reason="调度者要按这份计划把任务派给多个空间",
            )
        )
        if not answer.allow:
            raise ToolError("用户没有批准这份计划。不要派发，问清楚用户想怎么改")
        state.plan = {step["space_id"] for step in steps}
        names = "、".join(dict.fromkeys(step["space"] for step in steps))
        return f"用户已确认计划，涉及空间：{names}。按计划 dispatch，计划外的空间不能派。"

    def admit(space: Space) -> None:
        """dispatch 和 followup 共用的关卡：过了就登记「本轮派过、正在跑」，没过抛 ToolError。

        调用方从检查到登记之间不能有 await：同一批并行的调用按顺序依次过这道关，并行绕不过去。
        """
        if state.plan is not None:
            if space.id not in state.plan:
                raise ToolError(
                    f"「{space.name}」不在已确认的计划里。要加步骤，先用 propose_plan 重新提交计划"
                )
        elif state.used and space.id not in state.used:
            done = "、".join(state.used.values())
            raise ToolError(
                f"本轮已经派给了「{done}」，再派给「{space.name}」就是跨空间任务："
                "先用 propose_plan 把计划交给用户确认"
            )
        if space.id in state.running:
            raise ToolError(f"「{space.name}」上一个子任务还没跑完，等它结束再派")
        state.used[space.id] = space.name
        state.running.add(space.id)

    async def dispatch(args: DispatchArgs, ctx: ToolContext) -> str:
        space = resolve_space(dispatcher.targets(), args.space)
        admit(space)
        try:
            result = await dispatcher.run_child(space.id, args.task, parent=parent)
        finally:
            state.running.discard(space.id)
        return result.render()

    async def recent_sessions(args: RecentArgs, ctx: ToolContext) -> str:
        space_id = resolve_space(dispatcher.targets(), args.space).id if args.space else None
        briefs = dispatcher.sessions(space_id, limit=args.limit)
        if not briefs:
            return "没有找到会话。" + ("这个空间还没跑过任务。" if space_id else "")
        head = "最近的会话（新的在前）。追问用 followup，session 填反引号里的 id："
        return "\n".join([head, *(b.render() for b in briefs)])

    async def followup(args: FollowupArgs, ctx: ToolContext) -> str:
        ref = args.session.strip().strip("`")
        brief = dispatcher.find_session(ref)
        if brief is None:
            raise ToolError(f"没有 id 为「{ref}」的会话。先用 recent_sessions 查一下")
        if brief.space_id == COMMAND_SPACE_ID:
            raise ToolError("调度会话不能追问，只能追问各个空间里的会话")
        space = next((s for s in dispatcher.targets() if s.id == brief.space_id), None)
        if space is None:
            raise ToolError(f"会话所在的空间「{brief.space_name}」已经关掉了，追问不了")
        if brief.locked:
            raise ToolError(
                f"这个会话被锁住了：空间「{space.name}」中途切过执行者，之前的历史接不上。"
                "要继续就用 dispatch 新开一个会话，把需要的背景写进任务描述"
            )
        if brief.busy:
            raise ToolError("这个会话正在跑，等它这一轮结束再追问")
        admit(space)
        try:
            result = await dispatcher.run_child(
                space.id, args.message, parent=parent, session_id=brief.session_id
            )
        finally:
            state.running.discard(space.id)
        return result.render()

    return [
        Tool(
            name="propose_plan",
            description=(
                "把跨空间的执行计划交给用户确认，用户点了确认才返回。要派给两个及以上的空间时，"
                "必须先调用它；确认后按计划调用 dispatch。只派一个空间时不需要。"
            ),
            args_model=PlanArgs,
            fn=propose_plan,
            # 不是只读：同一批里有它时整批按顺序执行，保证先确认、后派发
            readonly=False,
        ),
        Tool(
            name="dispatch",
            description=(
                "把一个任务派给一个空间：在那个空间新建会话去跑，跑完返回结论（状态、改动的文件、"
                "最后的回复）。同一条回复里的多个 dispatch 会并行执行。"
            ),
            args_model=DispatchArgs,
            fn=dispatch,
            # 对调度者来说派发是可以并行的：各个空间在自己的目录里跑，改不到调度者这边
            readonly=True,
        ),
        Tool(
            name="recent_sessions",
            description=(
                "列出打开的空间里最近的会话（不含指挥台）：id、空间、标题、状态、多久前、"
                "最后一句回复的开头。用户提到之前的任务、要接着改时，"
                "先用它找到会话，再用 followup。"
            ),
            args_model=RecentArgs,
            fn=recent_sessions,
            readonly=True,
        ),
        Tool(
            name="followup",
            description=(
                "在一个已有的会话里接着问：它记得之前的上下文，跑完返回这一轮的结论。"
                "和 dispatch 一样受跨空间确认的约束；正在跑或被锁住的会话不能追问。"
                "和已有会话无关的新任务用 dispatch。"
            ),
            args_model=FollowupArgs,
            fn=followup,
            # 和 dispatch 一样：跑在别的空间里，可以并行
            readonly=True,
        ),
    ]
