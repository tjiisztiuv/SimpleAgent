"""M7：对话之外，agent 在会话开始时带上的三样东西——项目指令、长期记忆、技能。

    Knowledge.load(config, cwd)      会话开始时读一次：AGENTS.md、记忆索引快照、技能清单
      .prompt_section()             拼进 system prompt 的几节（顺序固定）
      .tools()                      memory_read / memory_write / memory_delete / load_skill

三者的共同点是「会话内不变」：都在会话开始时定下来，会话中途改了文件，下一个会话才生效。
system prompt 和工具列表在会话内一变，前缀缓存就整段失效（M6 的结论），所以这里宁可晚一步，
也不中途刷新。模型自己写的记忆、改的 AGENTS.md，它在这个会话里本来就知道。

REPL、`sa run`、`sa serve` 三个前端都用它，拼法只有这一份。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from simpleagent.config import Config, home_dir
from simpleagent.knowledge.instructions import (
    InstructionFile,
    instructions_section,
    load_instructions,
)
from simpleagent.knowledge.memory import MEMORY_DIRNAME, MemoryStore, memory_section, memory_tools
from simpleagent.knowledge.skills import SkillCatalog, discover_skills, skill_roots
from simpleagent.tools.base import Tool


@dataclass
class Knowledge:
    instructions: list[InstructionFile] = field(default_factory=list)
    memory: MemoryStore | None = None  # None：记忆关掉了
    memory_index: str = ""  # 会话开始时的索引快照，进 system prompt 的就是它
    confirm_memory_writes: bool = True
    skills: SkillCatalog = field(default_factory=SkillCatalog)

    @classmethod
    def load(cls, config: Config, cwd: Path, home: Path | None = None) -> Knowledge:
        home = home or home_dir()
        instructions = (
            load_instructions(
                cwd, home, config.instructions.filenames, config.instructions.max_chars
            )
            if config.instructions.enabled
            else []
        )
        memory = MemoryStore(home / MEMORY_DIRNAME) if config.memory.enabled else None
        skills = (
            discover_skills(skill_roots(config.skills.dirs, home, cwd))
            if config.skills.enabled
            else SkillCatalog()
        )
        return cls(
            instructions=instructions,
            memory=memory,
            memory_index=memory.read_index() if memory else "",
            confirm_memory_writes=config.memory.confirm_writes,
            skills=skills,
        )

    def tools(self) -> list[Tool]:
        """顺序固定：记忆三个在前，load_skill 在后。和内置工具、MCP 工具一起在会话内不变。"""
        tools: list[Tool] = []
        if self.memory is not None:
            tools += memory_tools(self.memory, confirm_writes=self.confirm_memory_writes)
        if (load_skill := self.skills.tool()) is not None:
            tools.append(load_skill)
        return tools

    def prompt_section(self) -> str:
        """追加到 system prompt 的内容（开头带空行）：项目指令 → 长期记忆 → 技能。都没有是空串。"""
        parts = []
        if self.instructions:
            parts.append(instructions_section(self.instructions))
        if self.memory is not None:
            parts.append(memory_section(self.memory, self.memory_index))
        if section := self.skills.prompt_section():
            parts.append(section)
        return "".join(f"\n\n{part}" for part in parts)

    def summary(self) -> str:
        """启动时打的那一行：项目指令 AGENTS.md · 记忆 3 条 · 技能 2 个。什么都没有是空串。"""
        parts = []
        if self.instructions:
            names = "、".join(_short(item.path) for item in self.instructions)
            parts.append(f"项目指令 {names}")
        if self.memory is not None and (count := len(self.memory.names())):
            parts.append(f"记忆 {count} 条")
        skills = self.skills
        if skills.skills or skills.errors:
            text = f"技能 {len(skills.skills)} 个"
            if skills.errors:
                text += f"（{len(skills.errors)} 个写坏了，/skills 看详情）"
            parts.append(text)
        return " · ".join(parts)


def _short(path: Path) -> str:
    """显示用：家目录缩成 ~。"""
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


__all__ = ["Knowledge"]
