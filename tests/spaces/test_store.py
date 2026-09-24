"""W1 持久化层测试：空间与会话的落盘、最近 5 规则、标题截断、重载。"""

import threading
from pathlib import Path

import pytest

from simpleagent.spaces.models import SpaceSpec
from simpleagent.spaces.store import SpaceStore, new_id


def store(home: Path) -> SpaceStore:
    return SpaceStore(home=home)


def test_new_id_unique():
    a, b = new_id("sp"), new_id("sp")
    assert a != b and a.startswith("sp_")


def test_create_generic_space_makes_tmp(tmp_path: Path):
    st = store(tmp_path)
    sp = st.create_space(SpaceSpec(name="临时整理", kind="generic"))
    assert sp.kind == "generic"
    assert (tmp_path / "spaces" / sp.id / "space.toml").exists()
    assert (tmp_path / "spaces" / sp.id / "tmp").is_dir()
    assert (tmp_path / "spaces" / sp.id / "sessions").is_dir()


def test_create_agent_space_sets_cwd_and_no_tmp(tmp_path: Path):
    st = store(tmp_path)
    sp = st.create_space(
        SpaceSpec(
            name="SA",
            kind="agent",
            executor="claude-code",
            cwd="/Users/x/dev",
            verify_command="pytest -q",
            verify_trigger="on_stop",
        )
    )
    assert sp.cwd == "/Users/x/dev"
    assert sp.agent is not None and sp.agent.command == "claude"
    assert sp.verify is not None and sp.verify.command == "pytest -q"
    # 已经有固定 cwd 的空间不建 tmp 目录
    assert not (tmp_path / "spaces" / sp.id / "tmp").exists()


def test_list_spaces_opened_only(tmp_path: Path):
    st = store(tmp_path)
    a = st.create_space(SpaceSpec(name="a", kind="generic"))
    b = st.create_space(SpaceSpec(name="b", kind="generic"))
    st.close_space(b.id)
    assert {s.id for s in st.list_spaces(opened_only=True)} == {a.id}
    assert len(st.list_spaces(opened_only=False)) == 2


def test_recent_five_with_pin_and_running(tmp_path: Path):
    st = store(tmp_path)
    sp = st.create_space(SpaceSpec(name="sp", kind="generic"))
    metas = [st.create_session(sp.id) for _ in range(8)]
    # 第 3 个置顶、第 5 个运行中
    st.update_meta(sp.id, metas[2].id, pinned=True)
    st.update_meta(sp.id, metas[4].id, status="running")

    visible = st.list_sessions(sp.id, limit=5)
    ids = [m.id for m in visible]
    # pin 和 running 永远在，且不占 5 个名额
    assert metas[2].id in ids
    assert metas[4].id in ids
    assert len(ids) <= 1 + 1 + 5
    # pin 排在最前
    assert ids[0] == metas[2].id
    # 隐藏的仍能通过大 limit 取回
    assert len(st.list_sessions(sp.id, limit=100)) == 8


def test_title_from_first_user_message(tmp_path: Path):
    st = store(tmp_path)
    sp = st.create_space(SpaceSpec(name="sp", kind="generic"))
    m = st.create_session(sp.id)
    long_text = "这是一段很长的用户消息，用来测试标题截断是否生效，超过四十字的部分应当被切掉"
    st.append_message(sp.id, m.id, {"role": "user", "content": long_text})
    meta = st.get_session_meta(sp.id, m.id)
    assert meta.title == long_text[:40]
    assert len(meta.title) <= 40


def test_title_supports_multiblock_content(tmp_path: Path):
    st = store(tmp_path)
    sp = st.create_space(SpaceSpec(name="sp", kind="generic"))
    m = st.create_session(sp.id)
    st.append_message(
        sp.id,
        m.id,
        {
            "role": "user",
            "content": [{"type": "text", "text": "先读 conftest"}, {"type": "image", "url": "x"}],
        },
    )
    assert st.get_session_meta(sp.id, m.id).title == "先读 conftest"


