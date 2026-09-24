"""调度者的工具：跨空间先确认、计划外不派、同一空间不并发、并行派发、prompt 里的空间清单。

用假的 Dispatcher，不起 Runner、不调模型。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from simpleagent.command import ChildResult, command_prompt, command_tools, spaces_section
from simpleagent.permissions import ApprovalDecision, ApprovalRequest
from simpleagent.spaces.models import Space
from simpleagent.tools import ToolContext, ToolRegistry


def _space(sid: str, name: str, **kw: Any) -> Space:
    return Space(id=sid, name=name, kind=kw.pop("kind", "generic"), **kw)


SPACES = [
    _space("sp_a", "finance", kind="agent", cwd="/p/finance", description="记账和理财数据"),
    _space("sp_b", "code", kind="agent", cwd="/p/code", executor="claude-code"),
    _space("sp_c", "main"),
]


class FakeDispatcher:
    def __init__(self, gate: bool = False) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.started: list[str] = []
        # gate=True：每个子任务都要等「两个都开始了」才返回——串行执行的话会卡住
        self.both_started = asyncio.Event() if gate else None

    def targets(self) -> list[Space]:
        return list(SPACES)

    async def run_child(self, space_id: str, task: str, *, parent: str) -> ChildResult:
        self.calls.append((space_id, task, parent))
        self.started.append(space_id)
        await asyncio.sleep(0.01)  # 真的子任务一定会挂起：跑模型、跑 CLI 都要等
        if self.both_started is not None:
            if len(self.started) >= 2:
                self.both_started.set()
            await asyncio.wait_for(self.both_started.wait(), timeout=2)
        name = next(s.name for s in SPACES if s.id == space_id)
        return ChildResult(space_id, name, f"se_{space_id}", "done", reply=f"{name} 做完了")


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
