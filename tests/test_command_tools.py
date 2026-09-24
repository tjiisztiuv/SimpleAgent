"""调度者的工具：跨空间先确认、计划外不派、同一空间不并发、并行派发、prompt 里的空间清单，
以及查会话（recent_sessions）和追问已有会话（followup）。

用假的 Dispatcher，不起 Runner、不调模型。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any

from simpleagent.command import (
    ChildResult,
    SessionBrief,
    command_prompt,
    command_tools,
    spaces_section,
)
from simpleagent.permissions import ApprovalDecision, ApprovalRequest
from simpleagent.spaces.models import COMMAND_SPACE_ID, Space
from simpleagent.tools import ToolContext, ToolError, ToolRegistry


def _space(sid: str, name: str, **kw: Any) -> Space:
    return Space(id=sid, name=name, kind=kw.pop("kind", "generic"), **kw)


SPACES = [
    _space("sp_a", "finance", kind="agent", cwd="/p/finance", description="记账和理财数据"),
    _space("sp_b", "code", kind="agent", cwd="/p/code", executor="claude-code"),
    _space("sp_c", "main"),
]


def _brief(sid: str, space_id: str, space_name: str, **kw: Any) -> SessionBrief:
    return SessionBrief(
        space_id=space_id,
        space_name=space_name,
        session_id=sid,
        title=kw.pop("title", "某个任务"),
        status=kw.pop("status", "done"),
        updated_at=kw.pop("updated_at", ""),
        **kw,
    )


BRIEFS = [
    _brief("se_a1", "sp_a", "finance", title="9 月开销报告", dispatched=True, last_text="写好了"),
    _brief("se_b1", "sp_b", "code", title="修登录", status="running", busy=True),
    _brief("se_b2", "sp_b", "code", title="加测试"),
    _brief("se_c1", "sp_c", "main", title="旧会话", locked=True),
    _brief("se_gone", "sp_gone", "archive"),  # 空间已经关掉了
    _brief("se_cmd_old", COMMAND_SPACE_ID, "指挥台"),
]


class FakeDispatcher:
    def __init__(self, gate: bool = False, busy_on_claim: bool = False) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.followups: list[tuple[str, str, str]] = []  # (空间, 会话, 追问的话)
        self.started: list[str] = []
        self.listed: list[tuple[str | None, int]] = []
        # gate=True：每个子任务都要等「两个都开始了」才返回——串行执行的话会卡住
        self.both_started = asyncio.Event() if gate else None
        # busy_on_claim=True：模拟工具查过之后、真正开跑之前，会话被别人占了
        self.busy_on_claim = busy_on_claim

    def targets(self) -> list[Space]:
        return list(SPACES)

    def sessions(self, space_id: str | None = None, limit: int = 10) -> list[SessionBrief]:
        self.listed.append((space_id, limit))
        opened = {s.id for s in SPACES}
        found = [b for b in BRIEFS if b.space_id in opened]
        return [b for b in found if space_id is None or b.space_id == space_id][:limit]

    def find_session(self, session_id: str) -> SessionBrief | None:
        return next((b for b in BRIEFS if b.session_id == session_id), None)

    async def run_child(
        self, space_id: str, task: str, *, parent: str, session_id: str | None = None
    ) -> ChildResult:
        if session_id is not None and self.busy_on_claim:
            raise ToolError(f"会话 {session_id} 正在跑，等它这一轮结束再追问")
        if session_id is None:
            self.calls.append((space_id, task, parent))
        else:
            self.followups.append((space_id, session_id, task))
        self.started.append(space_id)
        await asyncio.sleep(0.01)  # 真的子任务一定会挂起：跑模型、跑 CLI 都要等
        if self.both_started is not None:
            if len(self.started) >= 2:
                self.both_started.set()
            await asyncio.wait_for(self.both_started.wait(), timeout=2)
        name = next(s.name for s in SPACES if s.id == space_id)
        return ChildResult(
            space_id,
            name,
            session_id or f"se_{space_id}",
            "done",
            reply=f"{name} 做完了",
            followup=session_id is not None,
        )


class FakeApprover:
    def __init__(self, allow: bool) -> None:
        self.allow = allow
        self.requests: list[ApprovalRequest] = []

    async def request(self, req: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(req)
        return ApprovalDecision(allow=self.allow)


def _registry(dispatcher, approver=None) -> ToolRegistry:
    return ToolRegistry(command_tools(dispatcher, parent="se_cmd", approver=approver))


def _call(name: str, args: dict, call_id: str = "c") -> dict:
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(args)}}


CTX = ToolContext(cwd=__import__("pathlib").Path("."), session_id="se_cmd")


async def _run(reg: ToolRegistry, *calls: dict) -> list:
    results = []
    async for batch in reg.execute_many(list(calls), CTX):
        results += batch
    return results


async def test_single_space_dispatch_goes_straight():
    d = FakeDispatcher()
    [res] = await _run(_registry(d), _call("dispatch", {"space": "finance", "task": "算账"}))
    assert not res.is_error
    assert "[finance] 完成 · 子会话 se_sp_a" in res.content
    assert "finance 做完了" in res.content
    assert d.calls == [("sp_a", "算账", "se_cmd")]


async def test_second_space_without_plan_is_rejected():
    d = FakeDispatcher()
    reg = _registry(d)
    await _run(reg, _call("dispatch", {"space": "finance", "task": "1"}))
    # 同一个空间再派没问题
    [again] = await _run(reg, _call("dispatch", {"space": "finance", "task": "2"}))
    assert not again.is_error
    [res] = await _run(reg, _call("dispatch", {"space": "code", "task": "3"}))
    assert res.is_error and "propose_plan" in res.content
    assert [c[0] for c in d.calls] == ["sp_a", "sp_a"]


async def test_parallel_dispatch_without_plan_only_first_passes():
    """同一批并行派给两个空间：第一个放行，第二个被拦——检查不能被并行绕过去。"""
    d = FakeDispatcher()
    results = await _run(
        _registry(d),
        _call("dispatch", {"space": "finance", "task": "1"}, "c1"),
        _call("dispatch", {"space": "code", "task": "2"}, "c2"),
    )
    assert not results[0].is_error
    assert results[1].is_error and "propose_plan" in results[1].content
    assert [c[0] for c in d.calls] == ["sp_a"]


async def test_plan_approved_then_parallel_dispatch():
    d = FakeDispatcher(gate=True)
    approver = FakeApprover(allow=True)
    reg = _registry(d, approver)
    plan = {
        "summary": "先查账再改代码",
        "steps": [
            {"space": "finance", "task": "查账"},
            {"space": "code", "task": "改代码"},
            {"space": "main", "task": "写总结", "after": [1, 2]},
        ],
    }
    [ok] = await _run(reg, _call("propose_plan", plan))
    assert not ok.is_error and "用户已确认" in ok.content
    sent = json.loads(approver.requests[0].arguments)
    assert approver.requests[0].session_id == "se_cmd"
    assert [s["space_id"] for s in sent["steps"]] == ["sp_a", "sp_b", "sp_c"]
    assert sent["steps"][2]["after"] == [1, 2]

    # 两个 dispatch 在同一批：FakeDispatcher 的闸门要求两者同时在跑，串行就会超时报错
    results = await _run(
        reg,
        _call("dispatch", {"space": "finance", "task": "查账"}, "c1"),
        _call("dispatch", {"space": "code", "task": "改代码"}, "c2"),
    )
    assert [r.is_error for r in results] == [False, False]
    assert sorted(d.started) == ["sp_a", "sp_b"]


async def test_plan_denied_blocks_dispatch_to_second_space():
    d = FakeDispatcher()
    reg = _registry(d, FakeApprover(allow=False))
    plan = {"steps": [{"space": "finance", "task": "1"}, {"space": "code", "task": "2"}]}
    [res] = await _run(reg, _call("propose_plan", plan))
    assert res.is_error and "没有批准" in res.content
    await _run(reg, _call("dispatch", {"space": "finance", "task": "1"}))
    [second] = await _run(reg, _call("dispatch", {"space": "code", "task": "2"}))
    assert second.is_error


async def test_space_outside_plan_is_rejected():
    d = FakeDispatcher()
    reg = _registry(d, FakeApprover(allow=True))
    plan = {"steps": [{"space": "finance", "task": "1"}, {"space": "code", "task": "2"}]}
    await _run(reg, _call("propose_plan", plan))
    [res] = await _run(reg, _call("dispatch", {"space": "main", "task": "3"}))
    assert res.is_error and "不在已确认的计划里" in res.content


async def test_invalid_plan_never_reaches_the_user():
    approver = FakeApprover(allow=True)
    reg = _registry(FakeDispatcher(), approver)
    [unknown] = await _run(reg, _call("propose_plan", {"steps": [{"space": "nope", "task": "x"}]}))
    assert unknown.is_error and "没有叫「nope」" in unknown.content
    forward = {"steps": [{"space": "finance", "task": "1", "after": [2]}]}
    [bad] = await _run(reg, _call("propose_plan", forward))
    assert bad.is_error and "after" in bad.content
    assert approver.requests == []


async def test_unattended_plan_is_refused():
    reg = _registry(FakeDispatcher(), approver=None)
    plan = {"steps": [{"space": "finance", "task": "1"}, {"space": "code", "task": "2"}]}
    [res] = await _run(reg, _call("propose_plan", plan))
    assert res.is_error and "无人值守" in res.content


async def test_same_space_twice_in_one_batch_is_rejected():
    d = FakeDispatcher()
    results = await _run(
        _registry(d),
        _call("dispatch", {"space": "finance", "task": "1"}, "c1"),
        _call("dispatch", {"space": "finance", "task": "2"}, "c2"),
    )
    assert not results[0].is_error
    assert results[1].is_error and "还没跑完" in results[1].content


async def test_resolve_by_id_case_and_duplicates():
    d = FakeDispatcher()
    [by_id] = await _run(_registry(d), _call("dispatch", {"space": "sp_b", "task": "x"}))
    assert not by_id.is_error
    [upper] = await _run(_registry(d), _call("dispatch", {"space": "@FINANCE", "task": "x"}))
    assert not upper.is_error
    [self_] = await _run(_registry(d), _call("dispatch", {"space": "sp_command", "task": "x"}))
    assert self_.is_error

    SPACES.append(_space("sp_d", "code"))
    try:
        [dup] = await _run(_registry(d), _call("dispatch", {"space": "code", "task": "x"}))
    finally:
        SPACES.pop()
    assert dup.is_error and "sp_b" in dup.content and "sp_d" in dup.content


def test_child_result_render_truncates_and_reports_failure():
    long = ChildResult("sp_a", "finance", "se_1", "done", reply="字" * 5000, files=["a.py"])
    text = long.render()
    assert "改动文件：a.py" in text and "全文在子会话 se_1" in text
    assert len(text) < 4200
    failed = ChildResult("sp_a", "finance", "se_2", "error", reason="Not logged in").render()
    assert "失败" in failed and "原因：Not logged in" in failed


def test_prompt_lists_spaces_with_abilities():
    text = command_prompt(SPACES)
    assert "**finance**（id `sp_a`）" in text and "记账和理财数据" in text
    assert "只读：只能看文件" in text  # code 是 safe 档的 claude-code
    assert "没写简介" in text
    assert "临时目录" in text  # main 是通用空间
    assert "还没有任何空间" in spaces_section([])


# ------------------------------------------------------------ 追问已有的会话
async def test_followup_continues_existing_session():
    d = FakeDispatcher()
    [res] = await _run(
        _registry(d), _call("followup", {"session": "`se_a1`", "message": "再加一张图"})
    )
    assert not res.is_error
    assert "[finance] 完成（追问） · 子会话 se_a1" in res.content
    assert d.followups == [("sp_a", "se_a1", "再加一张图")]
    assert d.calls == []  # 没有新建会话


async def test_followup_counts_as_dispatch_for_cross_space():
    """追问也算「派给这个空间」：和 dispatch 共用一道关，跨空间照样要先有计划。"""
    d = FakeDispatcher()
    reg = _registry(d)
    await _run(reg, _call("dispatch", {"space": "finance", "task": "1"}))
    [res] = await _run(reg, _call("followup", {"session": "se_b2", "message": "2"}))
    assert res.is_error and "propose_plan" in res.content

    # 反过来：先追问 finance，同一批里再派 code，第二个被拦
    d2 = FakeDispatcher()
    results = await _run(
        _registry(d2),
        _call("followup", {"session": "se_a1", "message": "1"}, "c1"),
        _call("dispatch", {"space": "code", "task": "2"}, "c2"),
    )
    assert not results[0].is_error
    assert results[1].is_error and "propose_plan" in results[1].content


async def test_followup_and_dispatch_same_space_in_one_batch():
    d = FakeDispatcher()
    results = await _run(
        _registry(d),
        _call("dispatch", {"space": "finance", "task": "1"}, "c1"),
        _call("followup", {"session": "se_a1", "message": "2"}, "c2"),
    )
    assert not results[0].is_error
    assert results[1].is_error and "还没跑完" in results[1].content


async def test_followup_inside_approved_plan():
    d = FakeDispatcher(gate=True)
    reg = _registry(d, FakeApprover(allow=True))
    plan = {"steps": [{"space": "finance", "task": "改报告"}, {"space": "code", "task": "加测试"}]}
    await _run(reg, _call("propose_plan", plan))
    results = await _run(
        reg,
        _call("followup", {"session": "se_a1", "message": "改报告"}, "c1"),
        _call("followup", {"session": "se_b2", "message": "加测试"}, "c2"),
    )
    assert [r.is_error for r in results] == [False, False]
    assert sorted(f[1] for f in d.followups) == ["se_a1", "se_b2"]


async def test_followup_refuses_sessions_it_cannot_continue():
    d = FakeDispatcher()
    cases = {
        "se_nope": "recent_sessions",  # 不存在：提示先查
        "se_cmd_old": "调度会话",  # 指挥台自己的会话
        "se_gone": "已经关掉",  # 空间关了
        "se_c1": "锁住",  # 空间切过执行者
        "se_b1": "正在跑",  # 这一轮还没结束
    }
    for ref, word in cases.items():
        [res] = await _run(_registry(d), _call("followup", {"session": ref, "message": "x"}))
        assert res.is_error and word in res.content, (ref, res.content)
    assert d.followups == [] and d.started == []


async def test_followup_busy_at_claim_frees_the_space():
    """查的时候没在跑、真正开跑时被占了：报错给模型，这个空间在本轮里也不能一直算「在跑」。"""
    d = FakeDispatcher(busy_on_claim=True)
    reg = _registry(d)
    [res] = await _run(reg, _call("followup", {"session": "se_a1", "message": "x"}))
    assert res.is_error and "正在跑" in res.content
    [again] = await _run(reg, _call("dispatch", {"space": "finance", "task": "新开一个"}))
    assert not again.is_error


async def test_recent_sessions_lists_and_filters():
    d = FakeDispatcher()
    [res] = await _run(_registry(d), _call("recent_sessions", {}))
    assert not res.is_error
    text = res.content
    assert "`se_a1` [finance] 9 月开销报告 · 完成" in text and "调度派发" in text
    assert "最后回复：写好了" in text
    assert "正在跑（现在不能追问）" in text  # se_b1
    assert "已锁定" in text  # se_c1
    # 关掉的空间、指挥台自己的会话不列
    assert "se_gone" not in text and "se_cmd_old" not in text
    assert d.listed == [(None, 10)]

    [only] = await _run(_registry(d), _call("recent_sessions", {"space": "code", "limit": 5}))
    assert "se_b2" in only.content and "se_a1" not in only.content
    assert d.listed[-1] == ("sp_b", 5)

    [too_many] = await _run(_registry(d), _call("recent_sessions", {"limit": 99}))
    assert too_many.is_error
    [unknown] = await _run(_registry(d), _call("recent_sessions", {"space": "nope"}))
    assert unknown.is_error and "没有叫「nope」" in unknown.content


async def test_recent_sessions_empty():
    d = FakeDispatcher()
    d.sessions = lambda space_id=None, limit=10: []  # type: ignore[method-assign]
    [res] = await _run(_registry(d), _call("recent_sessions", {"space": "main"}))
    assert not res.is_error and "没有找到会话" in res.content


def test_session_brief_render():
    now = datetime(2026, 9, 24, 12, 0).astimezone()
    ago = (now - timedelta(minutes=3)).isoformat()
    brief = _brief("se_1", "sp_a", "finance", title="报告", updated_at=ago)
    assert brief.render(now) == "- `se_1` [finance] 报告 · 完成 · 3 分钟前 · 直接发起"
    stale = _brief("se_2", "sp_a", "finance", status="running", updated_at="坏的时间")
    assert "上次没跑完" in stale.render(now) and "前" not in stale.render(now)
    fresh = _brief("se_3", "sp_a", "finance", status="idle")
    assert "还没跑过" in fresh.render(now)


def test_child_result_render_marks_followup():
    text = ChildResult("sp_a", "finance", "se_1", "done", reply="好了", followup=True).render()
    assert text.startswith("[finance] 完成（追问） · 子会话 se_1")
