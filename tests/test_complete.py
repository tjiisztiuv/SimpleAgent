"""REPL 的斜杠命令补全：候选怎么算、readline 回调的协议、REPL 给的候选对不对。"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from simpleagent.config import Config
from simpleagent.llm.fake import FakeLLM
from simpleagent.ui import complete
from simpleagent.ui.complete import SlashCompleter
from simpleagent.ui.repl import COMMANDS, HELP, Repl


@pytest.fixture
def completer() -> SlashCompleter:
    return SlashCompleter(
        ["compact", "context", "model", "debug", "release", "usage", "usage"],
        {"model": ["b", "a"], "debug": ["off", "on", "verbose", "full"]},
    )


def test_command_names(completer: SlashCompleter):
    assert completer.candidates("/") == [
        "/compact",
        "/context",
        "/debug",
        "/model",
        "/release",
        "/usage",  # 内置命令和同名技能只列一次
    ]
    assert completer.candidates("/co") == ["/compact", "/context"]
    assert completer.candidates("/rel") == ["/release"]
    assert completer.candidates("/zzz") == []


def test_first_argument(completer: SlashCompleter):
    assert completer.candidates("/model ") == ["a", "b"]
    assert completer.candidates("/debug v") == ["verbose"]
    assert completer.candidates("/debug o") == ["off", "on"]
    assert completer.candidates("/compact ") == []  # 没有给候选的命令不补
    assert completer.candidates("/model a b") == []  # 只补第一个参数


def test_plain_text_is_not_completed(completer: SlashCompleter):
    assert completer.candidates("") == []
    assert completer.candidates("帮我看看 /co") == []
    assert completer.candidates(" /co") == []  # REPL 也只把行首的 / 当命令


def test_readline_protocol(completer: SlashCompleter, monkeypatch: pytest.MonkeyPatch):
    class FakeReadline:
        buffer = "/debug o 后面的字"
        endidx = len("/debug o")

        def get_line_buffer(self) -> str:
            return self.buffer

        def get_endidx(self) -> int:
            return self.endidx

    fake = FakeReadline()
    monkeypatch.setattr(complete, "readline", fake)

    def tab(text: str) -> list[str]:
        """按一次 Tab：readline 从 state=0 一直问到拿到 None。"""
        got = []
        while (item := completer.complete(text, len(got))) is not None:
            got.append(item)
        return got

    assert tab("o") == ["off", "on"]  # 只看光标之前的部分
    fake.buffer, fake.endidx = "/mo", 3
    assert tab("/mo") == ["/model "]  # 唯一候选补上空格，接着输入参数


def test_help_lists_every_command():
    for name, (args, _) in COMMANDS.items():
        assert f"/{name} {args}".rstrip() in HELP
    # 中文参数按显示宽度对齐：说明都从同一列开始
    assert "  /model [name]   查看或切换模型" in HELP
    assert "  /compact [重点] 把早期对话压成摘要" in HELP


async def test_every_listed_command_is_handled(config: Config, sa_home: Path):
    """表里列出的命令都要有实现，不能落到「未知命令」。"""
    out = io.StringIO()
    repl = Repl(config, llm_factory=lambda n, p: FakeLLM([], name=n, profile=p), out=out)
    for name in COMMANDS:
        if name != "exit":
            await repl.handle(f"/{name}")
    assert "未知命令" not in out.getvalue()
    assert await repl.handle("/exit") is False


async def test_bare_slash_shows_help(config: Config, sa_home: Path):
    out = io.StringIO()
    repl = Repl(config, llm_factory=lambda n, p: FakeLLM([], name=n, profile=p), out=out)
    await repl.handle("/")
    assert "/model [name]" in out.getvalue()
    assert "未知命令" not in out.getvalue()


def test_repl_completer_includes_skills_and_profiles(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    skill = tmp_path / ".agents" / "skills" / "release"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: release\ndescription: 发版流程\n---\n正文\n")
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    repl = Repl(config, llm_factory=lambda n, p: FakeLLM([], name=n, profile=p), out=io.StringIO())
    c = repl.completer()
    assert "/release" in c.candidates("/")
    assert c.candidates("/re") == ["/release"]
    assert c.candidates("/model ") == ["a", "b"]
    assert c.candidates("/debug f") == ["full"]
    assert c.candidates("/mode ") == ["full", "read-only", "workspace"]
