"""sa mcp list：真的启动一遍每个 server，列出状态和工具；有失败时退出码为 1。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from simpleagent.cli import main
from simpleagent.config import config_path

FAKE_SERVER = Path(__file__).parent.parent / "fixtures" / "mcp" / "fake_mcp_server.py"
BASE = 'default_profile = "a"\n[profiles.a]\nbase_url = "http://a"\nmodel = "m"\n'


def write_config(extra: str = "") -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(BASE + extra, encoding="utf-8")


def fake_block(name: str, *flags: str) -> str:
    # JSON 字符串同时也是合法的 TOML 字符串，路径里的特殊字符不用自己转义
    args = json.dumps([str(FAKE_SERVER), *flags])
    return f"[mcp_servers.{name}]\ncommand = {json.dumps(sys.executable)}\nargs = {args}\n"


def test_mcp_list_ok(sa_home: Path, capsys: pytest.CaptureFixture[str]):
    write_config(fake_block("fake") + 'disabled_tools = ["crash"]\n')
    assert main(["mcp", "list"]) == 0
    out = capsys.readouterr().out
    assert "fake   ✓ 旧协议 2025-11-25 · fake-mcp 1.2.3 · 8 个工具" in out
    assert "mcp__fake__echo" in out and "免确认 · 可并行" in out
    assert "mcp__fake__crash" not in out


def test_mcp_list_failure_exit_code(sa_home: Path, capsys: pytest.CaptureFixture[str]):
    write_config(fake_block("fake") + '[mcp_servers.ghost]\ncommand = "definitely-not-a-cmd"\n')
    assert main(["mcp", "list"]) == 1
    out = capsys.readouterr().out
    assert "fake    ✓" in out
    assert "ghost   ✗ 连接 MCP server ghost 失败：找不到命令 definitely-not-a-cmd" in out


def test_mcp_list_without_servers(sa_home: Path, capsys: pytest.CaptureFixture[str]):
    write_config()
    assert main(["mcp", "list"]) == 0
    assert "还没有配置 MCP server" in capsys.readouterr().out
