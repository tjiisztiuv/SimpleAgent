"""指挥台调度者端到端：Runner + FakeLLM，不联网。

调度者和子任务用不同的 profile，工厂按 profile 名发脚本：cmd 给调度者，x / y 给两个目标空间。
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from typing import Any

import pytest

from simpleagent.config import Config
from simpleagent.llm.fake import FakeLLM
from simpleagent.serve.app import Server
from simpleagent.serve.runner import Runner
from simpleagent.spaces.models import COMMAND_SPACE_ID, SpaceSpec
from simpleagent.spaces.store import SpaceStore

FINAL = ("done", "error", "cancelled")


@pytest.fixture
def cfg(sa_home) -> Config:
    return Config.model_validate(
        {
            "default_profile": "x",
            "profiles": {
                name: {"base_url": f"http://{name}.invalid/v1", "model": f"model-{name}"}
                for name in ("cmd", "x", "y")
            },
            "command": {"profile": "cmd"},
        }
    )


class Factory:
    """每次构造 LLM 从对应 profile 的队列里取一份脚本（一轮对话一份）。"""

    def __init__(self, scripts: dict[str, list[list[Any]]]) -> None:
        self.scripts = scripts
        self.made: dict[str, list[FakeLLM]] = defaultdict(list)

    def __call__(self, name: str, profile: Any) -> FakeLLM:
        llm = FakeLLM(self.scripts[name].pop(0))
        self.made[name].append(llm)
        return llm


def _tool_call(name: str, args: dict, call_id: str) -> dict:
    return {"id": call_id, "name": name, "arguments": args}


def _wait_for(pred, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if got := pred():
            return got
        time.sleep(0.05)
    raise AssertionError("等待超时")


def _setup(cfg, sa_home, scripts):
    store = SpaceStore(sa_home)
    store.ensure_command_space()
    work = store.create_space(SpaceSpec(name="work", kind="generic", profile="x"))
    home = store.create_space(SpaceSpec(name="home", kind="generic", profile="y"))
    factory = Factory(scripts)
    runner = Runner(cfg, store=store, llm_factory=factory)
    runner.start()
    coord = store.create_session(COMMAND_SPACE_ID)
    return store, work, home, factory, runner, coord


def _children(store: SpaceStore, space_id: str, parent: str) -> list:
    return [m for m in store.list_sessions(space_id, limit=50) if m.parent_session_id == parent]


def _tool_names(llm: FakeLLM) -> set[str]:
    return {t["function"]["name"] for t in llm.requests[0]["tools"] or []}


def test_single_space_dispatch_end_to_end(cfg, sa_home):
    scripts = {
        "cmd": [
            [
                {"tool_calls": [_tool_call("dispatch", {"space": "work", "task": "算 6×7"}, "c1")]},
                {"content": "work 算出来是 42"},
            ]
        ],
        "x": [[{"content": "结果是 42"}]],
    }
    store, work, _, factory, runner, coord = _setup(cfg, sa_home, scripts)
    q, _ = runner.bus.subscribe(coord.id)
    runner.run_input(COMMAND_SPACE_ID, coord.id, "让 work 算一下 6×7")
    while (f := q.get(timeout=10)).type != "status" or f.payload["status"] not in FINAL:
        pass
    runner.shutdown()

    assert f.payload["status"] == "done"
    # 收口结果只给调度者在等的子会话记，取走后清空：调度者自己的这一轮不该留在里面
    assert runner._outcomes == {}
    [child] = _children(store, work.id, coord.id)
    assert child.status == "done"
    child_msgs = store.load_session(work.id, child.id).messages
    assert child_msgs[0] == {"role": "user", "content": "算 6×7"}

    tool_msgs = [
        m for m in store.load_session(COMMAND_SPACE_ID, coord.id).messages if m["role"] == "tool"
    ]
    assert "[work] 完成" in tool_msgs[0]["content"] and "结果是 42" in tool_msgs[0]["content"]

    # 调度者只有两个调度工具，prompt 里有空间清单；子会话拿不到 dispatch
    [coord_llm] = factory.made["cmd"]
    assert _tool_names(coord_llm) == {"propose_plan", "dispatch"}
    system = coord_llm.requests[0]["messages"][0]["content"]
    assert "**work**" in system and "**home**" in system and "sp_command" not in system
    [child_llm] = factory.made["x"]
    assert "dispatch" not in _tool_names(child_llm)
    assert "write_file" in _tool_names(child_llm)


def test_cross_space_needs_plan_approval(cfg, sa_home):
    plan = {
        "summary": "两边各算一半",
        "steps": [{"space": "work", "task": "算 A"}, {"space": "home", "task": "算 B"}],
    }
    scripts = {
        "cmd": [
            [
                {"tool_calls": [_tool_call("propose_plan", plan, "p1")]},
                {
                    "tool_calls": [
                        _tool_call("dispatch", {"space": "work", "task": "算 A"}, "d1"),
                        _tool_call("dispatch", {"space": "home", "task": "算 B"}, "d2"),
                    ]
                },
                {"content": "A、B 都算完了"},
            ]
        ],
        "x": [[{"content": "A=1"}]],
        "y": [[{"content": "B=2"}]],
    }
    store, work, home, _, runner, coord = _setup(cfg, sa_home, scripts)
    q, _ = runner.bus.subscribe(coord.id)
    runner.run_input(COMMAND_SPACE_ID, coord.id, "两边一起算")

    while (f := q.get(timeout=10)).type != "approval_request":
        pass
    assert f.payload["tool_name"] == "propose_plan"
    sent = json.loads(f.payload["arguments"])
    assert [s["space_id"] for s in sent["steps"]] == [work.id, home.id]
    # 没批准之前一个子会话都没有
    assert not _children(store, work.id, coord.id) and not _children(store, home.id, coord.id)
    # 待审批列表里挂在调度者会话上，面板靠它显示计划卡
    assert runner.pending.details()[0]["session_id"] == coord.id

    runner.approve(f.payload["approval_id"], "allow")
    while (f := q.get(timeout=10)).type != "status" or f.payload["status"] not in FINAL:
        pass
    runner.shutdown()

    assert f.payload["status"] == "done"
    assert [m.status for m in _children(store, work.id, coord.id)] == ["done"]
    assert [m.status for m in _children(store, home.id, coord.id)] == ["done"]
    results = [
        m["content"]
        for m in store.load_session(COMMAND_SPACE_ID, coord.id).messages
        if m["role"] == "tool"
    ]
    assert "用户已确认" in results[0]
    assert "A=1" in results[1] and "B=2" in results[2]


def test_cancel_commander_cancels_child(cfg, sa_home):
    scripts = {
        "cmd": [
            [{"tool_calls": [_tool_call("dispatch", {"space": "work", "task": "慢活"}, "c1")]}]
        ],
        "x": [[{"content": "慢吞吞", "delay": 5}]],
    }
    store, work, _, _, runner, coord = _setup(cfg, sa_home, scripts)
    q, _ = runner.bus.subscribe(coord.id)
    runner.run_input(COMMAND_SPACE_ID, coord.id, "干个慢活")
    child = _wait_for(
        lambda: next(
            (m for m in _children(store, work.id, coord.id) if m.status == "running"), None
        )
    )

    runner.cancel(coord.id)
    while (f := q.get(timeout=10)).type != "status" or f.payload["status"] not in FINAL:
        pass
    assert f.payload["status"] == "cancelled"
    _wait_for(lambda: store.get_session_meta(work.id, child.id).status == "cancelled")
    assert runner._outcomes == {}  # 取消的路径也要把登记取走
    runner.shutdown()


def test_child_failure_reaches_commander(cfg, sa_home):
    """子任务失败（这里是 profile 不存在起不来）：原因要回到调度者手里，由它如实汇报。"""
    scripts = {
        "cmd": [
            [
                {"tool_calls": [_tool_call("dispatch", {"space": "broken", "task": "x"}, "c1")]},
                {"content": "broken 没跑起来"},
            ]
        ],
    }
    store, _, _, _, runner, coord = _setup(cfg, sa_home, scripts)
    store.create_space(SpaceSpec(name="broken", kind="generic", profile="nope"))
    q, _ = runner.bus.subscribe(coord.id)
    runner.run_input(COMMAND_SPACE_ID, coord.id, "让 broken 干活")
    while (f := q.get(timeout=10)).type != "status" or f.payload["status"] not in FINAL:
        pass
    runner.shutdown()
    [result] = [
        m["content"]
        for m in store.load_session(COMMAND_SPACE_ID, coord.id).messages
        if m["role"] == "tool"
    ]
    assert "[broken] 失败" in result and "nope" in result


def test_targets_skip_closed_spaces_and_commander(cfg, sa_home):
    store = SpaceStore(sa_home)
    store.ensure_command_space()
    keep = store.create_space(SpaceSpec(name="keep", kind="generic", profile="x"))
    gone = store.create_space(SpaceSpec(name="gone", kind="generic", profile="x"))
    store.close_space(gone.id)
    runner = Runner(cfg, store=store)
    assert [s.id for s in runner.targets()] == [keep.id]


def test_server_command_space(cfg, sa_home):
    server = Server(cfg, store=SpaceStore(sa_home))
    server.start()
    try:
        meta = server.handle("GET", "/api/meta", {}, b"").body
        assert meta["command_space_id"] == COMMAND_SPACE_ID
        # 系统空间：左栏不显示、不能改、不能删，但能读（前端要显示调度会话的头部）
        listed = [s["id"] for s in server.handle("GET", "/api/spaces", {}, b"").body]
        assert COMMAND_SPACE_ID not in listed
        detail = server.handle("GET", f"/api/spaces/{COMMAND_SPACE_ID}", {}, b"")
        assert detail.status == 200 and detail.body["name"] == "指挥台"
        patch = json.dumps({"name": "x"}).encode()
        assert server.handle("PATCH", f"/api/spaces/{COMMAND_SPACE_ID}", {}, patch).status == 400
        assert server.handle("DELETE", f"/api/spaces/{COMMAND_SPACE_ID}", {}, b"").status == 400

        # 面板摘要带上 parent_session_id，子会话卡片才挂得到调度者下面
        store = server.store
        work = store.create_space(SpaceSpec(name="work", kind="generic", profile="x"))
        coord = store.create_session(COMMAND_SPACE_ID)
        child = store.create_session(work.id, parent=coord.id)
        store.update_meta(work.id, child.id, status="running")
        running = server.handle("GET", "/api/panel/summary", {}, b"").body["running"]
        assert {r["session_id"]: r["parent_session_id"] for r in running} == {child.id: coord.id}
        card = server.handle("GET", f"/api/sessions/{child.id}/summary", {}, b"").body
        assert card["parent_session_id"] == coord.id
    finally:
        server.runner.shutdown()


def test_command_profile_must_exist(sa_home):
    with pytest.raises(ValueError, match="command.profile"):
        Config.model_validate(
            {
                "default_profile": "x",
                "profiles": {"x": {"base_url": "http://x.invalid/v1", "model": "m"}},
                "command": {"profile": "nope"},
            }
        )
