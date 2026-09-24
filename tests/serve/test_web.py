"""W3 测试：静态资源托管、/api/meta、sessions 的 limit 参数。

不联网：用 port=0 起真实的 http.server，用 urllib 打请求；Runner 起的是后台线程，
不主动发请求就不会碰网络（llm_factory 缺省，但本文件不触发 run_input）。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

from simpleagent.serve.app import make_server
from simpleagent.serve.static import asset_bytes
from simpleagent.spaces.models import SpaceSpec
from simpleagent.spaces.store import SpaceStore


def _serve(config):
    httpd = make_server(config, host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}"


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


def _get_json(url: str):
    status, _, body = _get(url)
    assert status == 200
    return json.loads(body)


def _patch(url: str, data: dict):
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
        method="PATCH",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


# --------------------------------------------------------------- 4. 会话重命名 / 置顶
def test_update_session_title_and_pin(config, sa_home):
    store = SpaceStore(sa_home)
    space = store.create_space(SpaceSpec(name="t", kind="generic", profile="a"))
    meta = store.create_session(space.id)
    base = _serve(config)
    url = f"{base}/api/sessions/{meta.id}"

    updated = _patch(url, {"title": "修 pytest 临时目录"})
    assert updated["title"] == "修 pytest 临时目录"

    pinned = _patch(url, {"pinned": True})
    assert pinned["pinned"] is True


def test_update_session_rejects_bad_input(config, sa_home):
    store = SpaceStore(sa_home)
    space = store.create_space(SpaceSpec(name="t", kind="generic", profile="a"))
    meta = store.create_session(space.id)
    base = _serve(config)
    url = f"{base}/api/sessions/{meta.id}"

    for bad, why in [
        ({"title": "  "}, "空标题"),
        ({"status": "done"}, "不允许改 status"),
        ({}, "空 body"),
    ]:
        try:
            _patch(url, bad)
        except urllib.error.HTTPError as e:
            assert e.code == 400, why
        else:
            raise AssertionError(f"应当 400：{why}")

    try:
        _patch(f"{base}/api/sessions/se_not_exist", {"title": "x"})
    except urllib.error.HTTPError as e:
        assert e.code == 404
    else:
        raise AssertionError("不存在的会话应当 404")


# ------------------------------------------------- 5. 启动失败不能静默
def test_startup_failure_sends_error_frame(config, sa_home):
    """构造 Agent 失败（没配 key / profile 不存在 / cwd 不存在）时必须发 error 帧。

    这段曾经是静默的：`_build_agent` 在 try 之外，异常被 asyncio future 吞掉，
    客户端一条帧都收不到，界面永远停在「运行中」。
    """
    from simpleagent.serve.app import Server
    from simpleagent.serve.runner import Runner

    store = SpaceStore(sa_home)
    space = store.create_space(SpaceSpec(name="t", kind="generic", profile="a"))
    session = store.create_session(space.id)

    def boom(name, profile):
        raise RuntimeError("没有 API key")

    server = Server(config, store=store, runner=Runner(config, store=store, llm_factory=boom))
    server.start()
    q, _ = server.runner.bus.subscribe(session.id)

    server.handle(
        "POST", f"/api/sessions/{session.id}/input", {}, json.dumps({"text": "hi"}).encode()
    )

    first = q.get(timeout=5)  # 拿不到帧就会超时，正是这个 bug 的表现
    second = q.get(timeout=5)
    assert first.type == "error"
    assert "没有 API key" in first.payload["message"]
    assert second.type == "status"
    assert second.payload["status"] == "error"

    meta = store.get_session_meta(space.id, session.id)
    assert meta.status == "error"
    server.runner.shutdown()


# ------------------------------------------------- 6. W4：验证通过后改文件 → stale
def test_verify_then_write_marks_stale(config, sa_home):
    """验证通过后工作目录又被改动，验证结果要降级为 stale，并且发帧通知客户端。"""
    from simpleagent.events import ToolResult
    from simpleagent.serve.runner import Runner

    store = SpaceStore(sa_home)
    space = store.create_space(
        SpaceSpec(name="t", kind="generic", profile="a", verify_command="true")
    )
    session = store.create_session(space.id)
    runner = Runner(config, store=store, llm_factory=_fake_factory([{"content": "ok"}]))
    runner.start()
    q, _ = runner.bus.subscribe(session.id)

    # 1) 跑一次验证 → passed，并冻结目录指纹
    runner.verify(space.id, session.id)
    while True:
        f = q.get(timeout=5)
        if f.type == "verification":
            break
    assert f.payload["status"] == "passed"
    assert f.payload["fingerprint"]
    runner.bus.unsubscribe(session.id, q)

    # 2) 模拟一个写工具改了文件
    cwd = runner.cwd_for(space)
    (cwd / "new.txt").write_text("hello", encoding="utf-8")

    q, _ = runner.bus.subscribe(session.id)
    runner._agents[session.id] = _WriteOnlyAgent()  # 假装这个会话有个 agent 在跑
    runner._maybe_mark_stale(space.id, session.id, ToolResult("c1", "write_file", "ok"))

    frame = q.get(timeout=5)
    assert frame.type == "verification"
    assert frame.payload["status"] == "stale"
    assert store.get_session_meta(space.id, session.id).verification.status == "stale"
    runner.shutdown()


def test_readonly_tool_does_not_mark_stale(config, sa_home):
    """只读工具即便在「写工具」名单外也不该影响验证；这里验证 is_readonly 那条短路。"""
    from simpleagent.events import ToolResult
    from simpleagent.serve.runner import Runner

    store = SpaceStore(sa_home)
    space = store.create_space(
        SpaceSpec(name="t", kind="generic", profile="a", verify_command="true")
    )
    session = store.create_session(space.id)
    runner = Runner(config, store=store, llm_factory=_fake_factory([{"content": "ok"}]))
    runner.start()
    q, _ = runner.bus.subscribe(session.id)
    runner.verify(space.id, session.id)  # 异步执行，要等帧落地
    while True:
        f = q.get(timeout=5)
        if f.type == "verification":
            break
    assert f.payload["status"] == "passed"
    runner._agents[session.id] = _ReadOnlyAgent()
    (runner.cwd_for(space) / "new.txt").write_text("x", encoding="utf-8")

    runner._maybe_mark_stale(space.id, session.id, ToolResult("c1", "read_file", "ok"))
    assert store.get_session_meta(space.id, session.id).verification.status == "passed"
    runner.shutdown()


def test_rerun_reuses_last_user_message(config, sa_home):
    store = SpaceStore(sa_home)
    space = store.create_space(SpaceSpec(name="t", kind="generic", profile="a"))
    session = store.create_session(space.id)
    base = _serve(config)

    # 还没有用户消息 → 400
    try:
        _post(f"{base}/api/sessions/{session.id}/rerun", {})
    except urllib.error.HTTPError as e:
        assert e.code == 400
    else:
        raise AssertionError("没有用户消息时重跑应当 400")

    store.append_message(space.id, session.id, {"role": "user", "content": "第一句"})
    store.append_message(space.id, session.id, {"role": "assistant", "content": "回复"})
    store.append_message(space.id, session.id, {"role": "user", "content": "第二句"})

    got = _post(f"{base}/api/sessions/{session.id}/rerun", {})
    assert got["accepted"] is True
    assert got["text"] == "第二句"  # 取最后一条用户消息


def test_space_files_tree(config, sa_home):
    store = SpaceStore(sa_home)
    workdir = sa_home / "work"
    (workdir / "src").mkdir(parents=True)
    (workdir / "src" / "a.py").write_text("x = 1", encoding="utf-8")
    (workdir / "README.md").write_text("hi", encoding="utf-8")
    (workdir / ".hidden").write_text("no", encoding="utf-8")

    space = store.create_space(SpaceSpec(name="t", kind="agent", profile="a", cwd=str(workdir)))
    base = _serve(config)
    data = _get_json(f"{base}/api/spaces/{space.id}/files")
    assert data["cwd"] == str(workdir)
    names = [n["name"] for n in data["tree"][0]["children"]]
    assert "README.md" in names
    assert "src" in names
    assert ".hidden" not in names  # 隐藏文件不出现


# ------------------------------------------------- 7. 待审批要带详情
def test_pending_approvals_carry_details(config, sa_home):
    """待审批必须带 session_id / tool_name，否则面板下发的任务会卡死没人管。

    审批帧发出去就过去了，SSE 首次连接又不重放，晚连上来的客户端只能靠
    `GET /api/approvals` 把那张卡补出来。
    """
    import time

    from simpleagent.serve.app import Server
    from simpleagent.serve.runner import Runner

    store = SpaceStore(sa_home)
    space = store.create_space(SpaceSpec(name="t", kind="generic", profile="a"))
    session = store.create_session(space.id)
    script = [
        {
            "tool_calls": [
                {"id": "c1", "name": "write_file", "arguments": {"path": "x.txt", "content": "hi"}}
            ]
        },
        {"content": "ok"},
    ]
    server = Server(
        config, store=store, runner=Runner(config, store=store, llm_factory=_fake_factory(script))
    )
    server.start()
    q, _ = server.runner.bus.subscribe(session.id)
    server.handle(
        "POST", f"/api/sessions/{session.id}/input", {}, json.dumps({"text": "写"}).encode()
    )

    while True:
        f = q.get(timeout=5)
        if f.type == "approval_request":
            break

    pending = server.handle("GET", "/api/approvals", {}, b"").body["pending"]
    assert len(pending) == 1
    p = pending[0]
    assert p["session_id"] == session.id
    assert p["tool_name"] == "write_file"
    assert "x.txt" in p["arguments"]

    server.handle(
        "POST", f"/api/approvals/{p['approval_id']}", {}, json.dumps({"action": "deny"}).encode()
    )
    time.sleep(0.3)  # 拒绝是在 runner 线程上落地的
    left = server.handle("GET", "/api/approvals", {}, b"").body["pending"]
    assert left == []
    server.runner.shutdown()


def test_pinned_session_stays_on_top(config, sa_home):
    import time

    store = SpaceStore(sa_home)
    space = store.create_space(SpaceSpec(name="t", kind="generic", profile="a"))
    old = store.create_session(space.id)
    for _ in range(5):
        # updated_at 只到毫秒，连着建会撞同一毫秒，old 就不一定排在最后；隔开 2ms 保证严格递增
        time.sleep(0.002)
        store.create_session(space.id)  # 把 old 挤出「最近 5 条」

    base = _serve(config)
    listed = _get_json(f"{base}/api/spaces/{space.id}/sessions")
    assert old.id not in [m["id"] for m in listed]

    _patch(f"{base}/api/sessions/{old.id}", {"pinned": True})
    listed = _get_json(f"{base}/api/spaces/{space.id}/sessions")
    assert listed[0]["id"] == old.id  # 置顶后常驻且不占名额


def _post(url: str, data: dict):
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _fake_factory(script):
    """llm_factory：忽略 profile，返回吃固定脚本的 FakeLLM（与 test_serve.py 同款）。"""

    def factory(name, profile):
        from simpleagent.llm.fake import FakeLLM

        return FakeLLM(list(script))

    return factory


class _WriteOnlyAgent:
    """假 agent：名下工具全是写工具，用来触发 stale 判定。"""

    class tools:  # noqa: N801
        @staticmethod
        def is_readonly(name: str) -> bool:
            return False


class _ReadOnlyAgent:
    class tools:  # noqa: N801
        @staticmethod
        def is_readonly(name: str) -> bool:
            return True


# --------------------------------------------------------------- 1. 页面与静态资源
def test_index_and_assets(config):
    base = _serve(config)
    status, ctype, body = _get(f"{base}/")
    assert status == 200
    assert ctype.startswith("text/html")
    assert "SimpleAgent 工作台" in body.decode("utf-8")

    # markdown.js 要先于 app.js 加载：app.js 直接调它挂在 window 上的 renderMarkdown
    page = body.decode("utf-8")
    assert page.index("/assets/markdown.js") < page.index("/assets/app.js")

    status, ctype, body = _get(f"{base}/index.html")
    assert status == 200 and ctype.startswith("text/html")

    _, ctype, js = _get(f"{base}/assets/app.js")
    assert ctype.startswith("text/javascript")
    assert "EventSource" in js.decode("utf-8")

    _, ctype, js = _get(f"{base}/assets/markdown.js")
    assert ctype.startswith("text/javascript")
    assert "renderMarkdown" in js.decode("utf-8")

    _, ctype, _ = _get(f"{base}/assets/styles.css")
    assert ctype.startswith("text/css")


def test_unknown_asset_is_404(config):
    base = _serve(config)
    try:
        _get(f"{base}/assets/nope.js")
    except urllib.error.HTTPError as e:
        assert e.code == 404
    else:
        raise AssertionError("未知静态资源应当 404")


def test_asset_path_traversal_blocked():
    # 穿越在 asset_bytes 里就被掐掉：只取文件名，且后缀必须在白名单里
    for bad in ["../spaces/models.py", "/etc/passwd", "..", "models.py", ""]:
        assert asset_bytes(bad) is None, bad
    assert asset_bytes("app.js") is not None


# --------------------------------------------------------------- 2. 元信息
def test_meta(config):
    base = _serve(config)
    data = _get_json(f"{base}/api/meta")
    assert data["profiles"] == ["a", "b"]
    assert data["default_profile"] == "a"
    assert "max_steps" in data
    # 执行者名单由后端给，前端不硬编码；本次外部 CLI 只有「本机默认」，模型列表为空
    execs = {e["name"]: e for e in data["executors"]}
    assert list(execs) == ["simpleagent", "claude-code", "opencode"]
    assert execs["simpleagent"]["external"] is False
    assert execs["simpleagent"]["models"] == ["a", "b"]
    assert execs["claude-code"]["external"] is True
    assert execs["claude-code"]["models"] == []
    # 权限两档由后端给，向导里直接渲染
    assert [p["name"] for p in execs["claude-code"]["permissions"]] == ["safe", "full"]
    assert execs["claude-code"]["default_permission"] == "safe"
    assert execs["simpleagent"]["permissions"] == []


# ------------------------------------------------- 2b. 新建空间：四种组合与校验
def _create_space(base: str, body: dict) -> dict:
    return _post(f"{base}/api/spaces", body)


def test_create_space_each_dir_shape_and_executor(config, sa_home):
    """目录形态与执行者正交：四种组合都能建，且 execution 落到 space.toml。"""
    base = _serve(config)
    combos = [
        ({"name": "通用内置", "kind": "generic", "executor": "simpleagent", "profile": "a"}, None),
        ({"name": "通用 claude", "kind": "generic", "executor": "claude-code"}, None),
        (
            {
                "name": "目录内置",
                "kind": "agent",
                "executor": "simpleagent",
                "profile": "b",
                "cwd": str(sa_home / "proj"),
            },
            str(sa_home / "proj"),
        ),
        (
            {
                "name": "目录 claude",
                "kind": "agent",
                "executor": "claude-code",
                "cwd": str(sa_home / "proj"),
            },
            str(sa_home / "proj"),
        ),
    ]
    for body, cwd in combos:
        created = _create_space(base, body)
        assert created["name"] == body["name"]
        assert created["executor"] == body["executor"]
        assert created["cwd"] == cwd
        assert (created["agent"] is not None) == (body["executor"] != "simpleagent")


def test_create_space_rejects_bad_combinations(config, sa_home):
    base = _serve(config)
    bad = [
        ({"name": "x", "kind": "agent", "executor": "claude-code"}, "绑定目录却没填 cwd"),
        ({"name": "x", "kind": "generic", "executor": "gpt"}, "未知执行者"),
        (
            {"name": "x", "kind": "generic", "executor": "simpleagent", "cli_model": "a"},
            "内置执行者却填了 cli_model",
        ),
        ({"name": "x", "kind": "generic", "cwd": "/tmp"}, "通用任务却指定 cwd"),
    ]
    for body, why in bad:
        try:
            _create_space(base, body)
        except urllib.error.HTTPError as e:
            assert e.code == 400, why
        else:
            raise AssertionError(f"应当 400：{why}")
    # 一个都不该落盘
    assert _get_json(f"{base}/api/spaces") == []


def test_external_executor_never_falls_back_to_builtin_loop(config, sa_home):
    """选了外部 agent 就必须走 CLI 路径，**不能**静默用内置 loop 跑。

    外部 CLI 用一个不存在的可执行文件：该报错就报错，但内置 loop 一次都不许被碰
    （llm_factory 被调用就说明回退了）。
    """
    from simpleagent.serve.app import Server
    from simpleagent.serve.runner import Runner

    store = SpaceStore(sa_home)
    space = store.create_space(
        SpaceSpec(
            name="claude空间",
            kind="agent",
            executor="claude-code",
            cwd=str(sa_home),
            command="/nonexistent/sa-cli",
        )
    )
    session = store.create_session(space.id)

    llm_calls: list[str] = []

    def factory(name, profile):
        llm_calls.append(name)
        return _fake_factory([])(name, profile)

    server = Server(config, store=store, runner=Runner(config, store=store, llm_factory=factory))
    server.start()
    q, _ = server.runner.bus.subscribe(session.id)
    server.handle(
        "POST", f"/api/sessions/{session.id}/input", {}, json.dumps({"text": "hi"}).encode()
    )

    started = q.get(timeout=5)
    assert started.type == "status" and started.payload["status"] == "running"
    first = q.get(timeout=5)
    assert first.type == "error"
    assert "找不到可执行文件" in first.payload["message"]
    second = q.get(timeout=5)
    assert second.type == "status" and second.payload["status"] == "error"
    assert llm_calls == []  # 内置 loop 完全没参与
    # 会话被落成 error，而不是悄悄跑起来；用户那句话留着，配好之后还能重跑
    assert store.get_session_meta(space.id, session.id).status == "error"
    assert store.load_session(space.id, session.id).messages == [{"role": "user", "content": "hi"}]
    server.runner.shutdown()


# --------------------------------------------------------------- 3. sessions limit
def test_sessions_limit(config, sa_home):
    store = SpaceStore(sa_home)
    space = store.create_space(SpaceSpec(name="t", kind="generic", profile="a"))
    for _ in range(6):
        store.create_session(space.id)

    base = _serve(config)
    default = _get_json(f"{base}/api/spaces/{space.id}/sessions")
    assert len(default) == 5  # 左栏只显示最近 5 条

    all_sessions = _get_json(f"{base}/api/spaces/{space.id}/sessions?limit=50")
    assert len(all_sessions) == 6  # 「查看全部」拉更多

    clamped = _get_json(f"{base}/api/spaces/{space.id}/sessions?limit=9999")
    assert len(clamped) == 6
