"""项目指令：会话开始时把 AGENTS.md 读进 system prompt。

AGENTS.md 已经是各家 agent 共用的约定（Claude Code / Codex / Cursor / OpenCode 都读），
所以不发明自己的格式，就读它。找的范围：

1. 个人指令：数据目录下的 `AGENTS.md`（`~/.simpleagent/AGENTS.md`），所有项目通用
2. 项目指令：从项目根目录（往上找到的第一个有 `.git` 的目录）一路到 cwd，每一层找一份；
   不在 git 仓库里就只看 cwd 本身——不然在 ~/Downloads 里跑，会把 ~ 下面的也捎进来

每个目录按 `filenames` 的顺序取第一个存在的文件（默认 AGENTS.md，没有再找 CLAUDE.md：
为 Claude Code 准备的目录不用再复制一份）。排列从通用到具体，靠后的更具体，冲突时以它为准。

只在会话开始时读一次：system prompt 在会话内不能变，否则前缀缓存全部失效（见 M6）。
会话中途改了 AGENTS.md，下一个会话才生效。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

GIT_MARKER = ".git"  # 目录或文件都算（worktree 里 .git 是文件）


@dataclass(frozen=True)
class InstructionFile:
    path: Path
    scope: Literal["user", "project"]
    content: str  # 放进 system prompt 的内容（可能被截断过）
    size: int  # 原文字数
    truncated: bool = False


def project_root(cwd: Path) -> Path | None:
    """cwd 所在的 git 仓库根目录（含 cwd 本身）；不在仓库里返回 None。"""
    for directory in (cwd, *cwd.parents):
        if (directory / GIT_MARKER).exists():
            return directory
    return None


def project_chain(cwd: Path) -> list[Path]:
    """项目根目录到 cwd 的每一层（从外到内）；不在仓库里只有 cwd。"""
    root = project_root(cwd)
    if root is None:
        return [cwd]
    chain = [cwd, *cwd.parents]
    return list(reversed(chain[: chain.index(root) + 1]))


def find_instruction_files(
    cwd: Path, home: Path, filenames: list[str]
) -> list[tuple[Path, Literal["user", "project"]]]:
    """按「个人 → 项目根 → … → cwd」的顺序列出存在的指令文件。同一个文件只出现一次。"""
    found: list[tuple[Path, Literal["user", "project"]]] = []
    seen: set[Path] = set()
    candidates: list[tuple[Path, Literal["user", "project"]]] = [(home, "user")]
    candidates += [(directory, "project") for directory in project_chain(cwd)]
    for directory, scope in candidates:
        path = _first_file(directory, filenames)
        if path is None:
            continue
        key = path.resolve()
        if key not in seen:  # cwd 就是数据目录之类的情况
            seen.add(key)
            found.append((path, scope))
    return found


def load_instructions(
    cwd: Path, home: Path, filenames: list[str], max_chars: int
) -> list[InstructionFile]:
    """读出所有指令文件。总字数超过 max_chars 时，超出的部分截掉，并告诉模型原文在哪。

    按顺序分预算：前面（通用）的先占。截断时留一句路径，模型需要时可以自己 read_file 读全文。
    """
    files: list[InstructionFile] = []
    budget = max_chars
    for path, scope in find_instruction_files(cwd, home, filenames):
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not text:
            continue
        content, truncated = text, False
        if len(text) > budget:
            content = text[: max(budget, 0)].rstrip()
            content += f"\n\n…（已截断：原文 {len(text)} 字，完整内容用 read_file 读 {path}）"
            truncated = True
        budget -= len(text)
        files.append(InstructionFile(path, scope, content, len(text), truncated))
    return files


def instructions_section(files: list[InstructionFile]) -> str:
    """system prompt 里的「项目指令」一节。"""
    parts = [
        "# 项目指令\n\n"
        "下面是用户写给你的长期指令（AGENTS.md），按从通用到具体排列。"
        "和上文的默认要求冲突时以这里为准；这几份之间冲突时，以靠后（更具体）的为准。"
    ]
    for item in files:
        label = "个人指令，所有项目通用" if item.scope == "user" else "项目指令"
        parts.append(f"## {item.path}（{label}）\n\n{item.content}")
    return "\n\n".join(parts)


def _first_file(directory: Path, filenames: list[str]) -> Path | None:
    for name in filenames:
        path = directory / name
        if path.is_file():
            return path
    return None
