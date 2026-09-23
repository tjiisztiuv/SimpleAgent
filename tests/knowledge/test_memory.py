"""M7：长期记忆。每条一个文件，索引 MEMORY.md 由工具维护，写入默认要确认。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from simpleagent.agent.loop import Agent
from simpleagent.agent.session import Session
from simpleagent.knowledge.frontmatter import split_frontmatter
from simpleagent.knowledge.memory import (
    INDEX_MAX_CHARS,
    MemoryStore,
    MemoryStoreError,
    index_line,
    memory_section,
    memory_tools,
)
from simpleagent.llm.fake import FakeLLM
from simpleagent.permissions import ApprovalDecision, Policy, WhitelistApprover
from simpleagent.tools import ToolContext, ToolRegistry


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "memory")


def test_write_creates_file_and_index_line(store: MemoryStore):
    created, line = store.write(
        "user-prefs", "回答风格：简洁、中文", "先给结论。\n\n原因：用户赶时间"
    )
    assert created
    assert line == "- [user-prefs](user-prefs.md) — 回答风格：简洁、中文"
    fields, body = split_frontmatter((store.root / "user-prefs.md").read_text())
    assert fields["name"] == "user-prefs"
    assert fields["description"] == "回答风格：简洁、中文"
    assert len(fields["updated"]) == 10  # YYYY-MM-DD
    assert body == "先给结论。\n\n原因：用户赶时间"
    index = store.read_index()
    assert index.startswith("# 记忆索引")
    assert index.endswith(f"。\n\n{line}")  # 接在说明文字后面要空一行，Markdown 里才是列表
    assert store.names() == ["user-prefs"]
    store.write("b", "第二条", "y")
    assert store.read_index().endswith(f"\n{line}\n- [b](b.md) — 第二条")  # 列表里接着写，不空行


def test_update_replaces_only_its_line_and_keeps_manual_edits(store: MemoryStore):
    store.write("a", "第一条", "x")
    store.write("b", "第二条", "y")
    # 用户手动整理过索引：加了分组标题和备注
    manual = "# 我的记忆\n\n## 关于我\n{a}\n备注：别删\n\n## 项目\n- [b](b.md) — 第二条"
    store.index_path.write_text(manual.format(a="- [a](a.md) — 第一条") + "\n")
    created, _ = store.write("a", "第一条（改过）", "x2")
    assert not created
    assert store.read_index() == manual.format(a="- [a](a.md) — 第一条（改过）")


def test_duplicate_index_lines_are_collapsed(store: MemoryStore):
    store.write("a", "旧", "x")
    store.index_path.write_text("- [a](a.md) — 旧\n- [a](./a.md) — 又一行\n")
    store.write("a", "新", "x")
    assert store.read_index() == "- [a](a.md) — 新"


def test_delete_removes_file_and_line(store: MemoryStore):
    store.write("a", "第一条", "x")
    store.write("b", "第二条", "y")
    store.delete("a")
    assert store.names() == ["b"]
    assert "a.md" not in store.read_index()
    with pytest.raises(MemoryStoreError, match="没有名为 a"):
        store.delete("a")


def test_read_unknown_lists_known(store: MemoryStore):
    store.write("a", "第一条", "x")
    with pytest.raises(MemoryStoreError, match="已有：a"):
        store.read("nope")


@pytest.mark.parametrize("name", ["../etc/passwd", "a/b", "-x", "", "MEMORY", "a" * 65, ".hidden"])
def test_bad_names_rejected(store: MemoryStore, name: str):
    with pytest.raises(MemoryStoreError, match="记忆名不合法"):
        store.write(name, "d", "c")


def test_chinese_name_and_md_suffix_accepted(store: MemoryStore):
    store.write("用户偏好.md", "d", "c")
    assert store.names() == ["用户偏好"]
    assert "用户偏好" in store.read("用户偏好")


def test_empty_description_or_content_rejected(store: MemoryStore):
    with pytest.raises(MemoryStoreError, match="description"):
        store.write("a", "  ", "c")
    with pytest.raises(MemoryStoreError, match="memory_delete"):
        store.write("a", "d", "\n")


def test_check_finds_unindexed_and_dangling(store: MemoryStore):
    store.write("a", "第一条", "x")
    (store.root / "manual.md").write_text("手动放进来的")
    store.index_path.write_text(store.read_index() + "\n- [gone](gone.md) — 文件删了\n")
    assert store.check() == (["manual"], ["gone"])


def test_index_line_clips_long_description():
    line = index_line("a", "长" * 200)
    assert line.endswith("…") and len(line) < 200


def test_section_with_and_without_index(store: MemoryStore):
    empty = memory_section(store, "")
    assert "还没有任何记忆" in empty and str(store.root) in empty
    section = memory_section(store, "- [a](a.md) — 第一条")
    assert "<memory_index>\n- [a](a.md) — 第一条\n</memory_index>" in section
    assert "memory_read" in section and "memory_write" in section


def test_section_truncates_huge_index(store: MemoryStore):
    section = memory_section(store, "x" * (INDEX_MAX_CHARS + 500))
    assert "x" * INDEX_MAX_CHARS in section
    assert "x" * (INDEX_MAX_CHARS + 1) not in section
    assert "索引太长" in section


# ------------------------------------------------------------------ 工具


def call(name: str, arguments: dict, call_id: str = "c1") -> dict:
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(arguments)}}


class Recorder:
    def __init__(self, allow: bool) -> None:
        self.allow = allow
        self.requests = []

    async def request(self, req):
        self.requests.append(req)
        return ApprovalDecision(allow=self.allow)


async def test_tools_permissions_and_results(store: MemoryStore, tmp_path: Path):
    approver = Recorder(allow=True)
    registry = ToolRegistry(memory_tools(store), approver=approver, policy=Policy(tmp_path / "w"))
    ctx = ToolContext(cwd=tmp_path / "w")
    write = await registry.execute(
        call("memory_write", {"name": "a", "description": "第一条", "content": "正文"}), ctx
    )
    # 记忆目录在工作目录之外，但记忆工具不报 scope，不会被边界拦下；要问一句
    assert not write.is_error and write.decision == "ask"
    assert "已新建记忆" in write.content and "- [a](a.md) — 第一条" in write.content
    assert approver.requests[0].reason == "会写入长期记忆，之后的每个会话都会读到"
    read = await registry.execute(call("memory_read", {"name": "a"}), ctx)
    assert read.decision == "allow" and "正文" in read.content
    delete = await registry.execute(call("memory_delete", {"name": "a"}), ctx)
    assert not delete.is_error and store.names() == []
    assert len(approver.requests) == 2  # 读不问，写和删各问一次
    assert registry.is_readonly("memory_read") and not registry.is_readonly("memory_write")


async def test_denied_write_leaves_nothing(store: MemoryStore, tmp_path: Path):
    registry = ToolRegistry(
        memory_tools(store), approver=Recorder(allow=False), policy=Policy(tmp_path)
    )
    result = await registry.execute(
        call("memory_write", {"name": "a", "description": "d", "content": "c"}),
        ToolContext(cwd=tmp_path),
    )
    assert result.is_error and "已被拒绝" in result.content
    assert not store.root.exists()


async def test_headless_whitelist_rejects_unless_allowed(store: MemoryStore, tmp_path: Path):
    args = {"name": "a", "description": "d", "content": "c"}
    ctx = ToolContext(cwd=tmp_path)
    denied = ToolRegistry(
        memory_tools(store), approver=WhitelistApprover(), policy=Policy(tmp_path)
    )
    assert (await denied.execute(call("memory_write", args), ctx)).is_error
    allowed = ToolRegistry(
        memory_tools(store), approver=WhitelistApprover(["memory_write"]), policy=Policy(tmp_path)
    )
    assert not (await allowed.execute(call("memory_write", args), ctx)).is_error


async def test_confirm_writes_off_means_allow(store: MemoryStore, tmp_path: Path):
    registry = ToolRegistry(
        memory_tools(store, confirm_writes=False), approver=None, policy=Policy(tmp_path)
    )
    result = await registry.execute(
        call("memory_write", {"name": "a", "description": "d", "content": "c"}),
        ToolContext(cwd=tmp_path),
    )
    assert not result.is_error and result.decision == "allow"


async def test_bad_name_is_a_tool_error(store: MemoryStore, tmp_path: Path):
    registry = ToolRegistry(memory_tools(store, confirm_writes=False))
    result = await registry.execute(
        call("memory_read", {"name": "../x"}), ToolContext(cwd=tmp_path)
    )
    assert result.is_error and "记忆名不合法" in result.content


async def test_model_remembers_through_the_loop(store: MemoryStore, tmp_path: Path):
    """用户说「记住」→ 模型调 memory_write → 文件和索引都有了；本会话的 system prompt 不变。"""
    llm = FakeLLM(
        [
            {
                "tool_calls": [
                    {
                        "id": "m1",
                        "name": "memory_write",
                        "arguments": {
                            "name": "coffee",
                            "description": "喝咖啡不加糖",
                            "content": "不加糖。原因：控糖",
                        },
                    }
                ]
            },
            "记住了",
            "好的",
        ]
    )
    agent = Agent(
        llm,
        ToolRegistry(memory_tools(store, confirm_writes=False)),
        "系统提示 v1",
        cwd=tmp_path,
    )
    session = Session("s")
    [e async for e in agent.run(session, "记住：我喝咖啡不加糖")]
    [e async for e in agent.run(session, "谢谢")]
    assert store.read_index().endswith("- [coffee](coffee.md) — 喝咖啡不加糖")
    assert {r["messages"][0]["content"] for r in llm.requests} == {"系统提示 v1"}