def test_load_session_rebuilds_messages(tmp_path: Path):
    st = store(tmp_path)
    sp = st.create_space(SpaceSpec(name="sp", kind="generic"))
    m = st.create_session(sp.id)
    st.append_message(sp.id, m.id, {"role": "user", "content": "hi"})
    st.append_message(sp.id, m.id, {"role": "assistant", "content": "hello"})
    sess = st.load_session(sp.id, m.id)
    assert len(sess.messages) == 2
    assert sess.messages[0]["content"] == "hi"
    assert sess.requests == 2


def test_same_cwd_multiple_spaces_allowed(tmp_path: Path):
    st = store(tmp_path)
    s1 = st.create_space(SpaceSpec(name="c1", kind="agent", executor="claude-code", cwd="/p"))
    s2 = st.create_space(SpaceSpec(name="c2", kind="agent", executor="opencode", cwd="/p"))
    assert s1.id != s2.id
    assert {x.id for x in st.list_spaces()} == {s1.id, s2.id}


# ------------------------------------------------- 目录形态 × 执行者：四种组合
COMBOS = [
    # (kind, executor, cwd)
    ("generic", "simpleagent", None),
    ("generic", "claude-code", None),
    ("agent", "simpleagent", "/Users/x/proj"),
    ("agent", "claude-code", "/Users/x/proj"),
]


@pytest.mark.parametrize(("kind", "executor", "cwd"), COMBOS)
def test_dir_shape_and_executor_are_independent(tmp_path: Path, kind, executor, cwd):
    """kind 只管「在哪儿跑」，executor 只管「谁跑」，四种组合都要能建、能读回来。"""
    st = store(tmp_path)
    sp = st.create_space(
        SpaceSpec(name=f"{kind}-{executor}", kind=kind, executor=executor, cwd=cwd)
    )
    assert sp.kind == kind and sp.executor == executor and sp.cwd == cwd

    # 没有固定 cwd 的（generic，或将来允许的空目录）落在 tmp，并且目录真的建出来了
    tmp_dir = tmp_path / "spaces" / sp.id / "tmp"
    if cwd:
        assert not tmp_dir.exists()
    else:
        assert tmp_dir.is_dir()

    # 重开一个 store 实例读回来，字段一个都不能丢
    again = SpaceStore(home=tmp_path).get_space(sp.id)
    assert again is not None
    assert (again.kind, again.executor, again.cwd) == (kind, executor, cwd)
    assert (again.agent is not None) == (executor != "simpleagent")


def test_generic_space_can_use_external_cli(tmp_path: Path):
    """通用任务 + 外部 agent：工作目录还是 tmp，但 agent 段要写下来。"""
    st = store(tmp_path)
    sp = st.create_space(SpaceSpec(name="临时用 claude", kind="generic", executor="claude-code"))
    toml = (tmp_path / "spaces" / sp.id / "space.toml").read_text(encoding="utf-8")
    assert "[generic]" in toml and "[agent]" in toml  # 两张表并存
    assert 'command = "claude"' in toml


def test_session_records_executor_as_agent(tmp_path: Path):
    st = store(tmp_path)
    sp = st.create_space(SpaceSpec(name="t", kind="agent", executor="claude-code", cwd="/p"))
    assert st.create_session(sp.id).agent == "claude-code"


def test_legacy_space_toml_without_executor(tmp_path: Path):
    """W1~W5 写的旧文件：没有 executor，执行者藏在 [agent].name，cwd 也在 [agent] 里。"""
    sp_dir = tmp_path / "spaces" / "sp_old"
    sp_dir.mkdir(parents=True)
    (sp_dir / "space.toml").write_text(
        "\n".join(
            [
                'id = "sp_old"',
                'name = "旧空间"',
                'kind = "agent"',
                'profile = "a"',
                "opened = true",
                "",
                "[agent]",
                'name = "claude-code"',
                'command = "claude"',
                "args = []",
                'cwd = "/Users/x/old"',
            ]
        ),
        encoding="utf-8",
    )
    sp = SpaceStore(home=tmp_path).get_space("sp_old")
    assert sp is not None
    assert sp.executor == "claude-code"  # 从 [agent].name 回填
    assert sp.cwd == "/Users/x/old"  # 从 [agent].cwd 提到顶层
    assert sp.agent is not None and sp.agent.command == "claude"


