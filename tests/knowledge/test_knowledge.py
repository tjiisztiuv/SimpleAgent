"""M7：三个来源拼进 system prompt、接进三个前端（REPL / sa run / sa serve）。

测试里的数据目录是 sa_home（临时目录），工作目录是临时建的 git 仓库，
不会读到本仓库自己的 AGENTS.md，也不会写真实的 ~/.simpleagent。
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from simpleagent.agent.prompt import build_system_prompt
from simpleagent.config import Config, Profile
from simpleagent.knowledge import Knowledge
from simpleagent.knowledge.memory import MemoryStore
from simpleagent.llm.fake import FakeLLM, Script
from simpleagent.ui.headless import Headless
from simpleagent.ui.repl import Repl


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一个带 AGENTS.md 和项目技能的 git 仓库，并把 cwd 切过去。"""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "AGENTS.md").write_text("改完代码先跑 pytest", encoding="utf-8")
    skill = root / ".agents" / "skills" / "release"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: release\ndescription: 发版流程\n---\n先写 changelog，再打 tag $ARGUMENTS\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def remembered(sa_home: Path) -> MemoryStore:
    store = MemoryStore(sa_home / "memory")
    store.write("prefs", "回答用中文、先给结论", "原因：看得快")
    return store


def test_prompt_sections_in_order(config: Config, sa_home: Path, repo: Path, remembered):
    (sa_home / "AGENTS.md").write_text("我叫小付", encoding="utf-8")
    knowledge = Knowledge.load(config, repo)
    prompt = build_system_prompt("基础", cwd=repo, knowledge=knowledge)
    order = ["基础", "# 环境", "# 项目指令", "我叫小付", "改完代码先跑 pytest", "# 长期记忆"]
    order += ["- [prefs](prefs.md) — 回答用中文、先给结论", "# 技能", "- release：发版流程"]
    positions = [prompt.index(text) for text in order]
    assert positions == sorted(positions)
    assert "先写 changelog" not in prompt  # 技能正文不进 prompt
    assert "原因：看得快" not in prompt  # 记忆正文不进 prompt
    assert [tool.name for tool in knowledge.tools()] == [
        "memory_read",
        "memory_write",
        "memory_delete",
        "load_skill",
    ]
    summary = knowledge.summary()
    assert summary.startswith(f"项目指令 {sa_home / 'AGENTS.md'}、{repo / 'AGENTS.md'}")
    assert summary.endswith(" · 记忆 1 条 · 技能 1 个")


def test_everything_can_be_switched_off(config: Config, repo: Path, remembered):
    config = config.model_copy(
        update={
            "instructions": config.instructions.model_copy(update={"enabled": False}),
            "memory": config.memory.model_copy(update={"enabled": False}),
            "skills": config.skills.model_copy(update={"enabled": False}),
        }
    )
    knowledge = Knowledge.load(config, repo)
    assert knowledge.prompt_section() == "" and knowledge.tools() == []
    assert knowledge.summary() == ""


def test_memory_snapshot_is_taken_at_load(config: Config, repo: Path, remembered):
    knowledge = Knowledge.load(config, repo)
    remembered.write("later", "会话中途写的", "x")
    assert "later" not in knowledge.prompt_section()  # 本会话不刷新：前缀缓存不受影响
    assert "later" in Knowledge.load(config, repo).prompt_section()  # 下个会话才有


def test_empty_memory_still_offers_tools(config: Config, repo: Path):
    knowledge = Knowledge.load(config, repo)
    assert "还没有任何记忆" in knowledge.prompt_section()
    assert "memory_write" in [tool.name for tool in knowledge.tools()]


# ------------------------------------------------------------------ REPL


class Harness:
    def __init__(self, config: Config, script: list[Script], inputs: list[str] = ()) -> None:
        self.out = io.StringIO()
        self.fake: FakeLLM | None = None
        remaining = iter(inputs)

        def factory(name: str, profile: Profile) -> FakeLLM:
            self.fake = FakeLLM(list(script), name=name, profile=profile)
            return self.fake

        def input_fn(prompt: str) -> str:
            try:
                return next(remaining)
            except StopIteration:
                raise EOFError from None

        self.repl = Repl(config, llm_factory=factory, out=self.out, input_fn=input_fn)


def test_repl_startup_summary_and_system_prompt(config: Config, repo: Path, remembered):
    h = Harness(config, ["好"], inputs=["hi", "/exit"])
    assert h.repl.run() == 0
    assert "项目指令" in h.out.getvalue() and "记忆 1 条 · 技能 1 个" in h.out.getvalue()
    system = h.fake.requests[0]["messages"][0]["content"]
    assert "改完代码先跑 pytest" in system and "- release：发版流程" in system


async def test_repl_memory_command(config: Config, repo: Path, remembered):
    h = Harness(config, [])
    await h.repl.handle("/memory")
    out = h.out.getvalue()
    assert f"记忆目录：{remembered.root}（1 条）" in out
    assert "- [prefs](prefs.md) — 回答用中文、先给结论" in out
    assert "改过" not in out
    remembered.write("new", "新写的", "x")
    (remembered.root / "orphan.md").write_text("手动放的")
    await h.repl.handle("/memory")
    out = h.out.getvalue()
    assert "索引在本会话开始后改过" in out
    assert "没进索引的记忆（模型看不到）：orphan" in out


