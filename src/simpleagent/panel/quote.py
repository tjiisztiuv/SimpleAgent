"""把一条控制面板消息「引用」进一次输入：调度者照着消息拆任务、派空间。

拼出来的文本长这样。用户写的要求放最前面：会话标题取前 40 字，左栏一眼看得出是哪件事。

    把异常的几笔查一下

    【引用消息】每日开销汇总
    来源：定时 · 2026-09-25 08:00 · 注意
    关联会话：空间 code（sp_xxx） · 会话 se_xxx    ← 消息指向某个会话时才有
    链接：https://…                                 ← 有链接才有
    正文：
    ……
    【引用结束】

两个标记之间是材料，不是指令：正文可能来自邮件、脚本投递。调度者的 prompt 说明了这一点，
runner 靠 `has_quote` 认出「这个调度会话带过引用」，让 command_tools 派发前一律先出计划卡。
正文和标题里自带的标记会被换掉，免得伪造出「引用已经结束」，把后面的话冒充成用户的要求。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

QUOTE_OPEN = "【引用消息】"
QUOTE_CLOSE = "【引用结束】"
# 调度者只负责拆任务，派发时还要把相关内容抄进任务描述：原文太长就只带开头
QUOTE_MAX_CHARS = 8000
# 只点了引用、一个字没写时的要求。怎么「处理」由调度者的 prompt 说明
DEFAULT_ASK = "处理这条消息"

# 和前端 SOURCE_LABEL / LEVEL_LABEL 同一套叫法
SOURCE_LABELS = {
    "system": "系统",
    "schedule": "定时",
    "mail": "邮件",
    "cli": "脚本",
    "manual": "手动",
}
LEVEL_LABELS = {"info": "信息", "success": "成功", "warn": "注意", "error": "错误"}


def has_quote(text: str) -> bool:
    """这段输入里有没有引用消息。用户自己敲出这个标记也算：只会让调度多一次确认。"""
    return QUOTE_OPEN in text


def _defuse(text: str) -> str:
    return text.replace(QUOTE_OPEN, "〔引用消息〕").replace(QUOTE_CLOSE, "〔引用结束〕")


def _line(value: Any) -> str:
    """单行字段（标题、来源、会话 id、链接）：拍平换行、换掉标记。

    这些字段和正文一样可能来自外部投递（`POST /api/inbox` 的 source / ref 不做校验），
    不能让它们顶出新的一行，也不能夹带标记。
    """
    return " ".join(_defuse(str(value or "")).split())


def _when(ts: Any) -> str:
    try:
        return datetime.fromisoformat(str(ts)).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(ts or "")


def quote_text(
    item: dict[str, Any], instruction: str = "", *, space_name: str | None = None
) -> str:
    """用户的要求 + 引用块。item 是 `PanelStore.get_message` 的结果（带全文）。

    space_name 是消息指向的那个空间的名字，调用方查不到（空间删了）就传 None，只写 id。
    """
    ask = instruction.strip() or DEFAULT_ASK
    source = _line(item.get("source"))
    level = _line(item.get("level"))
    meta = [
        SOURCE_LABELS.get(source, source),
        _when(item.get("ts")),
        LEVEL_LABELS.get(level, level),
    ]
    lines = [
        f"{QUOTE_OPEN}{_line(item.get('title'))}",
        "来源：" + " · ".join(part for part in meta if part),
    ]
    ref = item.get("ref") or {}
    space_id, session_id = _line(ref.get("space_id")), _line(ref.get("session_id"))
    if space_id and session_id:
        where = f"{_line(space_name)}（{space_id}）" if space_name else space_id
        lines.append(f"关联会话：空间 {where} · 会话 {session_id}")
    if url := _line(ref.get("url")):
        lines.append(f"链接：{url}")

    body = _defuse(str(item.get("body") or "").strip())
    if len(body) > QUOTE_MAX_CHARS:
        body = (
            body[:QUOTE_MAX_CHARS]
            + f"\n…（正文共 {len(body)} 字，只引用了前 {QUOTE_MAX_CHARS} 字）"
        )
    lines += ["正文：", body or "（空）", QUOTE_CLOSE]
    return f"{ask}\n\n" + "\n".join(lines)
