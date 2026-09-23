import asyncio
import io
from collections.abc import Iterable

import httpx2
import openai
import pytest

from simpleagent.agent.session import SessionStore
from simpleagent.config import Config, ConfigError, Profile
from simpleagent.llm.fake import FakeLLM, Script
from simpleagent.ui.repl import Repl


class Harness:
    """用 FakeLLM 驱动 REPL：每个 profile 一份脚本。"""

    def __init__(
        self,
        config: Config,
        scripts: dict[str, list[Script]],
        inputs: Iterable[str] = (),
        broken: set[str] = frozenset(),
    ):
        self.out = io.StringIO()
        self.fakes: dict[str, FakeLLM] = {}
        remaining = iter(inputs)

        def factory(name: str, profile: Profile) -> FakeLLM:
            if name in broken:
                raise ConfigError(f"{name} 缺少 API key")
            self.fakes[name] = FakeLLM(scripts.get(name, []), name=name, profile=profile)
            return self.fakes[name]

        def input_fn(prompt: str) -> str:
            try:
                return next(remaining)
            except StopIteration:
                raise EOFError from None

        self.repl = Repl(config, llm_factory=factory, out=self.out, input_fn=input_fn)

    @property
    def output(self) -> str:
        return self.out.getvalue()

    def reset(self) -> None:
        """清空已有输出，只看之后打出来的。"""
        self.out.seek(0)
        self.out.truncate()


async def test_chat_turn_records_history_and_prints_stats(config: Config):
    h = Harness(
        config,
        {
            "a": [
                {
                    "content": "你好！",
                    "reasoning": "用户在打招呼",
                    "usage": {"prompt_tokens": 20, "completion_tokens": 8},
                },
                "第二轮",
            ]
        },
    )
    await h.repl.handle("你好")
    await h.repl.handle("再来")

    assert h.repl.session.messages == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好！", "reasoning_content": "用户在打招呼"},
        {"role": "user", "content": "再来"},
        {"role": "assistant", "content": "第二轮"},
    ]
    second_request = h.fakes["a"].requests[1]["messages"]
    assert second_request[0]["role"] == "system"
    assert "工作目录" in second_request[0]["content"]
    assert second_request[1:] == h.repl.session.messages[:3]

    assert "思考：用户在打招呼" in h.output
    assert "你好！" in h.output
    assert "[model-a · 输入 20 · 输出 8" in h.output
    assert h.repl.session.requests == 2
    assert h.repl.session.usage.prompt_tokens == 20


async def test_model_switch_keeps_history(config: Config):
    h = Harness(config, {"a": ["来自 a"], "b": ["来自 b"]})
    await h.repl.handle("q1")
    await h.repl.handle("/model")
    assert "* a" in h.output and "  b" in h.output

    await h.repl.handle("/model b")
    assert h.fakes["a"].closed
    assert h.repl.agent.llm.name == "b"
    await h.repl.handle("q2")
    assert h.fakes["b"].requests[0]["messages"][1:] == [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "来自 a"},
        {"role": "user", "content": "q2"},
    ]


async def test_model_switch_failures_keep_current_model(config: Config):
    h = Harness(config, {}, broken={"b"})
    await h.repl.handle("/model nope")
    await h.repl.handle("/model b")
    assert h.repl.agent.llm.name == "a"
    assert "没有名为 'nope' 的 profile" in h.output
    assert "b 缺少 API key" in h.output


async def test_api_error_rolls_back_user_message(config: Config):
    error = openai.APIConnectionError(request=httpx2.Request("POST", "http://a.invalid/v1"))
    h = Harness(config, {"a": [error]})
    await h.repl.handle("hi")
    assert h.repl.session.messages == []
    assert "请求失败" in h.output
    assert "检查 API key" not in h.output


async def test_auth_error_points_to_key_location(config: Config, sa_home):
    response = httpx2.Response(401, request=httpx2.Request("POST", "http://b.invalid/v1"))
    error = openai.AuthenticationError("invalid key", response=response, body=None)
    config.profiles["a"].api_key_env = "SA_TEST_KEY"
    h = Harness(config, {"a": [error]})
    await h.repl.handle("hi")
    assert "HTTP 401" in h.output
    assert f"检查 API key：SA_TEST_KEY，环境变量优先，其次 {sa_home / '.env'}" in h.output


