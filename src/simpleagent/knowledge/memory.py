"""长期记忆：`<数据目录>/memory/` 下每条记忆一个 Markdown 文件，`MEMORY.md` 是索引。

    memory/
      MEMORY.md                 索引：每行一条 `- [名字](文件) — 一句话说明`
      user-preferences.md       一条记忆：frontmatter（name / description / updated）+ 正文
      workbench-direction.md

和技能一样是「渐进式披露」：会话开始时只把索引放进 system prompt（几十行），正文由模型
用 `memory_read` 按需读。记忆越攒越多，常驻 prompt 的也只是索引。

几个取舍：

- **全用 Markdown 文件**，不上数据库、不上向量检索：人能直接读、直接改，能 grep、能进 git。
  索引就是「策展过的目录」，靠搜索才想得起来的重要事情，说明索引没整理好
- **索引由工具维护**，不让模型自己改 MEMORY.md：写一条记忆只要一次调用，不会漏改索引；
  只动链接到这个文件的那一行，人手动加的标题、分组、备注都保留
- **写入默认要确认**（`[memory] confirm_writes`）：记忆会进之后每个会话的 system prompt，
  被网页或文件里的内容诱导写进一条假记忆，影响的是以后所有会话。`sa run` 里默认直接拒绝
- **本会话的 system prompt 不刷新**：写完只在下一个会话生效，前缀缓存不受影响；
  写记忆的这个会话里，模型本来就从对话里知道这件事
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from simpleagent.knowledge.frontmatter import format_frontmatter
from simpleagent.tools.base import Tool, ToolContext, ToolError

MEMORY_DIRNAME = "memory"
INDEX_FILENAME = "MEMORY.md"
# 索引进 system prompt 的字数上限：社区经验是常驻部分控制在几 KB，多了反而挤占思考的地方
INDEX_MAX_CHARS = 6000
DESCRIPTION_MAX = 150  # 索引里一行说明的字数上限
# 记忆名 = 文件名：字母数字（含中文）、下划线、连字符，不能以连字符开头
NAME_RE = re.compile(r"\w[\w-]{0,63}")
INDEX_HEADER = (
    "# 记忆索引\n\n"
    "每行一条记忆：`- [名字](文件) — 一句话说明`。"
    "由 memory_write / memory_delete 维护，也可以手动整理（改说明、加分组）。\n"
)
CONFIRM_REASON = "会写入长期记忆，之后的每个会话都会读到"


class MemoryStoreError(Exception):
    """记忆操作失败：名字不合法、记忆不存在、写盘失败。消息可以直接给模型看。"""


class MemoryStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def index_path(self) -> Path:
        return self.root / INDEX_FILENAME

    def path_for(self, name: str) -> Path:
        name = name.strip().removesuffix(".md")
        if not NAME_RE.fullmatch(name) or name.upper() == "MEMORY":
            raise MemoryStoreError(
                f"记忆名不合法：{name!r}。用字母、数字、连字符，最长 64 个字符，"
                "如 user-preferences；MEMORY 是索引的名字，不能用"
            )
        return self.root / f"{name}.md"

    def names(self) -> list[str]:
        """已有的记忆（不含索引），按名字排序。"""
        if not self.root.is_dir():
            return []
        return sorted(
            path.stem
            for path in self.root.glob("*.md")
            if path.name != INDEX_FILENAME and path.is_file()
        )

    def read_index(self) -> str:
        try:
            return self.index_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def read(self, name: str) -> str:
        path = self.path_for(name)
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            known = "、".join(self.names()) or "（还没有任何记忆）"
            raise MemoryStoreError(f"没有名为 {path.stem} 的记忆。已有：{known}") from None
        except OSError as e:
            raise MemoryStoreError(f"读取 {path} 失败：{e}") from e

    def write(self, name: str, description: str, content: str) -> tuple[bool, str]:
        """新建或覆盖一条记忆，同步更新索引。返回 (是否新建, 索引里的那一行)。"""
        path = self.path_for(name)
        description = " ".join(description.split())
        if not description:
            raise MemoryStoreError("description 不能为空：索引靠它判断以后要不要读这条记忆")
        if not content.strip():
            raise MemoryStoreError("content 不能为空；要删除这条记忆用 memory_delete")
        created = not path.exists()
        fields = {
            "name": path.stem,
            "description": description,
            "updated": date.today().isoformat(),
        }
        _atomic_write(path, format_frontmatter(fields, content))
        line = index_line(path.stem, description)
        self._update_index(path.stem, line)
        return created, line

    def delete(self, name: str) -> None:
        path = self.path_for(name)
        if not path.exists():
            raise MemoryStoreError(f"没有名为 {path.stem} 的记忆")
        try:
            path.unlink()
        except OSError as e:
            raise MemoryStoreError(f"删除 {path} 失败：{e}") from e
        self._update_index(path.stem, None)

    def check(self) -> tuple[list[str], list[str]]:
        """索引和文件对不上的地方：(没进索引的记忆, 索引里指向不存在文件的名字)。"""
        index = self.read_index()
        linked = set(re.findall(r"\]\((?:\./)?([^)/]+?)\.md\)", index))
        names = set(self.names())
        return sorted(names - linked), sorted(linked - names)

    def _update_index(self, name: str, line: str | None) -> None:
        """把链接到 <name>.md 的那一行换成 line（None 表示删掉），没有就追加。其余内容不动。"""
        text = self.read_index()
        lines = text.splitlines() if text else INDEX_HEADER.splitlines()
        link = re.compile(rf"\]\((?:\./)?{re.escape(name)}\.md\)")
        hits = [i for i, existing in enumerate(lines) if link.search(existing)]
        if line is not None and hits:
            lines[hits[0]] = line
            hits = hits[1:]  # 重复的行顺手去掉
        for i in reversed(hits):
            del lines[i]
        if line is not None and not link.search("\n".join(lines)):
            while lines and not lines[-1].strip():
                lines.pop()
            if lines and not lines[-1].lstrip().startswith("- "):
                lines.append("")  # 接在标题、说明文字后面时空一行，Markdown 里才是独立的列表
            lines.append(line)
        _atomic_write(self.index_path, "\n".join(lines).strip() + "\n")


def index_line(name: str, description: str) -> str:
    description = " ".join(description.split())
    if len(description) > DESCRIPTION_MAX:
        description = description[:DESCRIPTION_MAX] + "…"
    return f"- [{name}]({name}.md) — {description}"


def memory_section(store: MemoryStore, index: str) -> str:
    """system prompt 里的「长期记忆」一节。index 是会话开始时读到的索引快照。"""
    if len(index) > INDEX_MAX_CHARS:
        index = (
            index[:INDEX_MAX_CHARS].rstrip() + f"\n…（索引太长，后面的没放进来：需要时用 grep 搜 "
            f"{store.root}，并提醒用户整理 {INDEX_FILENAME}）"
        )
    shown = f"<memory_index>\n{index}\n</memory_index>" if index else "（还没有任何记忆。）"
    return (
        "# 长期记忆\n\n"
        f"你有一份跨会话的长期记忆，存在 {store.root}：每条记忆一个 Markdown 文件，"
        f"{INDEX_FILENAME} 是索引。下面是会话开始时的索引，"
        "需要某条的细节时用 memory_read 读全文。\n\n"
        f"{shown}\n\n"
        "什么时候写（memory_write）：\n"
        "- 用户明确让你记住什么：马上写，不要等到对话结束\n"
        "- 用户纠正了你的做法、说明了偏好，或者一起定下了以后还用得上的决定：写下来，连同原因\n"
        "- 同一件事更新原来那条（同名覆盖，给完整的新内容），不要另建一条；"
        "记错了或过时了就改掉，或者用 memory_delete 删掉\n"
        "不要记：只对这次对话有用的、从文件或命令能直接查到的、密钥和密码。\n"
        "记忆是写下时的快照，可能已经过时：和眼前的事实冲突时以眼前为准，并顺手更新那条记忆。"
    )


# ------------------------------------------------------------------ 工具


class MemoryNameArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="记忆名，也就是文件名（不带 .md），见索引里的链接")


class MemoryWriteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        description="记忆名 = 文件名（不带 .md），用小写英文和连字符，如 user-preferences；"
        "和已有的同名就是覆盖更新"
    )
    description: str = Field(
        description="一句话说明这条记忆是什么，会写进索引 MEMORY.md；以后靠它判断要不要读全文"
    )
    content: str = Field(
        description="正文（Markdown）。偏好和决定要写上原因；更新时给完整的新内容，不是增量"
    )


def memory_tools(store: MemoryStore, *, confirm_writes: bool = True) -> list[Tool]:
    """三个记忆工具：读是只读、免确认；写和删默认要确认（confirm_writes=False 时直接放行）。

    不声明 scope：记忆目录在工作目录之外，报了路径会被工作目录边界直接拒掉。
    记忆目录是 SimpleAgent 自己的数据，边界要防的是模型乱改用户的文件。
    """

    async def memory_read(args: MemoryNameArgs, ctx: ToolContext) -> str:
        try:
            return store.read(args.name)
        except MemoryStoreError as e:
            raise ToolError(str(e)) from e

    async def memory_write(args: MemoryWriteArgs, ctx: ToolContext) -> str:
        try:
            created, line = store.write(args.name, args.description, args.content)
        except MemoryStoreError as e:
            raise ToolError(str(e)) from e
        action = "新建" if created else "更新"
        return f"已{action}记忆 {store.path_for(args.name)}，索引里对应的一行：\n{line}"

    async def memory_delete(args: MemoryNameArgs, ctx: ToolContext) -> str:
        try:
            store.delete(args.name)
        except MemoryStoreError as e:
            raise ToolError(str(e)) from e
        return f"已删除记忆 {args.name}，索引里的那一行也去掉了"

    permission = "ask" if confirm_writes else "allow"
    return [
        Tool(
            name="memory_read",
            description="读一条长期记忆的全文（索引在 system prompt 里，这里按名字取正文）。",
            args_model=MemoryNameArgs,
            fn=memory_read,
        ),
        Tool(
            name="memory_write",
            description=(
                "新建或覆盖一条长期记忆，并更新索引 MEMORY.md。"
                "用来记住以后的会话还用得上的事：用户的偏好和要求、做过的决定及原因、长期在做的事。"
            ),
            args_model=MemoryWriteArgs,
            fn=memory_write,
            readonly=False,
            permission=permission,
            confirm_reason=CONFIRM_REASON,
        ),
        Tool(
            name="memory_delete",
            description="删除一条过时或记错的长期记忆，同时从索引里去掉。",
            args_model=MemoryNameArgs,
            fn=memory_delete,
            readonly=False,
            permission=permission,
            confirm_reason="会删除一条长期记忆",
        ),
    ]


def _atomic_write(path: Path, text: str) -> None:
    """先写临时文件再改名：写到一半进程没了，原文件还是完整的。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    except OSError as e:
        raise MemoryStoreError(f"写入 {path} 失败：{e}") from e
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError as e:
        Path(tmp).unlink(missing_ok=True)
        raise MemoryStoreError(f"写入 {path} 失败：{e}") from e
