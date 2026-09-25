"""空间切换执行者：只影响新会话，老会话锁成只读，有会话在跑时不许切。

runner 走内置 loop 用 FakeLLM，走 CLI 用 tests/fixtures/cli/fake_cli.sh，全程不联网。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from simpleagent.llm.fake import FakeLLM
from simpleagent.serve.app import Server
from simpleagent.serve.runner import Runner
from simpleagent.spaces.models import SpaceSpec
from simpleagent.spaces.store import SpaceStore

FIXTURES = Path(__file__).parent.parent / "fixtures" / "cli"
FAKE = str(FIXTURES / "fake_cli.sh")


def _fake_factory(script: list[dict]) -> Any:
    def factory(name: str, profile: Any) -> FakeLLM:
        return FakeLLM(list(script))

    return factory


def _fake_cli(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FAKE_CLI_ARGV", str(tmp_path / "argv.txt"))
    monkeypatch.setenv("FAKE_CLI_EVENTS", str(FIXTURES / "opencode-bash-pwd.jsonl"))


def _until_settled(runner: Runner, session_id: str, timeout: float = 15) -> list:
    q, _ = runner.bus.subscribe(session_id)
    frames = []
    while True:
        f = q.get(timeout=timeout)
        frames.append(f)
        if f.type == "status" and f.payload["status"] in ("done", "error", "cancelled"):
            return frames


def _wait_for(pred, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError("等待超时")


def _patch(server: Server, space_id: str, body: dict):
    return server.handle("PATCH", f"/api/spaces/{space_id}", {}, json.dumps(body).encode())


def _input(server: Server, session_id: str, text: str):
    return server.handle(
        "POST", f"/api/sessions/{session_id}/input", {}, json.dumps({"text": text}).encode()
    )


# ------------------------------------------------------------------ runner
def test_locked_session_rejected_by_runner(config, sa_home, tmp_path, monkeypatch):
    """老会话执行者对不上：发 error 帧，不落用户消息、不改状态、不拉起 CLI。"""
    _fake_cli(monkeypatch, tmp_path)
    store = SpaceStore(sa_home)
    space = store.create_space(SpaceSpec(name="t", kind="generic", profile="a"))
    old = store.create_session(space.id)
    store.change_executor(space.id, "opencode", command=FAKE)

    runner = Runner(config, store=store, llm_factory=_fake_factory([{"content": "不该跑"}]))
    runner.start()
    q, _ = runner.bus.subscribe(old.id)
    runner.run_input(space.id, old.id, "还能接着聊吗")
    frame = q.get(timeout=5)
    runner.shutdown()

    assert frame.type == "error"
    assert "simpleagent" in frame.payload["message"] and "opencode" in frame.payload["message"]
    assert store.load_session(space.id, old.id).messages == []
    assert store.get_session_meta(space.id, old.id).status == "idle"
    assert not (tmp_path / "argv.txt").exists()


def test_new_session_uses_new_executor(config, sa_home, tmp_path, monkeypatch):
    _fake_cli(monkeypatch, tmp_path)
    store = SpaceStore(sa_home)
    space = store.create_space(SpaceSpec(name="t", kind="generic", profile="a"))
    store.change_executor(space.id, "opencode", command=FAKE)
    session = store.create_session(space.id)

    llm_calls: list[str] = []

    def factory(name, profile):
        llm_calls.append(name)
        return FakeLLM([])

    runner = Runner(config, store=store, llm_factory=factory)
    runner.start()
    runner.run_input(space.id, session.id, "跑一下 pwd")
    frames = _until_settled(runner, session.id)
    runner.shutdown()

    assert frames[-1].payload["status"] == "done"
    assert (tmp_path / "argv.txt").exists()  # 走的是 CLI
    assert llm_calls == []  # 内置 loop 一次都没碰


def test_switch_back_unlocks(config, sa_home):
    """切走再切回：老会话接着跑，模型看到的历史还在。"""
    store = SpaceStore(sa_home)
    space = store.create_space(SpaceSpec(name="t", kind="generic", profile="a"))
    session = store.create_session(space.id)
    runner = Runner(
        config, store=store, llm_factory=_fake_factory([{"content": "第一轮"}, {"content": "二"}])
    )
    runner.start()
    runner.run_input(space.id, session.id, "一")
    _until_settled(runner, session.id)

    store.change_executor(space.id, "claude-code")
    store.change_executor(space.id, "simpleagent")
    runner.run_input(space.id, session.id, "二")
    frames = _until_settled(runner, session.id)
    runner.shutdown()

    assert frames[-1].payload["status"] == "done"
    history = store.load_model_session(space.id, session.id).messages
    assert [m["content"] for m in history if m["role"] == "user"] == ["一", "二"]


def test_space_busy(config, sa_home, tmp_path, monkeypatch):
    _fake_cli(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_CLI_SLEEP", "30")
    store = SpaceStore(sa_home)
    space = store.create_space(
        SpaceSpec(name="t", kind="generic", executor="opencode", command=FAKE)
    )
    other = store.create_space(SpaceSpec(name="o", kind="generic", profile="a"))
    session = store.create_session(space.id)
    runner = Runner(config, store=store)
    runner.start()
    assert not runner.space_busy(space.id)

    runner.run_input(space.id, session.id, "长任务")
    _wait_for(lambda: (tmp_path / "argv.txt").exists())
    assert runner.space_busy(space.id)
    assert not runner.space_busy(other.id)  # 别的空间的会话不算

    runner.cancel(session.id)
    _until_settled(runner, session.id)
    _wait_for(lambda: not runner.space_busy(space.id))
    runner.shutdown()


# ------------------------------------------------------------------ API
def test_patch_executor_roundtrip(config, sa_home):
    store = SpaceStore(sa_home)
    space = store.create_space(SpaceSpec(name="t", kind="generic", profile="a"))
    server = Server(config, store=store)

    r = _patch(server, space.id, {"name": "改名", "executor": "claude-code", "permission": "full"})
    assert r.status == 200
    assert r.body["name"] == "改名"
    assert r.body["executor"] == "claude-code" and r.body["permission"] == "full"
    assert r.body["agent"]["command"] == "claude"

    r = _patch(server, space.id, {"executor": "simpleagent", "profile": "b"})
    assert r.status == 200
    assert r.body["executor"] == "simpleagent" and r.body["profile"] == "b"
    # 切回内置没带 permission：回到「没单独设过」，实际跟配置默认（工作区）
    assert r.body["agent"] is None and r.body["permission"] is None
    assert r.body["mode"] == "workspace"


def test_patch_executor_rejects_bad_input(config, sa_home):
    store = SpaceStore(sa_home)
    space = store.create_space(SpaceSpec(name="t", kind="generic", profile="a"))
    server = Server(config, store=store)
    bad = [
        ({"executor": "gpt"}, "未知执行者"),
        ({"executor": "simpleagent", "cli_model": "x"}, "内置却带 cli_model"),
        ({"permission": "full"}, "改权限不带 executor"),
        ({"profile": "nope"}, "profile 不在 config 里"),
        ({"executor": "simpleagent", "kind": "agent"}, "kind 不开放修改"),
        ({"executor": "claude-code", "kind": "agent"}, "混着不许改的字段：执行者也不能先切"),
    ]
    for body, why in bad:
        assert _patch(server, space.id, body).status == 400, why
    got = store.get_space(space.id)
    assert got.executor == "simpleagent" and got.profile == "a"
    assert _patch(server, "sp_missing", {"executor": "simpleagent"}).status == 404


def test_patch_executor_conflicts_while_running(config, sa_home):
    class BusyRunner(Runner):
        def space_busy(self, space_id: str) -> bool:
            return True

    store = SpaceStore(sa_home)
    space = store.create_space(SpaceSpec(name="t", kind="generic", profile="a"))
    server = Server(config, store=store, runner=BusyRunner(config, store=store))

    assert _patch(server, space.id, {"executor": "claude-code"}).status == 409
    assert store.get_space(space.id).executor == "simpleagent"
    # 不涉及执行者的修改（改名、切 profile）不受影响
    assert _patch(server, space.id, {"name": "x", "profile": "b"}).status == 200
    # 「空间设置」保存时总带着 executor；执行者没变就不算切换，不该 409
    r = _patch(server, space.id, {"name": "y", "executor": "simpleagent", "profile": "a"})
    assert r.status == 200 and r.body["name"] == "y" and r.body["profile"] == "a"


def test_locked_session_input_returns_409(config, sa_home):
    store = SpaceStore(sa_home)
    space = store.create_space(SpaceSpec(name="t", kind="generic", profile="a"))
    session = store.create_session(space.id)
    store.append_message(space.id, session.id, {"role": "user", "content": "旧的一句"})
    store.change_executor(space.id, "claude-code")
    server = Server(config, store=store)

    r = _input(server, session.id, "hi")
    assert r.status == 409 and "claude-code" in r.body["error"]
    r = server.handle("POST", f"/api/sessions/{session.id}/rerun", {}, b"{}")
    assert r.status == 409