def test_invalid_combinations_rejected(tmp_path: Path):
    st = store(tmp_path)
    bad = [
        (SpaceSpec(name="x", kind="agent"), "绑定目录却不给 cwd"),
        (SpaceSpec(name="x", kind="generic", cwd="/p"), "通用任务却指定了 cwd"),
        (SpaceSpec(name="x", kind="generic", executor="gpt"), "未知执行者"),
        (SpaceSpec(name="x", kind="generic", cli_model="deepseek"), "内置执行者却填了 cli_model"),
        (SpaceSpec(name="x", kind="weird"), "未知形态"),
    ]
    for spec, why in bad:
        with pytest.raises(ValueError) as err:
            st.create_space(spec)
        assert str(err.value), f"拒绝 {why} 时得给出人能看懂的原因"
    # 校验在落盘之前，非法组合不该留下半个空间目录
    assert not (tmp_path / "spaces").exists() or list((tmp_path / "spaces").iterdir()) == []


def test_change_executor_builtin_to_cli(tmp_path: Path):
    st = store(tmp_path)
    sp = st.create_space(SpaceSpec(name="t", kind="agent", cwd="/p", profile="deepseek"))
    st.change_executor(sp.id, "claude-code")

    got = SpaceStore(home=tmp_path).get_space(sp.id)
    assert got is not None
    assert got.executor == "claude-code"
    assert got.agent is not None and got.agent.command == "claude"
    assert got.permission == "safe"
    assert got.profile == "deepseek"  # 留着，切回内置时接着用


def test_change_executor_cli_to_builtin(tmp_path: Path):
    st = store(tmp_path)
    sp = st.create_space(
        SpaceSpec(name="t", kind="generic", executor="opencode", permission="full")
    )
    st.change_executor(sp.id, "simpleagent", profile="kimi")

    got = st.get_space(sp.id)
    assert got is not None
    assert got.executor == "simpleagent" and got.profile == "kimi"
    assert got.agent is None and got.cli_model is None and got.permission == "safe"
    toml = (tmp_path / "spaces" / sp.id / "space.toml").read_text(encoding="utf-8")
    assert "[agent]" not in toml and "permission" not in toml


def test_change_executor_same_keeps_binding(tmp_path: Path):
    """同一个执行者只改权限：手写的 command / args 不能被默认值冲掉。"""
    st = store(tmp_path)
    sp = st.create_space(
        SpaceSpec(
            name="t",
            kind="generic",
            executor="claude-code",
            command="/opt/claude",
            args=["--verbose"],
        )
    )
    got = st.change_executor(sp.id, "claude-code", permission="full")
    assert got.permission == "full"
    assert got.agent is not None
    assert got.agent.command == "/opt/claude" and got.agent.args == ["--verbose"]


def test_change_executor_rejects_invalid(tmp_path: Path):
    st = store(tmp_path)
    sp = st.create_space(SpaceSpec(name="t", kind="generic"))
    toml = tmp_path / "spaces" / sp.id / "space.toml"
    before = toml.read_text(encoding="utf-8")
    bad = [
        {"executor": "gpt"},
        {"executor": "simpleagent", "cli_model": "deepseek"},
        {"executor": "simpleagent", "permission": "full"},
        {"executor": "claude-code", "permission": "root"},
    ]
    for kwargs in bad:
        with pytest.raises(ValueError):
            st.change_executor(sp.id, **kwargs)
    assert toml.read_text(encoding="utf-8") == before  # 校验在落盘之前
    with pytest.raises(KeyError):
        st.change_executor("sp_missing", "simpleagent")


