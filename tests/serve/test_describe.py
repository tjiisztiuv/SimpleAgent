"""空间简介：PATCH 保存、自动生成（FakeLLM，不联网）、素材怎么拼。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from simpleagent.llm.fake import FakeLLM
from simpleagent.serve.app import Server
from simpleagent.spaces.describe import gather_material
from simpleagent.spaces.models import SpaceSpec
from simpleagent.spaces.store import SpaceStore


def _server(config, sa_home, script: list[Any]) -> tuple[Server, list[FakeLLM]]:
    made: list[FakeLLM] = []

    def factory(name: str, profile: Any) -> FakeLLM:
        llm = FakeLLM(list(script))
        made.append(llm)
        return llm

    server = Server(config, store=SpaceStore(sa_home), llm_factory=factory)
    server.start()
    return server, made


def _post(server: Server, path: str, body: dict | None = None):
    return server.handle("POST", path, {}, json.dumps(body or {}).encode())


def test_patch_description(config, sa_home):
    server, _ = _server(config, sa_home, [])
    space = server.store.create_space(SpaceSpec(name="t", kind="generic", profile="a"))
    path = f"/api/spaces/{space.id}"
    resp = server.handle("PATCH", path, {}, json.dumps({"description": "整理下载目录"}).encode())
    assert resp.status == 200
    assert resp.body["description"] == "整理下载目录"
    too_long = json.dumps({"description": "字" * 201}).encode()
    assert server.handle("PATCH", path, {}, too_long).status == 400
    assert server.store.get_space(space.id).description == "整理下载目录"
    server.runner.shutdown()


def test_describe_returns_suggestion_without_saving(config, sa_home, tmp_path: Path):
    project = tmp_path / "invest"
    project.mkdir()
    (project / "README.md").write_text("# 投资日报\n每天抓行情、生成日报。", encoding="utf-8")
    server, made = _server(config, sa_home, [{"content": "「负责投资日报：抓行情、写日报。」"}])
    space = server.store.create_space(
        SpaceSpec(name="invest", kind="agent", profile="a", cwd=str(project))
    )

    resp = _post(server, f"/api/spaces/{space.id}/describe")

    assert resp.status == 200
    assert resp.body["description"] == "负责投资日报：抓行情、写日报。"  # 引号被去掉
    # 只是建议：没有写进 space.toml
    assert server.store.get_space(space.id).description == ""
    # 素材里有 README，且不带工具；用完关掉客户端
    sent = made[0].requests[0]
    assert "投资日报" in sent["messages"][-1]["content"]
    assert not sent["tools"]
    assert made[0].closed
    server.runner.shutdown()


def test_describe_empty_generic_space_is_400(config, sa_home):
    server, made = _server(config, sa_home, [])
    space = server.store.create_space(SpaceSpec(name="空的", kind="generic", profile="a"))
    resp = _post(server, f"/api/spaces/{space.id}/describe")
    assert resp.status == 400
    assert "先手写" in resp.body["error"]
    assert made == []  # 没东西可读就不调模型
    server.runner.shutdown()


def test_describe_model_error_is_502(config, sa_home):
    server, _ = _server(config, sa_home, [RuntimeError("boom")])
    space = server.store.create_space(SpaceSpec(name="t", kind="generic", profile="a"))
    meta = server.store.create_session(space.id)
    server.store.append_message(space.id, meta.id, {"role": "user", "content": "算一下 token"})
    resp = _post(server, f"/api/spaces/{space.id}/describe")
    assert resp.status == 502
    assert "boom" in resp.body["error"]
    assert _post(server, "/api/spaces/sp_nope/describe").status == 404
    server.runner.shutdown()


def test_gather_material(tmp_path: Path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    (cwd / "AGENTS.md").write_text("这是指令文件", encoding="utf-8")
    (cwd / "CLAUDE.md").write_text("不该读到我", encoding="utf-8")
    (cwd / "README.md").write_text("x" * 5000, encoding="utf-8")
    (cwd / ".env").write_text("SECRET=1", encoding="utf-8")
    (cwd / "src").mkdir()
    store = SpaceStore(tmp_path / "home")
    space = store.create_space(SpaceSpec(name="p", kind="agent", cwd=str(cwd)))

    text = gather_material(space, cwd, ["新会话", "修测试", "修测试", "写文档"])

    assert "这是指令文件" in text
    assert "不该读到我" not in text  # 有 AGENTS.md 就不读 CLAUDE.md
    assert "后面省略" in text  # README 只取开头
    assert ".env" not in text  # 隐藏文件不列
    assert "src/" in text
    assert text.count("修测试") == 1 and "新会话" not in text
    # 什么都没有：None
    empty = store.create_space(SpaceSpec(name="e", kind="generic"))
    assert gather_material(empty, tmp_path / "missing", ["新会话"]) is None