async def test_repl_skills_and_prompt_commands(config: Config, repo: Path):
    broken = repo / ".agents" / "skills" / "broken"
    broken.mkdir()
    (broken / "SKILL.md").write_text("---\nname: broken\n---\n")
    h = Harness(config, [])
    await h.repl.handle("/skills")
    out = h.out.getvalue()
    assert "release" in out and "发版流程" in out
    assert "[写坏了，没加载]" in out and "description" in out
    await h.repl.handle("/prompt")
    assert "# 技能" in h.out.getvalue() and "[共 " in h.out.getvalue()


async def test_repl_invokes_skill_by_slash(config: Config, repo: Path):
    h = Harness(config, ["好的"])
    await h.repl.handle("/release v0.3.0")
    user = h.fake.requests[0]["messages"][-1]["content"]
    assert user.startswith("/release v0.3.0\n")
    assert "先写 changelog，再打 tag v0.3.0" in user
    await h.repl.handle("/nope")
    assert "未知命令 /nope" in h.out.getvalue()


async def test_repl_builtin_commands_win_over_skills(config: Config, repo: Path):
    skill = repo / ".agents" / "skills" / "usage"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\ndescription: 同名技能\n---\n不该被调用\n")
    h = Harness(config, [])
    await h.repl.handle("/usage")
    assert "请求 0 次" in h.out.getvalue()


# ------------------------------------------------------------------ sa run


def make_headless(config: Config, cwd: Path, script: list[Script], allowed=()) -> Headless:
    def factory(name: str, profile: Profile) -> FakeLLM:
        return FakeLLM(list(script), name=name, profile=profile)

    return Headless(
        config,
        cwd=cwd,
        allowed_tools=allowed,
        llm_factory=factory,
        out=io.StringIO(),
        err=io.StringIO(),
    )


async def test_headless_injects_knowledge_and_rejects_memory_writes(
    config: Config, repo: Path, remembered
):
    write = {
        "id": "w",
        "name": "memory_write",
        "arguments": {"name": "x", "description": "d", "content": "c"},
    }
    frontend = make_headless(config, repo, [{"tool_calls": [write]}, "算了"])
    assert await frontend.run("记住 x") == 0
    system = frontend.agent.llm.requests[0]["messages"][0]["content"]
    assert "改完代码先跑 pytest" in system and "prefs" in system
    tool = frontend.session.messages[2]
    assert tool["content"].endswith("已被拒绝")  # 白名单里没有 memory_write：默认拒绝
    assert remembered.names() == ["prefs"]
    assert "记忆 1 条" in frontend.err.getvalue()


async def test_headless_memory_write_with_allow(config: Config, repo: Path, remembered):
    write = {
        "id": "w",
        "name": "memory_write",
        "arguments": {"name": "x", "description": "d", "content": "c"},
    }
    frontend = make_headless(
        config, repo, [{"tool_calls": [write]}, "好"], allowed=["memory_write"]
    )
    await frontend.run("记住 x")
    assert remembered.names() == ["prefs", "x"]


async def test_headless_skill_command(config: Config, repo: Path):
    frontend = make_headless(config, repo, ["好"])
    assert await frontend.run("/release v1") == 0
    assert "再打 tag v1" in frontend.agent.llm.requests[0]["messages"][-1]["content"]


async def test_headless_broken_skill_exits_1(config: Config, repo: Path):
    frontend = make_headless(config, repo, [])
    (repo / ".agents" / "skills" / "release" / "SKILL.md").unlink()
    assert await frontend.run("/release") == 1
    assert "技能加载失败" in frontend.err.getvalue()


# ------------------------------------------------------------------ sa serve


def test_runner_freezes_system_prompt_per_session(config: Config, sa_home: Path, remembered):
    from simpleagent.serve.runner import Runner
    from simpleagent.spaces.models import SpaceSpec
    from simpleagent.spaces.store import SpaceStore

    fakes: list[FakeLLM] = []

    def factory(name: str, profile: Any) -> FakeLLM:
        fakes.append(FakeLLM(["好"]))
        return fakes[-1]

    store = SpaceStore(sa_home)
    space = store.create_space(SpaceSpec(name="t", kind="generic", profile="a"))
    session = store.create_session(space.id)
    runner = Runner(config, store=store, llm_factory=factory)
    runner.start()
    try:
        for text in ("第一轮", "第二轮"):
            queue, _ = runner.bus.subscribe(session.id)
            runner.run_input(space.id, session.id, text)
            while True:
                frame = queue.get(timeout=10)
                if frame.type == "status" and frame.payload["status"] in ("done", "error"):
                    assert frame.payload["status"] == "done", frame.payload
                    break
            remembered.write("mid", "两轮之间写的", "x")  # 下一轮的 system prompt 也不该变
    finally:
        runner.shutdown()
    first, second = (fake.requests[0]["messages"][0]["content"] for fake in fakes)
    assert first == second
    assert "prefs" in first and "mid" not in first
    assert [t["function"]["name"] for t in fakes[1].requests[0]["tools"]][-3:] == [
        "memory_read",
        "memory_write",
        "memory_delete",
    ]
