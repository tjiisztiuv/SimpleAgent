"""M7：frontmatter 解析。只认技能和记忆用得到的 YAML 子集，认不出的字段跳过、不报错。"""

from __future__ import annotations

import pytest

from simpleagent.knowledge.frontmatter import (
    FrontmatterError,
    format_frontmatter,
    is_true,
    split_frontmatter,
)


def test_plain_fields_and_body():
    fields, body = split_frontmatter(
        "---\nname: weekly-report\ndescription: 汇总本周的提交\n---\n\n# 步骤\n1. 看 git log\n"
    )
    assert fields == {"name": "weekly-report", "description": "汇总本周的提交"}
    assert body == "# 步骤\n1. 看 git log"


def test_no_frontmatter_returns_original_text():
    assert split_frontmatter("# 只是正文\n") == ({}, "# 只是正文\n")


def test_unterminated_frontmatter_is_an_error():
    with pytest.raises(FrontmatterError):
        split_frontmatter("---\nname: x\n正文没有结束标记\n")


def test_quoted_values():
    fields, _ = split_frontmatter(
        '---\na: "带: 冒号和 \\"引号\\""\nb: \'it\'\'s\'\nc: "多行\n  续上"\n---\n'
    )
    assert fields == {"a": '带: 冒号和 "引号"', "b": "it's", "c": "多行 续上"}


def test_block_scalars():
    text = (
        "---\n"
        "literal: |\n"
        "  第一行\n"
        "  第二行\n"
        "folded: >-\n"
        "  折成\n"
        "  一行\n"
        "\n"
        "  新段落\n"
        "name: after\n"
        "---\n"
    )
    fields, _ = split_frontmatter(text)
    assert fields["literal"] == "第一行\n第二行"
    assert fields["folded"] == "折成 一行\n新段落"
    assert fields["name"] == "after"


def test_plain_value_continues_on_indented_lines():
    fields, _ = split_frontmatter(
        "---\ndescription: 从 PDF 里抽表格，\n  也能抽文字\nname: pdf\n---\n"
    )
    assert fields["description"] == "从 PDF 里抽表格， 也能抽文字"


def test_value_starting_on_next_line_is_a_plain_scalar():
    fields, _ = split_frontmatter("---\ndescription:\n  下一行才开始\n---\n")
    assert fields["description"] == "下一行才开始"


def test_nested_maps_and_lists_are_skipped():
    text = (
        "---\n"
        "name: x\n"
        "metadata:\n"
        "  author: me\n"
        "  version: 1\n"
        "tags:\n"
        "- a\n"
        "- b\n"
        "allowed-tools: Bash(git:*) Read\n"
        "---\n"
    )
    fields, _ = split_frontmatter(text)
    assert fields["name"] == "x"
    assert fields["metadata"] == "" and fields["tags"] == ""
    assert "author" not in fields  # 嵌套的键不会冒充顶层字段
    assert fields["allowed-tools"] == "Bash(git:*) Read"


def test_comments_and_bom_and_crlf():
    text = "﻿---\r\n# 注释\r\nname: x # 行尾注释\r\ndescription: C#不算注释\r\n---\r\n正文\r\n"
    fields, body = split_frontmatter(text)
    assert fields == {"name": "x", "description": "C#不算注释"}
    assert body == "正文"


def test_is_true():
    assert is_true("true") and is_true("Yes") and is_true(" on ")
    assert not is_true("false") and not is_true(None) and not is_true("")


def test_format_round_trips_and_quotes_when_needed():
    fields = {"name": "prefs", "description": "偏好: 简洁 #1", "updated": "2026-09-23"}
    text = format_frontmatter(fields, "正文\n")
    assert 'description: "偏好: 简洁 #1"' in text
    assert "name: prefs\n" in text
    assert split_frontmatter(text) == (fields, "正文")


def test_format_collapses_newlines_in_values():
    text = format_frontmatter({"description": "第一行\n第二行"}, "x")
    assert split_frontmatter(text)[0] == {"description": "第一行 第二行"}
