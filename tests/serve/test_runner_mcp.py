"""M5：sa serve 的 Runner 持有一个 McpManager，所有空间、所有会话共用同一批 server 进程。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from simpleagent.llm.fake import FakeLLM
from simpleagent.serve.runner import Runner
from simpleagent.spaces.models import SpaceSpec
from simpleagent.spaces.store import SpaceStore


def _fake_factory(script: list[Any]) -> Any:
    def factory(name: str, profile: Any) -> FakeLLM:
        return FakeLLM(list(script))

    return factory


def _wait_done(queue: Any) -> None:
    while True:
        frame = queue.get(timeout=10)
        if frame.type == "status" and frame.payload["status"] in ("done", "error"):
            assert frame.payload["status"] == "done", frame.payload
            return


def test_runner_shares_mcp_server_across_sessions(config, sa_home, fake_mcp, tmp_path: Path):
    record = tmp_path / "received.jsonl"
    config = config.model_copy(update={"mcp_servers": {"fake": fake_mcp("--record", str(record))}})
    store = SpaceStore(sa_home)
    space = store.create_space(SpaceSpec(name="t", kind="generic", profile="a"))
    sessions = [store.create_session(space.id) for _ in range(2)]
    call = {"id": "c1", "name": "mcp__fake__echo", "arguments": {"text": "你好"}}
    runner = Runner(
        config, store=store, llm_factory=_fake_factory([{"tool_calls": [call]}, "好了"])
    )
    runner.start()
    try:
        for session in sessions:
            queue, _ = runner.bus.subscribe(session.id)
            runner.run_input(space.id, session.id, "让它说你好")
            _wait_done(queue)
            messages = store.load_session(space.id, session.id).messages
            tool = next(m for m in messages if m.get("role") == "tool")
            assert tool["content"] == "你好"
        server = runner.mcp.servers[0]
        assert server.state == "ready" and server.client is not None
        transport = server.client.transport
    finally:
        runner.shutdown()
    lines = record.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line).get("method") for line in lines].count("initialize") == 1
    assert transport.returncode is not None  # shutdown 时关掉了子进程


def test_server_close_shuts_down_mcp(config, sa_home, fake_mcp):
    from simpleagent.serve.app import make_server

    config = config.model_copy(update={"mcp_servers": {"fake": fake_mcp()}})
    httpd = make_server(config, host="127.0.0.1", port=0)
    runner = httpd.app.runner
    assert runner._mcp_started is not None
    runner._mcp_started.result(timeout=10)  # 等启动完
    transport = runner.mcp.servers[0].client.transport
    httpd.server_close()  # sa serve 按 Ctrl+C 走的就是这里
    assert runner.mcp.servers[0].state == "closed"
    assert transport.returncode is not None
