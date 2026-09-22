"""M6 第 4 步：工作台的「模型看到的历史」改由 SessionStore 落盘（sessions/model/<sid>.jsonl）。

以前每次输入都从展示用的 jsonl 重建历史，而那份只按事件镜像：中断修复、清理、压缩都存不下来。
"""

from __future__ import annotations

import json
from queue import Queue
from typing import Any

from simpleagent.llm.fake import FakeLLM
from simpleagent.serve.bus import Frame
from simpleagent.serve.runner import Runner
from simpleagent.spaces.models import SpaceSpec
from simpleagent.spaces.store import SpaceStore


class SharedScript:
    """每条输入都会新建 agent 和模型客户端：让它们吃同一份脚本，并记下每个 FakeLLM。"""

    def __init__(self, script: list[Any]):
        self.script = list(script)
        self.fakes: list[FakeLLM] = []

    def __call__(self, name: str, profile: Any) -> FakeLLM:
        fake = FakeLLM([], name=name, profile=profile)
        fake.responses = self.script  # 共用同一个列表：上一条输入吃掉的，下一条就看不到了
        self.fakes.append(fake)
        return fake


def wait_for(q: Queue, predicate) -> list[Frame]:
    frames = []
    while True:
        frame = q.get(timeout=5)
        frames.append(frame)
        if predicate(frame):
            return frames


def is_status(status: str):
    return lambda f: f.type == "status" and f.payload.get("status") == status


def assert_paired(messages: list[dict]) -> None:
    for i, message in enumerate(messages):
        ids = [call["id"] for call in message.get("tool_calls") or []]
        assert [m.get("tool_call_id") for m in messages[i + 1 : i + 1 + len(ids)]] == ids


def setup(config, sa_home, script: list[Any]):
    store = SpaceStore(sa_home)
    space = store.create_space(SpaceSpec(name="t", kind="generic", profile="a"))
    session = store.create_session(space.id)
    llm = SharedScript(script)
    runner = Runner(config, store=store, llm_factory=llm)
    runner.start()
    q, _ = runner.bus.subscribe(session.id)
    return store, space, session, llm, runner, q


def test_cancel_during_approval_then_continue(config, sa_home):
    """回归：等审批时取消，_repair 补的「执行被中断」要落盘，否则下一条输入带着悬空的
    tool_call 发出去，会被 API 拒绝。"""
    call = {"id": "w1", "name": "write_file", "arguments": {"path": "x.txt", "content": "hi"}}
    store, space, session, llm, runner, q = setup(
        config, sa_home, [{"tool_calls": [call]}, "好的，先不写了"]
    )
    try:
        runner.run_input(space.id, session.id, "写个文件")
        wait_for(q, lambda f: f.type == "approval_request")
        runner.cancel(session.id)
        wait_for(q, is_status("cancelled"))

        runner.run_input(space.id, session.id, "算了")
        wait_for(q, is_status("done"))
    finally:
        runner.shutdown()

    sent = llm.fakes[1].requests[0]["messages"][1:]
    assert_paired(sent)
    assert sent[2]["role"] == "tool" and "中断" in sent[2]["content"]
    assert sent[-1] == {"role": "user", "content": "算了"}
    # 展示用的 jsonl 还是只记实际发生过的事
    shown = store.load_session(space.id, session.id).messages
    assert [m["role"] for m in shown] == ["user", "assistant", "user", "assistant"]


def test_cleared_results_survive_across_inputs(config, sa_home):
    """清理写进模型历史：下一条输入不用重新清，展示用的 jsonl 里还是原文。"""
    config.profiles["a"].context_window = 6000  # 输入上限 5000
    config.profiles["a"].max_tokens = 1000
    config.context.compact_at = 0
    calls = [
        {"tool_calls": [{"id": f"r{i}", "name": "read_file", "arguments": {"path": "big.txt"}}]}
        for i in range(4)
    ]
    store, space, session, llm, runner, q = setup(config, sa_home, [*calls, "读完了", "好"])
    (store._space_dir(space.id) / "tmp").mkdir(parents=True, exist_ok=True)
    (store._space_dir(space.id) / "tmp" / "big.txt").write_text(("x" * 99 + "\n") * 40)
    try:
        runner.run_input(space.id, session.id, "读四遍")
        frames = wait_for(q, is_status("done"))
        assert any(f.type == "context_edited" for f in frames)

        runner.run_input(space.id, session.id, "谢谢")
        frames = wait_for(q, is_status("done"))
        # 清理结果从模型历史里读回来，不用再清一遍（以前每轮都从展示用的 jsonl 重建，得重新清）
        assert not any(f.type == "context_edited" for f in frames)
    finally:
        runner.shutdown()

    sent = llm.fakes[1].requests[0]["messages"]
    assert any(str(m.get("content")).startswith("[已清理]") for m in sent)
    shown = store.load_session(space.id, session.id).messages
    assert not any(str(m.get("content")).startswith("[已清理]") for m in shown)


def test_legacy_history_is_migrated_and_repaired(config, sa_home):
    """老会话只有展示用的 jsonl，里面还留着以前中断留下的悬空 tool_call：迁移时补上。"""
    store, space, session, llm, runner, q = setup(config, sa_home, ["继续聊"])
    legacy = [
        {"role": "user", "content": "写个文件"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "w1",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": "{}"},
                }
            ],
        },
    ]
    jsonl = store._session_jsonl(space.id, session.id)
    jsonl.write_text("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in legacy))
    store.update_meta(space.id, session.id, usage={"prompt_tokens": 50, "completion_tokens": 5})
    try:
        runner.run_input(space.id, session.id, "接着来")
        wait_for(q, is_status("done"))
    finally:
        runner.shutdown()

    sent = llm.fakes[0].requests[0]["messages"][1:]
    assert_paired(sent)
    assert sent[:2] == legacy and sent[-1] == {"role": "user", "content": "接着来"}
    model = store.model_sessions(space.id).load(session.id)
    assert model is not None and model.messages[-1]["content"] == "继续聊"
    assert model.usage.prompt_tokens == 50  # 用量从 meta 接过来，接着累计
