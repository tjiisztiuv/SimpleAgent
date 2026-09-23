"""System prompt 组装：基础提示词 + 环境信息 + M7 的项目指令、长期记忆、技能列表。

    基础提示词（config.system_prompt）
    # 环境            日期、系统、工作目录
    # 项目指令        AGENTS.md（个人 → 项目根 → … → cwd）
    # 长期记忆        MEMORY.md 索引的快照 + 什么时候该写记忆
    # 技能            每个技能一行：名字 + 描述
    （MCP server 的 instructions 由前端在 MCP 启动后追加到最后）

整个会话里不变：前缀缓存按字节匹配，这里任何一处变了，后面的全部作废。
"""

from __future__ import annotations

import platform
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from simpleagent.knowledge import Knowledge


def build_system_prompt(
    base: str,
    cwd: Path | None = None,
    now: datetime | None = None,
    knowledge: Knowledge | None = None,
) -> str:
    now = now or datetime.now()
    cwd = cwd or Path.cwd()
    weekday = "一二三四五六日"[now.weekday()]
    # 只精确到日期：system prompt 在会话内保持不变，才能持续命中前缀缓存
    prompt = (
        f"{base}\n\n"
        "# 环境\n"
        f"- 日期：{now:%Y-%m-%d}（星期{weekday}）\n"
        f"- 系统：{platform.system()} {platform.release()}\n"
        f"- 工作目录：{cwd}"
    )
    if knowledge is not None:
        prompt += knowledge.prompt_section()
    return prompt
