"""web/slash.js 的 / 补全候选：用 node 跑前端文件，在 Python 里断言输出。

和 test_markdown.py 一样不装任何 JS 包；机器上没有 node 就整体跳过。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import simpleagent.web

NODE = shutil.which("node")
SCRIPT = Path(simpleagent.web.__file__).parent / "slash.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="需要 node 来跑前端 JS")

COMMANDS = [
    {"name": "help", "args": "", "desc": "显示可用的命令和技能"},
    {"name": "verify", "args": "", "desc": "跑验证命令"},
    {"name": "model", "args": "<profile>", "desc": "切换模型"},
]
SKILLS = [
    {"name": "code-review", "description": "审查改动"},
    {"name": "help", "description": "和内置命令同名"},
    {"name": "release", "description": "发版流程"},
]
OPTIONS = {
    "commands": COMMANDS,
    "skills": SKILLS,
    "profiles": ["deepseek", "kimi"],
    "current": "kimi",
}


def menu(before: str, options: dict = OPTIONS):
    js = (
        f"const {{ slashMenu }} = require({json.dumps(str(SCRIPT))});"
        "const [before, options] = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
        "process.stdout.write(JSON.stringify(slashMenu(before, options)));"
    )
    out = subprocess.run(
        [NODE, "-e", js],
        input=json.dumps([before, options]),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def labels(result) -> list[str]:
    return [item["label"] for item in result["items"]]


def test_slash_lists_commands_then_skills():
    got = menu("/")
    assert labels(got) == ["/help", "/verify", "/model", "/code-review", "/release"]
    help_, _, model, review, _ = got["items"]
    # 不带参数的命令：选中就执行；带参数的补上空格等着输入
    assert help_ == {
        "label": "/help",
        "hint": "",
        "desc": "显示可用的命令和技能",
        "tag": "",
        "value": "/help",
        "submit": True,
    }
    assert model["value"] == "/model " and model["submit"] is False
    assert review["tag"] == "技能" and review["value"] == "/code-review "
    assert review["submit"] is False


def test_filter_prefix_before_substring():
    assert labels(menu("/re")) == ["/release", "/code-review"]
    assert labels(menu("/RE")) == ["/release", "/code-review"]  # 不分大小写
    assert labels(menu("/m")) == ["/model"]
    assert menu("/zzz") is None


def test_model_argument_lists_profiles():
    got = menu("/model ")
    assert labels(got) == ["deepseek", "kimi"]
    assert got["items"][1]["desc"] == "当前"
    assert got["items"][0]["value"] == "/model deepseek"
    assert got["items"][0]["submit"] is True
    assert labels(menu("/model ki")) == ["kimi"]
    assert labels(menu("/model k")) == ["kimi", "deepseek"]  # 前缀匹配的在前
    assert menu("/model kimi x") is None


def test_no_menu_outside_command_position():
    assert menu("") is None
    assert menu("帮我看看 /re") is None  # 不在开头
    assert menu(" /re") is None  # 发送时也只认开头的 /
    assert menu("/release v1") is None  # 已经在写补充说明了
    assert menu("/re\n第二行") is None


def test_works_without_skills_or_profiles():
    assert labels(menu("/", {"commands": COMMANDS})) == ["/help", "/verify", "/model"]
    assert menu("/model ", {"commands": COMMANDS}) is None
