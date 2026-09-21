"""M3 headless 测试：`sa run` 前端的无人值守行为，以及 sessions / --resume 的 CLI 接线。

核心要证明的是：**没有人在终端前时，写操作既不会被执行也不会卡住等待**。
全部用 FakeLLM，不联网。"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from simpleagent.agent.session import Session
from simpleagent.cli import list_sessions, resolve_resume, session_store
from simpleagent.config import Config, Profile
from simpleagent.llm.fake import FakeLLM, Script
from simpleagent.ui.headless import Headless


def make_headless(
    tmp_path: Path,
    config: Config,
    scripts: list[Script],
    allowed: tuple[str, ...] = (),
) -> tuple[Headless, FakeLLM]:
    factory_calls: list[FakeLLM] = []

    def factory(name: str, profile: Profile) -> FakeLLM:
        fake = FakeLLM(list(scripts), name=name, profile=profile)
        factory_calls.append(fake)
        return fake

    out = io.StringIO()
    frontend = Headless(
        config,
        cwd=tmp_path,
        allowed_tools=allowed,
        llm_factory=factory,
        out=out,
    )
    return frontend, factory_calls[0]


# ------------------------------------------------------------ 1. 基本输出
async def test_headless_prints_reply_and_usage(config: Config, tmp_path: Path):
    frontend, _ = make_headless(
        tmp_path,
        config,
        [{"content": "做完了", "usage": {"prompt_tokens": 5, "completion_tokens": 2}}],
    )
    assert await frontend.run("整理一下") == 0
    assert "做完了" in frontend.out.getvalue()
    assert "输入 5" in frontend.out.getvalue()


async def test_headless_records_history(config: Config, tmp_path: Path):
    frontend, _ = make_headless(tmp_path, config, ["收到"])
    await frontend.run("你好")
    assert frontend.session.messages == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "收到"},
    ]


# ------------------------------------------------------------ 2. 无人值守的权限
async def test_write_is_refused_without_allow(config: Config, tmp_path: Path):
    """没有白名单就没人可以按 y：模型拿到的是一条明确的拒绝，而且文件没被创建。"""
    frontend, _ = make_headless(
        tmp_path,
        config,
        [
            {
                "tool_calls": [
                    {"name": "write_file", "arguments": {"path": "a.txt", "content": "x"}}
                ]
            },
            "好吧",
        ],
    )
    assert await frontend.run("写个文件") == 0
    assert not (tmp_path / "a.txt").exists()
    tool_message = frontend.session.messages[-2]
    assert tool_message["role"] == "tool"
    assert "已被拒绝" in tool_message["content"]
    assert "会改动文件或执行命令" in tool_message["content"]


async def test_allow_list_lets_the_tool_run(config: Config, tmp_path: Path):
    frontend, _ = make_headless(
        tmp_path,
        config,
        [
            {
                "tool_calls": [
                    {"name": "write_file", "arguments": {"path": "a.txt", "content": "x"}}
                ]
            },
            "写好了",
        ],
        allowed=("write_file",),
    )
    await frontend.run("写个文件")
    assert (tmp_path / "a.txt").read_text() == "x"


async def test_dangerous_command_is_denied_even_when_allowed(config: Config, tmp_path: Path):
    """`--allow bash` 不等于放行一切：危险命令由 Policy 拦，白名单管不着。"""
    frontend, _ = make_headless(
        tmp_path,
        config,
        [{"tool_calls": [{"name": "bash", "arguments": {"command": "rm -rf ~"}}]}, "遵命"],
        allowed=("bash",),
    )
    await frontend.run("清一下家目录")
    content = frontend.session.messages[-2]["content"]
    assert "拒绝执行" in content
    assert "rm -rf" in content


async def test_write_outside_cwd_is_denied(config: Config, tmp_path: Path):
    outside = tmp_path.parent / "outside.txt"
    frontend, _ = make_headless(
        tmp_path,
        config,
        [
            {
                "tool_calls": [
                    {"name": "write_file", "arguments": {"path": str(outside), "content": "x"}}
                ]
            },
            "明白",
        ],
        allowed=("write_file",),
    )
    await frontend.run("写到外面去")
    assert "工作目录之外" in frontend.session.messages[-2]["content"]
    assert not outside.exists()


# ------------------------------------------------------------ 3. CLI：sessions / --resume
def test_list_sessions_when_empty(config: Config, sa_home: Path, capsys):
    assert list_sessions(session_store(), 20) == 0
    assert "还没有保存的会话" in capsys.readouterr().out


def test_sessions_and_resume_roundtrip(config: Config, sa_home: Path, capsys):
    store = session_store()
    session = store.start(Session("cli-1"))
    session.add({"role": "user", "content": "帮我看看这个项目"})
    session.record_stats(requests=1)

    assert list_sessions(store, 20) == 0
    printed = capsys.readouterr().out
    assert "cli-1" in printed and "帮我看看这个项目" in printed

    # --resume 不带 id：接着最近那次
    restored = resolve_resume(store, "__latest__")
    assert restored is not None and restored.id == "cli-1"
    assert [m["content"] for m in restored.messages] == ["帮我看看这个项目"]

    # 不带 --resume：返回 None，照常新开会话
    assert resolve_resume(store, None) is None


def test_resume_missing_session_raises_cli_error(config: Config, sa_home: Path):
    from simpleagent.cli import CliError

    with pytest.raises(CliError):
        resolve_resume(session_store(), "__latest__")


# ------------------------------------------------------------ MCP（M5）
@pytest.mark.parametrize(
    ("allowed", "expected"),
    [
        ((), "来自 MCP server fake，server 没有标注它是只读的；已被拒绝"),
        (("mcp__fake__image",), "[图片 image/png，约 2.0 KB，未展示给模型]"),
    ],
)
async def test_headless_mcp_tools_follow_whitelist(
    config: Config, fake_mcp, tmp_path: Path, allowed: tuple[str, ...], expected: str
):
    """需要确认的 MCP 工具和内置写工具一样：不在 --allow 里就拒绝，在就执行。"""
    config = config.model_copy(update={"mcp_servers": {"fake": fake_mcp()}})
    call = {"id": "c1", "name": "mcp__fake__image", "arguments": {}}
    out, err = io.StringIO(), io.StringIO()
    frontend = Headless(
        config,
        cwd=tmp_path,
        allowed_tools=allowed,
        llm_factory=lambda name, profile: FakeLLM([{"tool_calls": [call]}, "好"], name=name),
        out=out,
        err=err,
    )
    assert await frontend.run("看图") == 0
    tool_message = next(m for m in frontend.session.messages if m["role"] == "tool")
    assert expected in tool_message["content"]
    # 状态走 stderr，正文里没有
    assert "MCP：fake ✓ 9 个工具" in err.getvalue()
    assert "MCP：" not in out.getvalue()
    assert frontend.mcp.servers[0].state == "closed"


async def test_headless_failed_mcp_server_only_warns(config: Config, tmp_path: Path):
    from simpleagent.config import McpServerConfig

    ghost = McpServerConfig(command="definitely-not-a-command-sa-test")
    config = config.model_copy(update={"mcp_servers": {"ghost": ghost}})
    err = io.StringIO()
    frontend = Headless(
        config,
        cwd=tmp_path,
        llm_factory=lambda name, profile: FakeLLM(["没有 MCP 也能做完"], name=name),
        out=io.StringIO(),
        err=err,
    )
    assert await frontend.run("做事") == 0
    assert "MCP：ghost ✗ 找不到命令" in err.getvalue()
    assert "  连接 MCP server ghost 失败：找不到命令" in err.getvalue()
