"""M7：项目指令。个人的 → 项目根 → … → cwd，每层一份；不在 git 仓库里只看 cwd。"""

from __future__ import annotations

from pathlib import Path

from simpleagent.knowledge.instructions import (
    find_instruction_files,
    instructions_section,
    load_instructions,
    project_chain,
    project_root,
)

NAMES = ["AGENTS.md", "CLAUDE.md"]


def make_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    """<tmp>/home（数据目录）、<tmp>/repo（git 仓库）、<tmp>/repo/pkg/sub（cwd）。"""
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    cwd = repo / "pkg" / "sub"
    cwd.mkdir(parents=True)
    home.mkdir()
    (repo / ".git").mkdir()
    return home, repo, cwd


def test_project_root_and_chain(tmp_path: Path):
    _, repo, cwd = make_repo(tmp_path)
    assert project_root(cwd) == repo
    assert project_chain(cwd) == [repo, repo / "pkg", cwd]


def test_worktree_git_file_counts_as_root(tmp_path: Path):
    (tmp_path / ".git").write_text("gitdir: /elsewhere\n")
    assert project_root(tmp_path / "x") == tmp_path


def test_outside_git_only_cwd(tmp_path: Path):
    cwd = tmp_path / "a" / "b"
    cwd.mkdir(parents=True)
    (tmp_path / "a" / "AGENTS.md").write_text("上层的不该被捎进来")
    assert project_chain(cwd) == [cwd]
    assert find_instruction_files(cwd, tmp_path / "home", NAMES) == []


def test_order_is_user_then_root_to_cwd(tmp_path: Path):
    home, repo, cwd = make_repo(tmp_path)
    (home / "AGENTS.md").write_text("个人")
    (repo / "AGENTS.md").write_text("仓库")
    (cwd / "AGENTS.md").write_text("子目录")
    found = find_instruction_files(cwd, home, NAMES)
    assert found == [
        (home / "AGENTS.md", "user"),
        (repo / "AGENTS.md", "project"),
        (cwd / "AGENTS.md", "project"),
    ]


def test_claude_md_is_fallback_per_directory(tmp_path: Path):
    home, repo, cwd = make_repo(tmp_path)
    (repo / "AGENTS.md").write_text("通用")
    (repo / "CLAUDE.md").write_text("@AGENTS.md")  # 有 AGENTS.md 就不看它
    (cwd / "CLAUDE.md").write_text("只给 Claude Code 写过")
    paths = [path for path, _ in find_instruction_files(cwd, home, NAMES)]
    assert paths == [repo / "AGENTS.md", cwd / "CLAUDE.md"]


def test_same_file_listed_once(tmp_path: Path):
    """cwd 就是数据目录的时候，同一份文件不重复注入。"""
    (tmp_path / "AGENTS.md").write_text("x")
    found = find_instruction_files(tmp_path, tmp_path, NAMES)
    assert found == [(tmp_path / "AGENTS.md", "user")]


def test_empty_files_are_skipped(tmp_path: Path):
    home, repo, cwd = make_repo(tmp_path)
    (repo / "AGENTS.md").write_text("  \n")
    assert load_instructions(cwd, home, NAMES, 1000) == []


def test_budget_truncates_and_points_to_file(tmp_path: Path):
    home, repo, cwd = make_repo(tmp_path)
    (home / "AGENTS.md").write_text("甲" * 600)
    (repo / "AGENTS.md").write_text("乙" * 600)
    files = load_instructions(cwd, home, NAMES, 1000)
    assert [item.truncated for item in files] == [False, True]
    assert files[1].content.startswith("乙" * 400)
    assert "乙" * 401 not in files[1].content
    assert f"原文 600 字，完整内容用 read_file 读 {repo / 'AGENTS.md'}" in files[1].content
    assert files[1].size == 600


def test_section_labels_scope(tmp_path: Path):
    home, repo, cwd = make_repo(tmp_path)
    (home / "AGENTS.md").write_text("回答用中文")
    (repo / "AGENTS.md").write_text("先跑测试")
    section = instructions_section(load_instructions(cwd, home, NAMES, 10_000))
    assert section.startswith("# 项目指令")
    assert f"## {home / 'AGENTS.md'}（个人指令，所有项目通用）\n\n回答用中文" in section
    assert f"## {repo / 'AGENTS.md'}（项目指令）\n\n先跑测试" in section
    assert section.index("回答用中文") < section.index("先跑测试")