async def test_cancel_keeps_partial_reply(config: Config):
    slow = {"content": "这是一段很长很长很长的回复", "delay": 0.01}
    h = Harness(config, {"a": [slow, {"content": "不会输出", "delay": 1}]})

    async def cancel_when(predicate):
        task = asyncio.create_task(h.repl.handle("hi"))
        while not predicate():
            await asyncio.sleep(0.005)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # 已有输出：保留部分回复
    await cancel_when(lambda: "这是" in h.output)
    assert h.repl.session.messages[0] == {"role": "user", "content": "hi"}
    assert h.repl.session.messages[1]["role"] == "assistant"
    assert h.repl.session.messages[1]["content"].startswith("这是")

    # 还没输出：撤回这条用户消息
    await cancel_when(lambda: len(h.fakes["a"].requests) == 2)
    assert len(h.repl.session.messages) == 2


async def test_tools_command_lists_builtin_tools(config: Config):
    h = Harness(config, {"a": ["ok"]})
    assert await h.repl.handle("/tools")
    for name in ("list_dir", "read_file", "write_file", "edit_file", "glob", "grep", "bash"):
        assert name in h.output
    assert "读取文本文件内容" in h.output  # 描述也列出来


async def test_commands(config: Config):
    h = Harness(config, {"a": ["ok"]})
    await h.repl.handle("hi")
    assert await h.repl.handle("/usage")
    assert "请求 1 次" in h.output
    assert await h.repl.handle("/clear")
    assert h.repl.session.messages == []
    assert await h.repl.handle("/whatever")
    assert "未知命令 /whatever" in h.output
    assert await h.repl.handle("/exit") is False


async def test_clear_is_persisted(config: Config, sa_home):
    """/clear 要写进 JSONL：直接清列表的话，--resume 之后清掉的历史又回来了。"""
    store = SessionStore(sa_home / "sessions")
    repl = Repl(
        config,
        llm_factory=lambda n, p: FakeLLM(["ok"], name=n, profile=p),
        out=io.StringIO(),
        store=store,
    )
    await repl.handle("hi")
    assert len(store.load(repl.session.id).messages) == 2
    await repl.handle("/clear")
    assert store.load(repl.session.id).messages == []


async def test_context_command(config: Config, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # 不让仓库自己的 AGENTS.md、技能目录影响结果
    h = Harness(
        config,
        {
            "a": [{"content": "你好", "usage": {"prompt_tokens": 40, "completion_tokens": 3}}],
            "b": [],
        },
    )
    await h.repl.handle("/context")
    assert "还没有实际用量" in h.output  # 还没请求过：全部按字符估
    assert "工具 10 个" in h.output  # 7 个内置 + 3 个记忆工具（M7）

    await h.repl.handle("hi")
    h.reset()
    await h.repl.handle("/context")
    assert "/ 112,000 token" in h.output  # 128k 窗口留 16k 给输出
    assert "40 来自上次请求的实际用量" in h.output
    assert "校准：上次请求实际 40" in h.output

    await h.repl.handle("/model b")  # 换了 tokenizer，上次的实际用量不能再用
    h.reset()
    await h.repl.handle("/context")
    assert "还没有实际用量" in h.output


def read_calls(n: int) -> list:
    """n 次请求各读一次 big.txt（约 4000 字符），最后回答完成。"""
    calls = [
        {"tool_calls": [{"id": f"r{i}", "name": "read_file", "arguments": {"path": "big.txt"}}]}
        for i in range(n)
    ]
    return [*calls, "完成"]


async def test_context_edited_is_shown(config: Config, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "big.txt").write_text(("x" * 99 + "\n") * 40)
    config.profiles["a"].context_window = 6000  # 输入上限 5000，读 4 次就过 60%
    config.profiles["a"].max_tokens = 1000
    config.context.compact_at = 0  # 只看清理：压缩另有测试
    h = Harness(config, {"a": read_calls(4)})
    await h.repl.handle("读四遍")
    assert "[清理了 1 个旧工具结果，上下文" in h.output


async def test_compaction_is_shown(config: Config):
    config.profiles["a"].context_window = 6000  # 输入上限 5000
    config.profiles["a"].max_tokens = 1000
    h = Harness(config, {"a": ["答" * 4500, "摘要：用户问过一个问题", "第二个回答"]})
    await h.repl.handle("第一个问题")
    await h.repl.handle("第二个问题")  # 一开口就超过 80%：先压缩，再回答
    assert "[压缩了前 2 条消息：上下文" in h.output
    assert "第二个回答" in h.output
    assert h.repl.session.messages[0]["content"].startswith("[对话摘要]")


async def test_compact_command(config: Config):
    h = Harness(config, {"a": ["答1", "答2", "摘要：问过两个问题"]})
    await h.repl.handle("/compact")
    assert "没有可以压缩的内容" in h.output

    await h.repl.handle("q1")
    await h.repl.handle("q2")
    h.reset()
    await h.repl.handle("/compact 保留文件名")
    assert "[压缩了前 2 条消息：上下文" in h.output
    assert "保留文件名" in h.fakes["a"].requests[-1]["messages"][-1]["content"]  # 重点进了指令
    messages = h.repl.session.messages
    assert messages[0]["content"].startswith("[对话摘要]")
    assert messages[2:] == [
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "答2"},
    ]  # 只留最后一轮


