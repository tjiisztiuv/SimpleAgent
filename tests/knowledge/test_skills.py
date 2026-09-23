"""M7：技能。名字和描述进 prompt，正文由 load_skill 按需加载；/技能名 由用户直接调用。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from simpleagent.agent.loop import Agent
from simpleagent.agent.session import Session
from simpleagent.events import ToolResult
from simpleagent.knowledge.skills import (
    LISTING_MAX_CHARS,
    SkillCatalog,
    SkillError,
    discover_skills,
    skill_roots,
)
from simpleagent.llm.fake import FakeLLM
from simpleagent.tools import ToolContext, ToolRegistry


def write_skill(root: Path, dirname: str, frontmatter: str, body: str = "照着做") -> Path:
    path = root / dirname / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return path


def test_discover_reads_name_and_description(tmp_path: Path):
    write_skill(tmp_path, "pdf", "name: pdf\ndescription: 从 PDF 抽表格")
    write_skill(tmp_path, "report", "description: 写周报")  # 没写 name 就用目录名
    catalog = discover_skills([tmp_path])
    assert sorted(catalog.skills) == ["pdf", "report"]
    assert catalog.skills["report"].description == "写周报"
    assert catalog.errors == []


def test_broken_skills_are_reported_not_loaded(tmp_path: Path):
    write_skill(tmp_path, "no-desc", "name: no-desc")
    write_skill(tmp_path, "bad-name", "name: 坏 名字\ndescription: x")
    (tmp_path / "no-fm").mkdir()
    (tmp_path / "no-fm" / "SKILL.md").write_text("# 没有 frontmatter")
    (tmp_path / "unterminated").mkdir()
    (tmp_path / "unterminated" / "SKILL.md").write_text("---\nname: x\n")
    (tmp_path / "not-a-skill").mkdir()  # 没有 SKILL.md 的目录直接忽略
    catalog = discover_skills([tmp_path])
    assert catalog.skills == {}
    reasons = {path.parent.name: reason for path, reason in catalog.errors}
    assert set(reasons) == {"no-desc", "bad-name", "no-fm", "unterminated"}
    assert "description" in reasons["no-desc"]
    assert "frontmatter" in reasons["no-fm"]


def test_first_root_wins_and_shadowed_are_recorded(tmp_path: Path):
    personal, project = tmp_path / "personal", tmp_path / "project"
    write_skill(personal, "deploy", "description: 我自己的部署流程")
    write_skill(project, "deploy", "description: 仓库里的同名技能")
    catalog = discover_skills([personal, project])
    assert catalog.skills["deploy"].description == "我自己的部署流程"
    assert [skill.root for skill in catalog.shadowed] == [project]


def test_same_root_twice_is_scanned_once(tmp_path: Path):
    """同一个目录经符号链接出现两次（比如 .claude/skills 链到 .agents/skills）：只扫一遍。"""
    root = tmp_path / "skills"
    write_skill(root, "a", "description: x")
    (tmp_path / "alias").symlink_to(root)
    catalog = discover_skills([root, tmp_path / "alias"])
    assert catalog.shadowed == [] and catalog.roots == [root]


def test_skill_roots_personal_first_then_project_chain(tmp_path: Path):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    cwd = repo / "sub"
    cwd.mkdir(parents=True)
    (repo / ".git").mkdir()
    roots = skill_roots([".agents/skills", "/abs/skills", "~/x"], home, cwd)
    assert roots == [
        home / "skills",
        repo / ".agents/skills",
        cwd / ".agents/skills",
        Path("/abs/skills"),
        Path("~/x").expanduser(),
    ]


def test_prompt_section_sorted_and_hides_manual_only(tmp_path: Path):
    write_skill(tmp_path, "zeta", "description: 最后一个")
    write_skill(tmp_path, "alpha", "description: >\n  第一个，\n  描述折行")
    write_skill(tmp_path, "deploy", "description: 部署\ndisable-model-invocation: true")
    catalog = discover_skills([tmp_path])
    section = catalog.prompt_section()
    assert section.startswith("# 技能")
    assert section.endswith("- alpha：第一个， 描述折行\n- zeta：最后一个")
    assert "deploy" not in section
    assert [skill.name for skill in catalog.listed] == ["alpha", "zeta"]


def test_prompt_section_budget_falls_back_to_names(tmp_path: Path):
    for i in range(20):
        write_skill(tmp_path, f"s{i:02d}", f"description: {'长' * 1000}")
    section = discover_skills([tmp_path]).prompt_section()
    assert "- s19" in section.splitlines()  # 预算用完的只列名字
    assert len(section) < LISTING_MAX_CHARS + 2000


def test_no_listed_skills_no_tool_no_section(tmp_path: Path):
    write_skill(tmp_path, "deploy", "description: 部署\ndisable-model-invocation: yes")
    catalog = discover_skills([tmp_path])
    assert catalog.tool() is None and catalog.prompt_section() == ""
    assert SkillCatalog().tool() is None


def test_render_includes_body_dir_and_resources(tmp_path: Path):
    path = write_skill(tmp_path, "pdf", "description: x", body="用 scripts/extract.py")
    (path.parent / "scripts").mkdir()
    (path.parent / "scripts" / "extract.py").write_text("print(1)")
    (path.parent / "references.md").write_text("参考")
    (path.parent / ".cache").mkdir()
    (path.parent / ".cache" / "junk").write_text("x")
    (path.parent / "node_modules" / "dep").mkdir(parents=True)
    (path.parent / "node_modules" / "dep" / "index.js").write_text("x")
    text = discover_skills([tmp_path]).skills["pdf"].render()
    assert text.startswith(
        f'<skill name="pdf" dir="{path.parent}">\n用 scripts/extract.py\n</skill>'
    )
    assert text.endswith("- references.md\n- scripts/extract.py")
    assert ".cache" not in text and "node_modules" not in text


async def test_load_skill_tool(tmp_path: Path):
    write_skill(tmp_path, "pdf", "description: x", body="第一步……")
    write_skill(tmp_path, "deploy", "description: y\ndisable-model-invocation: true")
    tool = discover_skills([tmp_path]).tool()
    assert tool is not None and tool.readonly and tool.permission == "allow"
    registry = ToolRegistry([tool])
    ctx = ToolContext(cwd=tmp_path)

    def call(name: str) -> dict:
        arguments = json.dumps({"name": name})
        return {"id": "c", "function": {"name": "load_skill", "arguments": arguments}}

    ok = await registry.execute(call("pdf"), ctx)
    assert not ok.is_error and "第一步……" in ok.content
    hidden = await registry.execute(call("deploy"), ctx)  # 只能手动调用的，模型加载不了
    assert hidden.is_error and "可用的技能：pdf" in hidden.content
    missing = await registry.execute(call("nope"), ctx)
    assert missing.is_error


async def test_body_is_read_fresh_each_time(tmp_path: Path):
    path = write_skill(tmp_path, "pdf", "description: x", body="旧版")
    catalog = discover_skills([tmp_path])
    path.write_text("---\ndescription: x\n---\n新版\n")
    assert "新版" in catalog.skills["pdf"].render()
    path.unlink()
    with pytest.raises(SkillError, match="读取技能 pdf 失败"):
        catalog.skills["pdf"].render()


def test_expand_command(tmp_path: Path):
    write_skill(tmp_path, "report", "description: 周报", body="写 $ARGUMENTS 的周报")
    write_skill(tmp_path, "deploy", "description: 部署\ndisable-model-invocation: true")
    catalog = discover_skills([tmp_path])
    expanded = catalog.expand_command("/report 第 39 周")
    assert expanded is not None
    assert expanded.startswith("/report 第 39 周\n\n（用户用 /report 调用了技能")
    assert "写 第 39 周 的周报" in expanded
    assert catalog.expand_command("/deploy") is not None  # 手动调用不受限制
    assert catalog.expand_command("/nope 参数") is None
    assert catalog.expand_command("普通的话") is None
    assert catalog.expand_command("/") is None


async def test_model_loads_skill_then_follows_it(tmp_path: Path):
    """渐进式披露走一遍：prompt 里只有描述，模型调 load_skill 拿到正文，下一次请求里就有了。"""
    write_skill(tmp_path / "skills", "tea", "description: 泡茶的步骤", body="水温 85 度")
    catalog = discover_skills([tmp_path / "skills"])
    llm = FakeLLM(
        [{"tool_calls": [{"id": "s1", "name": "load_skill", "arguments": {"name": "tea"}}]}, "好"]
    )
    agent = Agent(llm, ToolRegistry([catalog.tool()]), catalog.prompt_section(), cwd=tmp_path)
    events = [e async for e in agent.run(Session("s"), "泡杯绿茶")]
    assert "水温 85 度" not in llm.requests[0]["messages"][0]["content"]
    assert "- tea：泡茶的步骤" in llm.requests[0]["messages"][0]["content"]
    result = next(e for e in events if isinstance(e, ToolResult))
    assert "水温 85 度" in result.content
    assert "水温 85 度" in llm.requests[1]["messages"][-1]["content"]
