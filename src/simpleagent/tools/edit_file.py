"""edit_file：把 old_string 替换成 new_string。

要求 old_string 在文件里唯一（或显式 replace_all），
否则模型可能改到它没看过的那一份；宁可报错让它把 old_string 写长一点。
返回 unified diff，让模型和用户都能一眼确认改了什么。
文件原来的换行符（LF / CRLF）保持不变。
"""

from __future__ import annotations

import asyncio
import difflib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from simpleagent.permissions import Scope
from simpleagent.tools.base import ToolContext, ToolError, tool
from simpleagent.tools.walk import is_binary

CONTEXT_LINES = 2  # diff 里改动前后各保留几行

DESCRIPTION = (
    "修改文件：把 old_string 精确替换成 new_string。"
    "old_string 必须和文件内容完全一致（包括缩进和换行），且在文件里只出现一次；"
    "出现多次时要加参数 replace_all=true，或者把 old_string 写长一点让它唯一。"
    "返回改动前后的 diff。改前不确定原文就先用 read_file。"
)


class EditFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(..., description="要修改的文件，绝对路径或相对工作目录的路径")
    old_string: str = Field(..., description="要被替换的原文，必须和文件内容完全一致")
    new_string: str = Field("", description="替换后的新内容；传空串表示删除 old_string")
    replace_all: bool = Field(
        False, description="为 true 时替换所有匹配；默认 false，此时 old_string 必须唯一"
    )


def _replace(text: str, old: str, new: str, replace_all: bool) -> tuple[str, int]:
    """返回 (新文本, 替换了几处)；0 处或多处（且没开 replace_all）时报错。"""
    count = text.count(old)
    if count == 0:
        raise ToolError(
            "没找到 old_string（文件共"
            f" {len(text.splitlines())} 行）：缩进、空格、换行都要完全一致，"
            "先用 read_file 确认原文"
        )
    if count > 1 and not replace_all:
        raise ToolError(
            f"old_string 在文件里出现 {count} 次，不确定要改哪一处："
            "把 old_string 写长一点让它唯一，或者用 replace_all=true 替换全部"
        )
    return (text.replace(old, new) if replace_all else text.replace(old, new, 1)), count


def _to_crlf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


def _diff(before: str, after: str, path: Path) -> str:
    # diff 只给人和模型看，统一成 \n，免得每行末尾带个 \r
    diff = difflib.unified_diff(
        before.replace("\r\n", "\n").splitlines(keepends=True),
        after.replace("\r\n", "\n").splitlines(keepends=True),
        fromfile=f"修改前 {path.name}",
        tofile=f"修改后 {path.name}",
        n=CONTEXT_LINES,
    )
    return "".join(diff)


def _edit(path: Path, old: str, new: str, replace_all: bool) -> str:
    if is_binary(path):
        raise ToolError(f"看起来是二进制文件，不能用文本替换修改：{path}")
    try:
        # newline=""：原样读写换行符。默认模式会把 \r\n 读成 \n 再写回 \n，
        # 改一行就把整个 Windows 风格文件的换行符全换了
        with path.open(encoding="utf-8", newline="") as f:
            before = f.read()
    except UnicodeDecodeError as e:
        raise ToolError(f"不是 UTF-8 文本，改不了：{path}（{e.reason}）") from e
    if "\r\n" in before and "\r" not in old:
        # 模型给的 old_string 一般是 \n 换行：按文件的换行风格转换后再匹配
        old, new = _to_crlf(old), _to_crlf(new)
    after, count = _replace(before, old, new, replace_all)
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(after)
    where = f"已修改 {path}（替换 {count} 处）"
    return f"{where}\n{_diff(before, after, path)}"


@tool(
    name="edit_file",
    description=DESCRIPTION,
    readonly=False,
    permission="ask",
    scope=lambda args, ctx: Scope(paths=(ctx.resolve(args.path),), file_edit=True),
)
async def edit_file(args: EditFileArgs, ctx: ToolContext) -> str:
    path = ctx.resolve(args.path)
    if not path.exists():
        raise ToolError(f"文件不存在：{path}；要新建文件用 write_file")
    if path.is_dir():
        raise ToolError(f"是一个目录：{path}")
    if not args.old_string:
        raise ToolError("old_string 不能为空")
    try:
        return await asyncio.to_thread(
            _edit, path, args.old_string, args.new_string, args.replace_all
        )
    except PermissionError as e:
        raise ToolError(f"没有权限写入：{path}") from e