async def test_compact_command_keeps_whole_last_turn(config: Config, tmp_path, monkeypatch):
    """最后一轮里调过工具：问题、工具调用、回答都要留下，不能只剩最后那条回答。"""
    monkeypatch.chdir(tmp_path)
    call = {"id": "c1", "name": "list_dir", "arguments": {}}
    h = Harness(config, {"a": ["答1", {"tool_calls": [call]}, "答2", "摘要"]})
    await h.repl.handle("q1")
    await h.repl.handle("q2")
    await h.repl.handle("/compact")
    roles = [m["role"] for m in h.repl.session.messages]
    assert roles == ["user", "assistant", "user", "assistant", "tool", "assistant"]
    assert h.repl.session.messages[2] == {"role": "user", "content": "q2"}


async def test_context_after_compact_uses_calibration(config: Config):
    usage = {"prompt_tokens": 30, "completion_tokens": 3}
    h = Harness(
        config,
        {"a": [{"content": "答1", "usage": usage}, {"content": "答2", "usage": usage}, "摘要"]},
    )
    await h.repl.handle("q1")
    await h.repl.handle("q2")
    await h.repl.handle("/compact")
    h.reset()
    await h.repl.handle("/context")
    assert "上次的实际用量已作废（清理 / 压缩过），按字符估再乘校准系数" in h.output


async def test_compact_command_failure(config: Config):
    h = Harness(config, {"a": ["答1", "答2", RuntimeError("连不上")]})
    await h.repl.handle("q1")
    await h.repl.handle("q2")
    await h.repl.handle("/compact")
    assert "压缩失败：RuntimeError: 连不上" in h.output
    assert len(h.repl.session.messages) == 4  # 历史没动


def test_run_loop_with_multiline_input(config: Config):
    h = Harness(config, {"a": ["收到"]}, inputs=["", "/help", '"""', "第一行", "第二行", '"""'])
    assert h.repl.run() == 0  # 输入耗尽 → EOFError → 正常退出
    assert h.fakes["a"].requests[0]["messages"][-1] == {
        "role": "user",
        "content": "第一行\n第二行",
    }
    assert "/model [name]" in h.output
    assert h.fakes["a"].closed