def test_update_space_rejects_executor(tmp_path: Path):
    st = store(tmp_path)
    sp = st.create_space(SpaceSpec(name="t", kind="generic"))
    with pytest.raises(ValueError):
        st.update_space(sp.id, executor="claude-code")


def test_change_executor_keeps_old_session_agent(tmp_path: Path):
    """切换只影响新会话：老会话的执行者锁在 meta.agent 里。"""
    st = store(tmp_path)
    sp = st.create_space(SpaceSpec(name="t", kind="generic"))
    old = st.create_session(sp.id)
    st.change_executor(sp.id, "opencode")
    new = st.create_session(sp.id)
    old_meta = st.get_session_meta(sp.id, old.id)
    assert old_meta is not None and old_meta.agent == "simpleagent"
    assert new.agent == "opencode"


def test_delete_space(tmp_path: Path):
    st = store(tmp_path)
    sp = st.create_space(SpaceSpec(name="sp", kind="generic"))
    assert (tmp_path / "spaces" / sp.id).exists()
    st.delete_space(sp.id)
    assert not (tmp_path / "spaces" / sp.id).exists()


def test_persistence_across_reload(tmp_path: Path):
    st = store(tmp_path)
    sp = st.create_space(SpaceSpec(name="持久化", kind="generic"))
    m = st.create_session(sp.id)
    st.append_message(sp.id, m.id, {"role": "user", "content": "abc"})

    # 新建一个 store 实例，模拟重启
    st2 = SpaceStore(home=tmp_path)
    sp2 = st2.get_space(sp.id)
    assert sp2 is not None and sp2.name == "持久化"
    assert len(st2.list_sessions(sp.id)) == 1
    assert st2.load_session(sp.id, m.id).messages[0]["content"] == "abc"


def test_update_meta_rejects_unknown_field(tmp_path: Path):
    st = store(tmp_path)
    sp = st.create_space(SpaceSpec(name="sp", kind="generic"))
    m = st.create_session(sp.id)
    try:
        st.update_meta(sp.id, m.id, foo="bar")
        pytest.fail("应当拒绝未知字段")
    except ValueError:
        pass


def test_meta_readers_never_see_partial_write(tmp_path: Path):
    # serve 里 runner 线程写 meta、HTTP 线程同时读；写入必须是原子的，读者不能读到空文件
    st = store(tmp_path)
    sp = st.create_space(SpaceSpec(name="sp", kind="generic"))
    m = st.create_session(sp.id)
    stop = threading.Event()
    errors: list[Exception] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                st.get_session_meta(sp.id, m.id)
                st.list_sessions(sp.id)
            except Exception as e:
                errors.append(e)
                return

    t = threading.Thread(target=reader)
    t.start()
    try:
        for i in range(500):
            st.update_meta(sp.id, m.id, title=f"t{i}")
            if errors:
                break
    finally:
        stop.set()
        t.join()
    assert errors == []
    assert list((tmp_path / "spaces" / sp.id / "sessions").glob("*.tmp")) == []


def test_description_roundtrip_and_update(tmp_path: Path):
    """简介进 space.toml：引号、换行都要能原样读回（换行拍平成一行），老文件没有这一行也能读。"""
    st = store(tmp_path)
    sp = st.create_space(SpaceSpec(name="理财", kind="generic"))
    assert sp.description == ""
    assert "description" not in (tmp_path / "spaces" / sp.id / "space.toml").read_text()

    st.update_space(sp.id, description='  管 "记账" 数据\n只读分析\x07  ')
    # 控制字符（\x07）换成空格：TOML 字符串不许有它，写进去整个空间就读不回来了
    again = SpaceStore(home=tmp_path).get_space(sp.id)
    assert again.description == '管 "记账" 数据 只读分析'


def test_description_too_long_rejected(tmp_path: Path):
    st = store(tmp_path)
    sp = st.create_space(SpaceSpec(name="x", kind="generic"))
    with pytest.raises(ValueError, match="最多 200 字"):
        st.update_space(sp.id, description="字" * 201)
    assert st.get_space(sp.id).description == ""
