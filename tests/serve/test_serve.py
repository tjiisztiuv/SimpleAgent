"""W2 测试：事件总线、审批回转、Runner（FakeLLM）、以及 HTTP + SSE 集成。

全部不联网：用 FakeLLM 取代真实模型，用 bus.subscribe 直接收帧，或用标准库 urllib 打真实 HTTP。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

from simpleagent.llm.fake import FakeLLM
from simpleagent.permissions import ApprovalDecision, ApprovalRequest
from simpleagent.serve.approval import APIApprover, PendingApprovals
from simpleagent.serve.bus import EventBus, Frame
from simpleagent.serve.runner import Runner
from simpleagent.spaces.models import SpaceSpec
from simpleagent.spaces.store import SpaceStore


def _fake_factory(script: list[dict]) -> Any:
    """llm_factory：忽略 profile，返回吃固定脚本的 FakeLLM。"""

    def factory(name: str, profile: Any) -> FakeLLM:
        return FakeLLM(list(script))

    return factory


# --------------------------------------------------------------- 1. 总线顺序与重放
def test_bus_order_and_replay():
    bus = EventBus()
    q, replay = bus.subscribe("se1")
    assert replay == []
    for i in range(3):
        bus.publish(Frame("se1", "tick", {"i": i}))
    got = [q.get(timeout=2).seq for _ in range(3)]
    assert got == [1, 2, 3]

    # 新的订阅者带 Last-Event-ID=1 应只重放 seq 2,3（重放帧在返回的列表里，不进队列）
    q2, replay2 = bus.subscribe("se1", last_event_id="1")
    assert [f.seq for f in replay2] == [2, 3]
    bus.unsubscribe("se1", q2)
    bus.unsubscribe("se1", q)


def test_sse_resume_via_query(config, sa_home):
    """EventSource 设不了 Last-Event-ID 头，前端主动续传时走 ?last_event_id=N。

    后台标签页会断开 SSE 把连接让出来（浏览器对同一 host 只给 6 条），切回前台再续传；
    这条不通的话，切走期间的帧就丢了。
    """
    from simpleagent.serve.app import Server

    server = Server(config, store=SpaceStore(sa_home))
    for i in range(3):
        server.runner.bus.publish(Frame("se1", "tick", {"i": i}))

    def first_ids(headers: dict[str, str], n: int) -> list[str]:
        # 只取重放出来的 n 帧：再往下取就是阻塞等新帧 / keepalive 了
        resp = server.handle("GET", "/api/sessions/se1/events", headers, b"")
        chunks = [next(resp.stream) for _ in range(n)]
        resp.stream.close()
        return [c.split("\n", 1)[0] for c in chunks]

    assert first_ids({"x-query": "last_event_id=1"}, 2) == ["id: 2", "id: 3"]
    # 浏览器自动重连带的头更新，两者都在时以头为准
    assert first_ids({"x-query": "last_event_id=1", "last-event-id": "2"}, 1) == ["id: 3"]


def test_session_messages_carry_seq(config, sa_home):
    """GET /api/sessions/{id} 带上当前帧号：前端画完历史只订阅它之后的帧。

    不带的话，页面在后台时打开会话、切回前台续传是 last_event_id=0，服务端会把
    整段历史的帧重放一遍，叠在已经画好的历史上。
    """
    from simpleagent.serve.app import Server

    store = SpaceStore(sa_home)
    server = Server(config, store=store)
    space = store.create_space(SpaceSpec(name="t", kind="generic", profile="a"))
    meta = store.create_session(space.id)
    assert server.handle("GET", f"/api/sessions/{meta.id}", {}, b"").body["seq"] == 0
    for i in range(3):
        server.runner.bus.publish(Frame(meta.id, "tick", {"i": i}))
    body = server.handle("GET", f"/api/sessions/{meta.id}", {}, b"").body
    assert body["seq"] == 3
    # 从这个帧号续传：之前的不再重放，之后的照常收到
    _, replay = server.runner.bus.subscribe(meta.id, str(body["seq"]))
    assert replay == []


# --------------------------------------------------------------- 2. 审批挂起 → 恢复
async def test_approval_roundtrip():
    bus = EventBus()
    pending = PendingApprovals()
    approver = APIApprover(bus, pending)

    async def requester() -> ApprovalDecision:
        return await approver.request(
            ApprovalRequest(session_id="se1", tool_name="bash", arguments='{"command":"rm -rf /"}')
        )

    task = asyncio.create_task(requester())  # noqa: F821
    await asyncio.sleep(0.02)
    # 应当已经推了一帧 approval_request，且 Future 还挂着
    assert pending.pending_ids()
    # 解析审批
    pending.resolve(pending.pending_ids()[0], ApprovalDecision(allow=True, always=True))
    decision = await task
    assert decision.allow is True
    assert decision.always is True

    # always 生效：同 session 同工具再次请求直接放行，不再推帧
    decision2 = await approver.request(
        ApprovalRequest(session_id="se1", tool_name="bash", arguments='{"x":1}')
    )
    assert decision2.allow is True


# --------------------------------------------------------------- 3. Runner + FakeLLM 流式
def test_runner_fake_llm_stream(config, sa_home):
    store = SpaceStore(sa_home)
    space = store.create_space(SpaceSpec(name="t", kind="generic", profile="a"))
    session = store.create_session(space.id)
    runner = Runner(config, store=store, llm_factory=_fake_factory([{"content": "你好，世界"}]))
    runner.start()
    q, _ = runner.bus.subscribe(session.id)

    runner.run_input(space.id, session.id, "hi")

    frames = []
    while True:
        f = q.get(timeout=5)
        frames.append(f)
        if f.type == "message_done":
            break

    assert any(f.type == "text_delta" for f in frames)
    assert frames[-1].type == "message_done"
    # 落盘：user + assistant 两条消息
    messages = store.load_session(space.id, session.id).messages
    assert len(messages) == 2
    assert messages[1]["role"] == "assistant"
    assert "你好" in messages[1]["content"]
    runner.shutdown()


# --------------------------------------------------------------- 4. 审批流：挂起 → POST → 继续
def test_runner_approval_flow(config, sa_home):
    store = SpaceStore(sa_home)
    space = store.create_space(SpaceSpec(name="t", kind="generic", profile="a"))
    session = store.create_session(space.id)
    script = [
        {
            "tool_calls": [
                {"id": "c1", "name": "write_file", "arguments": {"path": "x.txt", "content": "hi"}}
            ]
        },
        {"content": "写完了"},
    ]
    runner = Runner(config, store=store, llm_factory=_fake_factory(script))
    runner.start()
    q, _ = runner.bus.subscribe(session.id)

    runner.run_input(space.id, session.id, "写个文件")

    # 读到 approval_request 为止，此时不应有 tool_result
    frames: list[Frame] = []
    while True:
        f = q.get(timeout=5)
        frames.append(f)
        if f.type == "approval_request":
            break
    assert frames[-1].type == "approval_request"
    assert not any(f.type == "tool_result" for f in frames)
    ap_id = frames[-1].payload["approval_id"]

    # 允许后继续，直到下一个 message_done
    runner.approve(ap_id, "allow")
    while True:
        f = q.get(timeout=5)
        frames.append(f)
        if f.type == "message_done":
            break
    assert any(f.type == "tool_result" for f in frames)
    assert any(f.type == "text_delta" for f in frames)
    # 文件确实被写了
    written = (store._space_dir(space.id) / "tmp" / "x.txt").read_text(encoding="utf-8")
    assert written == "hi"
    runner.shutdown()


# --------------------------------------------------------------- 5. 验证命令
def test_runner_verify(config, sa_home):
    store = SpaceStore(sa_home)
    space = store.create_space(
        SpaceSpec(name="t", kind="generic", profile="a", verify_command="true")
    )
    session = store.create_session(space.id)
    runner = Runner(config, store=store, llm_factory=_fake_factory([{"content": "ok"}]))
    runner.start()
    q, _ = runner.bus.subscribe(session.id)

    runner.verify(space.id, session.id)
    frames = []
    while True:
        f = q.get(timeout=5)
        frames.append(f)
        if f.type == "verification":
            break
    assert frames[-1].type == "verification"
    assert frames[-1].payload["status"] == "passed"
    runner.shutdown()


# --------------------------------------------------------------- 6. HTTP + SSE 集成
def test_serve_http_sse(config, sa_home):
    store = SpaceStore(sa_home)
    space = store.create_space(SpaceSpec(name="t", kind="generic", profile="a"))
    session = store.create_session(space.id)

    from simpleagent.serve.app import make_server

    httpd = make_server(
        config, host="127.0.0.1", port=0, llm_factory=_fake_factory([{"content": "来自服务端"}])
    )
    port = httpd.server_address[1]
    srv = threading.Thread(target=httpd.serve_forever, daemon=True)
    srv.start()
    base = f"http://127.0.0.1:{port}"

    collected: list[dict] = []
    stop = threading.Event()

    def reader() -> None:
        try:
            with urllib.request.urlopen(
                f"{base}/api/sessions/{session.id}/events", timeout=10
            ) as resp:
                for raw in resp:
                    line = raw.decode("utf-8").strip()
                    if line.startswith("data:"):
                        payload = json.loads(line[5:].strip())
                        collected.append(payload)
                        if payload.get("type") == "message_done":
                            break
        except Exception:
            pass
        finally:
            stop.set()

    rt = threading.Thread(target=reader, daemon=True)
    rt.start()
    time.sleep(0.1)  # 确保 SSE 已订阅

    req = urllib.request.Request(
        f"{base}/api/sessions/{session.id}/input",
        data=json.dumps({"text": "hi"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=5)
    assert resp.status == 202

    rt.join(timeout=8)
    assert any(c.get("type") == "text_delta" for c in collected)
    assert any(c.get("type") == "message_done" for c in collected)

    # 审批列表接口也通
    with urllib.request.urlopen(f"{base}/api/approvals", timeout=5) as r:
        assert r.status == 200

    httpd.shutdown()
    srv.join(timeout=3)


# ------------------------------------------------- 7. 正常结束必须广播 status: done
def test_runner_emits_status_done(config, sa_home):
    """跑完要发 status: done —— 控制面板靠这一帧知道任务结束了。

    改动前只有 cancelled / error 发帧，正常结束只写 meta，所以"跑完了"从来没出过总线。
    """
    store = SpaceStore(sa_home)
    space = store.create_space(SpaceSpec(name="t", kind="generic", profile="a"))
    session = store.create_session(space.id)
    runner = Runner(config, store=store, llm_factory=_fake_factory([{"content": "干完了"}]))
    runner.start()
    q, _ = runner.bus.subscribe(session.id)

    runner.run_input(space.id, session.id, "hi")

    frames: list[Frame] = []
    while True:
        f = q.get(timeout=5)
        frames.append(f)
        if f.type == "status" and f.payload["status"] == "done":
            break

    # 开场先发 running，面板才知道"开始了"
    assert frames[0].type == "status"
    assert frames[0].payload["status"] == "running"
    assert frames[0].payload["space_id"] == space.id
    # 收尾帧排在最后一条 message_done 之后，且带上 usage
    last_message_done = max(i for i, f in enumerate(frames) if f.type == "message_done")
    assert last_message_done < len(frames) - 1
    assert frames[-1].payload["space_id"] == space.id
    assert "usage" in frames[-1].payload
    # 落盘终态
    assert store.get_session_meta(space.id, session.id).status == "done"
    runner.shutdown()  # 等 loop 停下，_finalize 里发帧之后的那步（落消息）一定已经走完
    # 完成只在指挥台看，不进消息：否则每跑完一个任务角标就 +1，失败反而被淹掉
    assert runner.panel.list_messages() == []


# ------------------------------------------------- 8. 取消仍要广播 status: cancelled
def test_runner_emits_status_cancelled(config, sa_home):
    """取消也走 _finalize 发帧 —— 重构收口点时不能把这条弄丢。"""
    store = SpaceStore(sa_home)
    space = store.create_space(SpaceSpec(name="t", kind="generic", profile="a"))
    session = store.create_session(space.id)
    # delay 让这一轮停在半路，好在它跑完之前取消
    runner = Runner(
        config, store=store, llm_factory=_fake_factory([{"content": "慢吞吞", "delay": 5}])
    )
    runner.start()
    q, _ = runner.bus.subscribe(session.id)

    runner.run_input(space.id, session.id, "hi")
    while True:
        f = q.get(timeout=5)
        if f.type == "status" and f.payload["status"] == "running":
            break

    runner.cancel(session.id)
    statuses = []
    while True:
        f = q.get(timeout=5)
        if f.type == "status":
            statuses.append(f.payload["status"])
            if f.payload["status"] == "cancelled":
                break

    assert "cancelled" in statuses
    assert store.get_session_meta(space.id, session.id).status == "cancelled"
    runner.shutdown()
    # 取消是用户自己点的，不用再进消息提醒一遍
    assert runner.panel.list_messages() == []


def test_runner_startup_error_goes_to_inbox(config, sa_home):
    """起不来（profile 不存在、没配 key）是最该提醒的失败：要走 _finalize 进消息，正文带原因。

    改动前这条分支自己落盘、自己发帧，绕开了收口点，消息里一条都没有。
    """
    store = SpaceStore(sa_home)
    space = store.create_space(SpaceSpec(name="t", kind="generic", profile="nope"))
    session = store.create_session(space.id)
    runner = Runner(config, store=store, llm_factory=_fake_factory([]))
    runner.start()
    q, _ = runner.bus.subscribe(session.id)

    runner.run_input(space.id, session.id, "hi")
    while True:
        f = q.get(timeout=5)
        if f.type == "status" and f.payload["status"] == "error":
            break
    runner.shutdown()

    assert "nope" in f.payload["reason"]
    assert f.payload["space_id"] == space.id
    assert store.get_session_meta(space.id, session.id).status == "error"
    [msg] = runner.panel.list_messages()
    assert msg["level"] == "error"
    assert msg["title"] == "t · 失败：hi"
    assert msg["ref"] == {"space_id": space.id, "session_id": session.id}
    assert "nope" in runner.panel.get_message(msg["id"])["body"]


# ------------------------------------------------- 9. 面板把完成的会话留在 recent 里
def test_panel_summary_lists_recent_done(config, sa_home):
    """跑完的任务要留在面板的 recent 里，而不是从列表消失。"""
    from simpleagent.serve.app import Server

    store = SpaceStore(sa_home)
    space = store.create_space(SpaceSpec(name="整理", kind="generic", profile="a"))
    session = store.create_session(space.id)
    runner = Runner(config, store=store, llm_factory=_fake_factory([{"content": "ok"}]))
    runner.start()
    q, _ = runner.bus.subscribe(session.id)

    runner.run_input(space.id, session.id, "hi")
    while True:
        f = q.get(timeout=5)
        if f.type == "status" and f.payload["status"] == "done":
            break
    runner.shutdown()

    app = Server(config, store=store, runner=runner)
    body = app.handle("GET", "/api/panel/summary", {}, b"").body

    assert body["running"] == []
    assert len(body["recent"]) == 1
    item = body["recent"][0]
    assert item["session_id"] == session.id
    assert item["space_id"] == space.id
    assert item["space_name"] == "整理"
    assert item["status"] == "done"
    assert item["verification"] == "unknown"
    assert item["updated_at"]


# ------------------------------------------------- 10. 超出留存窗口的完成记录会掉出
def test_panel_summary_drops_stale_recent(config, sa_home):
    """超过留存窗口的完成记录要从 recent 消失，别让面板无限长。"""
    from simpleagent.serve.app import Server

    store = SpaceStore(sa_home)
    space = store.create_space(SpaceSpec(name="t", kind="generic", profile="a"))
    session = store.create_session(space.id)
    store.update_meta(space.id, session.id, status="done")
    app = Server(config, store=store, runner=Runner(config, store=store))

    def recent_ids() -> list[str]:
        body = app.handle("GET", "/api/panel/summary", {}, b"").body
        return [item["session_id"] for item in body["recent"]]

    assert recent_ids() == [session.id]

    # 手动把 updated_at 推回 25 小时前
    meta = store.get_session_meta(space.id, session.id)
    meta.updated_at = (datetime.now(UTC) - timedelta(hours=25)).isoformat(timespec="milliseconds")
    store._write_meta(space.id, meta)

    assert recent_ids() == []


def test_context_edited_frame():
    from simpleagent.events import ContextEdited, Usage
    from simpleagent.serve.frames import event_to_frame

    frame = event_to_frame(ContextEdited("clear", 4000, 1000, 5000, 3), "s1")
    assert frame.type == "context_edited"
    assert frame.payload["count"] == 3
    assert frame.payload["summary"].startswith("清理了 3 个旧工具结果")
    assert frame.payload["usage"] is None and frame.payload["error"] is None

    usage = Usage(prompt_tokens=4000, completion_tokens=20, cached_tokens=3900)
    frame = event_to_frame(ContextEdited("compact", 4500, 1500, 5000, 8, usage=usage), "s1")
    assert frame.payload["kind"] == "compact"
    assert frame.payload["usage"]["cached_tokens"] == 3900