async def test_tool_call_is_rendered(config: Config, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # REPL 创建时取当前目录作为工具的工作目录
    for i in range(8):
        (tmp_path / f"f{i}.txt").write_text("hello")
    h = Harness(
        config,
        {"a": [{"tool_calls": [{"name": "list_dir", "arguments": {"depth": 1}}]}, "一共 8 个文件"]},
    )
    await h.repl.handle("当前目录有什么？")

    lines = h.output.splitlines()
    start = lines.index('→ list_dir {"depth": 1}')
    assert lines[start + 1 : start + 7] == [
        f"  {tmp_path.resolve()}",
        "  f0.txt 5B",
        "  f1.txt 5B",
        "  f2.txt 5B",
        "  f3.txt 5B",
        "  …（共 9 行）",
    ]
    assert "一共 8 个文件" in h.output
    assert h.output.count("[model-a") == 2
    assert [m["role"] for m in h.repl.session.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]


async def test_parallel_results_are_labeled_and_errors_shown(config: Config, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = [
        {"id": "c1", "name": "list_dir", "arguments": {"path": "nope"}},
        {"id": "c2", "name": "list_dir", "arguments": {}},
    ]
    h = Harness(config, {"a": [{"tool_calls": calls}, "好的"]})
    await h.repl.handle("看看")

    lines = h.output.splitlines()
    start = lines.index('→ list_dir {"path": "nope"}')
    assert lines[start + 1 : start + 7] == [
        "→ list_dir {}",
        "← list_dir",
        f"  错误：路径不存在：{tmp_path.resolve() / 'nope'}",
        "← list_dir",
        f"  {tmp_path.resolve()}",
        "  (空目录)",
    ]


async def test_max_steps_message(config: Config, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.max_steps = 1
    h = Harness(config, {"a": [{"tool_calls": [{"name": "list_dir", "arguments": {}}]}]})
    await h.repl.handle("看看")
    assert "[达到 max_steps=1，本轮停止" in h.output


async def test_repl_hides_api_key_env_from_tools(config: Config):
    config.profiles["a"].api_key_env = "SA_TEST_KEY_A"
    config.profiles["b"].api_key_env = "SA_TEST_KEY_B"
    h = Harness(config, {})
    assert h.repl.agent.hidden_env == frozenset({"SA_TEST_KEY_A", "SA_TEST_KEY_B"})


def test_startup_failure_leaves_no_empty_session(config: Config, sa_home):
    # 缺 API key 时创建模型客户端就会抛错：这时不能已经往 sessions/ 里写了一条空会话
    def broken(name: str, profile: Profile) -> FakeLLM:
        raise ConfigError("找不到 SA_TEST_KEY")

    store = SessionStore(sa_home / "sessions")
    with pytest.raises(ConfigError):
        Repl(config, llm_factory=broken, out=io.StringIO(), store=store)
    assert store.list() == []

    Repl(config, llm_factory=lambda n, p: FakeLLM([], name=n, profile=p), store=store)
    assert len(store.list()) == 1


# ------------------------------------------------------------------ MCP（M5）


def test_repl_starts_mcp_and_calls_its_tools(config: Config, fake_mcp, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = config.model_copy(update={"mcp_servers": {"fake": fake_mcp()}})
    call = {"id": "c1", "name": "mcp__fake__echo", "arguments": {"text": "你好"}}
    h = Harness(
        config,
        {"a": [{"tool_calls": [call]}, "它说了你好"]},
        inputs=["/mcp", "让它说你好", "/exit"],
    )
    assert h.repl.run() == 0
    out = h.output
    assert "启动 MCP server：fake（按 Ctrl+C 跳过）" in out
    assert "MCP：fake ✓ 9 个工具" in out
    assert "fake   ✓ 旧协议 2025-11-25 · fake-mcp 1.2.3 · 9 个工具" in out  # /mcp
    assert "→ mcp__fake__echo" in out and "它说了你好" in out
    request = h.fakes["a"].requests[0]
    assert "mcp__fake__echo" in [t["function"]["name"] for t in request["tools"]]
    assert "## fake\n测试用的 server，工具都是假的。" in request["messages"][0]["content"]
    assert h.repl.mcp.servers[0].state == "closed"  # 退出时关掉了子进程


def test_repl_reports_failed_mcp_server(config: Config, tmp_path, monkeypatch):
    from simpleagent.config import McpServerConfig

    monkeypatch.chdir(tmp_path)
    ghost = McpServerConfig(command="definitely-not-a-command-sa-test")
    config = config.model_copy(update={"mcp_servers": {"ghost": ghost}})
    h = Harness(config, {}, inputs=["/mcp", "/exit"])
    assert h.repl.run() == 0
    assert "MCP：ghost ✗ 找不到命令 definitely-not-a-command-sa-test" in h.output
    assert "（/mcp 看详情）" in h.output
    assert "ghost   ✗ 连接 MCP server ghost 失败：找不到命令" in h.output


async def test_mcp_command_without_servers(config: Config):
    h = Harness(config, {})
    await h.repl.handle("/mcp")
    assert "没有配置 MCP server" in h.output
