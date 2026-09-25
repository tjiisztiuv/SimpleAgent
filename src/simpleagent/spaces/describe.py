"""空间简介的自动生成：指挥台的调度者靠简介决定把任务派给哪个空间。

    gather_material(space, cwd, titles)   拼出给模型看的素材；什么都读不到返回 None
    describe_space(llm, material)         发一次请求，拿回一段 50～100 字的简介

生成结果只是建议，不自动保存：简介直接决定任务派给谁，要人过目。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from simpleagent.events import MessageDone
from simpleagent.permissions import Mode
from simpleagent.spaces.models import DESCRIPTION_MAX, EXECUTOR_LABELS, Space, one_line

# 项目说明：先找指令文件（和 M7 一样，有 AGENTS.md 就不读 CLAUDE.md），再加 README
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
README_FILES = ("README.md", "README", "readme.md")
DOC_CHARS = 2000  # 每份文档只取开头：够看出项目是干什么的
LIST_LIMIT = 50  # 顶层文件列表最多列这么多
TITLE_LIMIT = 10  # 最近几个会话标题

DESCRIBE_SYSTEM = (
    "你在给一个「空间」写简介。空间是一个本地 AI 工作台里的任务容器：绑定一个工作目录和一个执行者，"
    "专门处理某一类任务。调度者会读这段简介，决定要不要把用户的任务派给这个空间。\n\n"
    "要求：\n"
    "- 50～100 字，一段话，不分点、不加标题、不加引号\n"
    "- 写清楚这个空间负责哪类任务、管着什么项目或数据；能看出来的话，再写适合 / 不适合派什么活\n"
    "- 只根据给你的材料写，不要编造材料里没有的能力\n"
    "- 直接输出简介本身，不要任何解释"
)


class DescribeError(Exception):
    """没拿到简介（API 报错、模型没给正文）。"""


def _read_head(path: Path, limit: int = DOC_CHARS) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    text = text.strip()
    if not text:
        return None
    return text[:limit] + ("\n…（后面省略）" if len(text) > limit else "")


def _first_existing(cwd: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        path = cwd / name
        if path.is_file():
            return path
    return None


def _listing(cwd: Path) -> list[str]:
    """顶层文件列表：跳过隐藏文件，目录名带 /。"""
    try:
        entries = sorted(p for p in cwd.iterdir() if not p.name.startswith("."))
    except OSError:
        return []
    names = [f"{p.name}/" if p.is_dir() else p.name for p in entries[:LIST_LIMIT]]
    if len(entries) > LIST_LIMIT:
        names.append(f"…还有 {len(entries) - LIST_LIMIT} 项")
    return names


def gather_material(space: Space, cwd: Path | None, titles: list[str]) -> str | None:
    """拼出给模型看的素材。目录里什么都没有、也没跑过会话时返回 None：没东西可摘要。

    cwd 是空间实际的工作目录（generic 空间是 spaces/<id>/tmp），调用方用 Runner.cwd_for 算好。
    """
    docs: list[tuple[str, str]] = []
    names: list[str] = []
    if cwd is not None and cwd.is_dir():
        for group in (INSTRUCTION_FILES, README_FILES):
            path = _first_existing(cwd, group)
            if path is not None and (text := _read_head(path)):
                docs.append((path.name, text))
        names = _listing(cwd)
    seen: set[str] = set()
    recent: list[str] = []
    for title in titles:
        title = title.strip()
        if title and title != "新会话" and title not in seen:
            seen.add(title)
            recent.append(title)
    recent = recent[:TITLE_LIMIT]
    if not docs and not names and not recent:
        return None

    kind = "绑定目录" if space.kind == "agent" else "通用任务（没有固定目录，产物落在临时目录）"
    executor = EXECUTOR_LABELS.get(space.executor, space.executor)
    parts = [
        f"# 空间：{space.name}",
        f"- 形态：{kind}",
        f"- 执行者：{executor}",
    ]
    if space.executor != "simpleagent":
        full = space.effective_mode(Mode.READ_ONLY) is Mode.FULL
        parts.append(f"- 权限：{'全放行（能改文件）' if full else '只读'}")
    if cwd is not None:
        parts.append(f"- 工作目录：{cwd}")
    for name, text in docs:
        parts += ["", f"## {name}（开头）", text]
    if names:
        parts += ["", "## 工作目录顶层", "、".join(names)]
    if recent:
        parts += ["", "## 最近跑过的会话标题", *(f"- {t}" for t in recent)]
    return "\n".join(parts)


def clip(text: str) -> str:
    """模型的输出整理成一行；偶尔会带引号、控制字符、超长，这里兜底，保证能直接存进 description。"""
    text = one_line(text).strip("\"'“”「」 ")
    if len(text) > DESCRIPTION_MAX:
        text = text[: DESCRIPTION_MAX - 1].rstrip() + "…"
    return text


async def describe_space(llm: Any, material: str) -> str:
    """发一次请求拿简介。不带工具：这里只要一段话。"""
    messages = [
        {"role": "system", "content": DESCRIBE_SYSTEM},
        {"role": "user", "content": material},
    ]
    done: MessageDone | None = None
    try:
        async for event in llm.stream(messages):
            if isinstance(event, MessageDone):
                done = event
    except Exception as e:  # noqa: BLE001  API 报错统一转成 DescribeError，HTTP 层给 502
        raise DescribeError(f"{type(e).__name__}: {e}") from e
    text = clip(str(done.message.get("content") or "")) if done else ""
    if not text:
        raise DescribeError("模型没有给出简介")
    return text
