"""write_file：新建或覆盖文件（父目录自动创建）。

只有 content 一个必填参数：整篇写入，不做局部修改（局部修改用 edit_file）。
整篇写入比打补丁好判断结果，代价是改一行也要重写全文，所以大文件优先用 edit_file。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from simpleagent.permissions import Scope
from simpleagent.tools.base import ToolContext, ToolError, tool

DESCRIPTION = (
    "把 content 整个写入文件：文件不存在就新建（父目录自动创建），已存在就覆盖。"
    "改一个字也要传完整内容，所以大文件改用 edit_file。写完会报告行数和字节数。"
)


class WriteFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(..., description="要写的文件，绝对路径或相对工作目录的路径")
    content: str = Field(..., description="文件的完整内容")


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@tool(
    name="write_file",
    description=DESCRIPTION,
    readonly=False,
    permission="ask",
    scope=lambda args, ctx: Scope(paths=(ctx.resolve(args.path),), file_edit=True),
)
async def write_file(args: WriteFileArgs, ctx: ToolContext) -> str:
    path = ctx.resolve(args.path)
    if path.is_dir():
        raise ToolError(f"是一个目录，不能写入：{path}")
    existed = path.exists()
    try:
        await asyncio.to_thread(_write, path, args.content)
    except OSError as e:
        raise ToolError(f"写入失败：{path}（{e.strerror or e}）") from e
    lines = len(args.content.splitlines())
    size = len(args.content.encode("utf-8"))
    action = "已覆盖" if existed else "已新建"
    return f"{action} {path}（{lines} 行 / {size:,} 字节）"
