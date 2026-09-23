"""Markdown 文件开头的 YAML frontmatter：技能（SKILL.md）和记忆文件共用。

    ---
    name: weekly-report
    description: 汇总本周的提交和待办，写成周报
    ---
    正文……

只实现用得到的 YAML 子集，不引 PyYAML：依赖尽量少是这个项目的原则，而 frontmatter 里真正要读的
只有几个顶层字符串字段。支持的写法：

- 顶层 `key: value`，值可以是普通写法、单引号、双引号，缩进的下一行算续行（折成一个空格）
- `|` / `>` 块（保留换行 / 折成空格），块内容按缩进收集
- 嵌套的 map / list（`metadata:` 下面缩进的内容、`- item`）原样跳过，值记成空串

解析不了的行直接跳过，不报错：技能可能是给别的工具写的，多几个我们不认识的字段很正常。
只有 frontmatter 没有结束的 `---` 算错误——那说明文件本身写坏了。
"""

from __future__ import annotations

import json
import re

_KEY_RE = re.compile(r"([A-Za-z_][\w-]*)\s*:(?:\s+(.*))?$")
_NESTED_RE = re.compile(r"(-\s|-$|[A-Za-z_][\w-]*\s*:(\s|$))")
_PLAIN_NEEDS_QUOTES = re.compile(r"(^[\s'\"&*!|>%@`{\[#-])|(:\s)|(\s#)|(:$)|(\s$)")


class FrontmatterError(ValueError):
    """frontmatter 写坏了（比如缺结束的 ---）。"""


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """拆成 (顶层字段, 正文)。没有 frontmatter 时返回 ({}, 原文)。字段值一律是字符串。"""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() in ("---", "...")), None)
    if end is None:
        raise FrontmatterError("frontmatter 没有结束的 ---")
    body = "\n".join(lines[end + 1 :]).strip("\n")
    return _parse(lines[1:end]), body


def format_frontmatter(fields: dict[str, str], body: str) -> str:
    """把字段和正文拼回文件内容。值需要时加双引号（JSON 字符串也是合法的 YAML 双引号字符串）。"""
    lines = ["---"]
    for key, value in fields.items():
        value = " ".join(value.split())  # frontmatter 里只放单行值
        if not value or _PLAIN_NEEDS_QUOTES.search(value):
            value = json.dumps(value, ensure_ascii=False)
        lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.strip("\n") + "\n"


def is_true(value: str | None) -> bool:
    return (value or "").strip().lower() in ("true", "yes", "on")


def _parse(lines: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#") or line[0] in " \t":
            continue  # 空行、注释、不属于任何顶层字段的缩进行
        match = _KEY_RE.match(line)
        if match is None:
            continue
        # 顶层字段的值 = 冒号后面的部分 + 紧跟着的缩进行（和空行）
        continuation: list[str] = []
        while i < len(lines) and (not lines[i].strip() or lines[i][0] in " \t"):
            continuation.append(lines[i])
            i += 1
        fields[match.group(1)] = _value((match.group(2) or "").strip(), continuation)
    return fields


def _value(head: str, continuation: list[str]) -> str:
    if head[:1] in ("|", ">"):
        return _block(head[0], continuation)
    rest = [line.strip() for line in continuation]
    if head[:1] in ('"', "'"):
        return _quoted(" ".join([head, *(part for part in rest if part)]))
    if not head:
        first = next((part for part in rest if part), "")
        if not first or _NESTED_RE.match(first):
            return ""  # 嵌套的 map / list，不是字符串
    return _fold([_strip_comment(head), *rest]).strip()


def _block(style: str, lines: list[str]) -> str:
    """`|` 保留换行，`>` 把相邻行折成一个空格（空行还是换行）。缩进按第一行去掉。"""
    content = list(lines)
    while content and not content[-1].strip():
        content.pop()
    indent = min((len(line) - len(line.lstrip()) for line in content if line.strip()), default=0)
    content = [line[indent:] for line in content]
    return ("\n".join(content) if style == "|" else _fold(content)).strip()


def _fold(lines: list[str]) -> str:
    """YAML 的折行：相邻的非空行用空格连起来，空行变成一个换行。"""
    out = ""
    for line in lines:
        if not line.strip():
            out += "\n"
        elif out and not out.endswith("\n"):
            out += " " + line.strip()
        else:
            out += line.strip()
    return out


def _quoted(text: str) -> str:
    quote = text[0]
    if quote == "'":
        # 单引号里只有 '' 一种转义
        end = 1
        while end < len(text):
            if text[end] == "'":
                if text[end + 1 : end + 2] == "'":
                    end += 2
                    continue
                return text[1:end].replace("''", "'")
            end += 1
        return text[1:].replace("''", "'")
    end = 1
    while end < len(text):
        if text[end] == "\\":
            end += 2
            continue
        if text[end] == '"':
            try:
                return json.loads(text[: end + 1])
            except json.JSONDecodeError:  # YAML 独有的转义（\x、\e 之类），退回原文
                return text[1:end]
        end += 1
    return text[1:]


def _strip_comment(text: str) -> str:
    """普通写法的值里，空格加 # 之后是注释（和 YAML 一致）。"""
    match = re.search(r"\s#", text)
    return text[: match.start()] if match else text
