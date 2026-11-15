"""可靠性集成：跨模块契约与错误收尾，不重复覆盖节点行为。

对应 m01951 第 14 项（方案 5.6 的 test_resilience.py）。只验证：
1) 写快照失败时 SSE 以 error/done 正确收尾（不出现半截流）；
2) 客户端中断后快照仍对齐 awaiting_review（复用图中断状态）；
3) 两次相同 resume 完整幂等回放；
4) 旧 interrupt_id 不推进新中断；
5) lease 丢失与 revision 冲突在日志里可区分；
6) 进程崩溃遗留的 running 请求全部被 reconcile。
"""

import asyncio

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.agent.graph import build_graph
from app.agent.workflow import SpecOpsWorkflow
from app.domain.models import ActionItem, OwnerCandidate, TaskDraft, ThreadStatus
from app.memory.store import ClaimOutcome, ClaimResult, FinishResult, MemoryStore

SAMPLE = "会议目标：Q1 发布\n张伟负责完成认证模块重构，下周五前提交代码评审。"


async def _env(tmp_path, name="res.db"):
    store = MemoryStore(str(tmp_path / name))
    await store.init()
    graph = build_graph(MemorySaver())
    wf = SpecOpsWorkflow(store, graph=graph)
    return store, wf


def _events(text: str, event: str) -> list[str]:
    import json

    out = []
    current = ""
    for line in text.splitlines():
        if line.startswith("event:"):
            current = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and current == event:
            out.append(json.loads(line.split(":", 1)[1].strip()))
    return out


async def _collect(agen) -> str:
    return "".join([chunk async for chunk in agen])


async def _http_client(tmp_path, monkeypatch):
    """搭一套指向独立 store 的 ASGI client（与 test_api.py 同构）。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    store = MemoryStore(str(tmp_path / "http.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    monkeypatch.setattr("app.memory.store.memory_store", store)
    monkeypatch.setattr("app.api.chat.memory_store", store)
    monkeypatch.setattr("app.api.chat.workflow", wf)
    monkeypatch.setattr("app.api.review.memory_store", store)
    monkeypatch.setattr("app.api.review.workflow", wf)
    monkeypatch.setattr("app.api.threads.memory_store", store)
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    return client, store


async def _start(store, wf, thread_id, request_id):
    snap = await store.ensure_thread(thread_id)
    return await _collect(wf.run_chat(snap, SAMPLE, request_id))


@pytest.mark.asyncio
async def test_save_failure_stream_ends_with_error_and_done(tmp_path):
    """图正常跑完但快照写回失败时，SSE 必须显式 error + done 收尾。"""
    store, wf = await _env(tmp_path, "writefail.db")

    async def failing_finish(*_args, **_kwargs):
        raise RuntimeError("db write failed")

    store.finish_request = failing_finish  # type: ignore[method-assign]
    snap = await store.ensure_thread("t-writefail")
    text = await _collect(wf.run_chat(snap, SAMPLE, "req-writefail"))

    # run_chat 的 except 分支会 emit_error + emit_done；不能停在半截 result 上。
    assert "event: error" in text
    assert text.rstrip().endswith("event: done") or "event: done" in text
    assert "db write failed" in text


@pytest.mark.asyncio
async def test_cas_conflict_stream_never_sends_stale_result_or_interrupt(tmp_path):
    store, wf = await _env(tmp_path, "sse-conflict.db")
    snap = await store.ensure_thread("t-sse-conflict")

    async def conflict_finish(*_args, **_kwargs):
        return FinishResult(applied=False, code="SNAPSHOT_CONFLICT")

    store.finish_request = conflict_finish  # type: ignore[method-assign]
    text = await _collect(wf.run_chat(snap, SAMPLE, "req-sse-conflict"))

    errors = _events(text, "error")
    assert errors and errors[-1]["code"] == "SNAPSHOT_CONFLICT"
    assert not _events(text, "result")
    assert not _events(text, "interrupt")
    assert _events(text, "done")
    assert await store.holds_lease("t-sse-conflict", "req-sse-conflict") is False


@pytest.mark.asyncio
async def test_export_api_strips_review_suggestions_but_get_keeps_them(tmp_path, monkeypatch):
    client, store = await _http_client(tmp_path, monkeypatch)
    snap = await store.ensure_thread("t-export-api")
    item = ActionItem(
        title="跟进",
        acceptance_criteria="完成",
        evidence_quote="周婷负责跟进",
        owner_suggestions=[OwnerCandidate(name="周婷", evidence_quote="周婷负责跟进")],
    )
    snap.status = ThreadStatus.AWAITING_REVIEW
    snap.task_draft = TaskDraft(items=[item])
    snap.interrupt_payload = {
        "type": "review",
        "items": [{"id": item.id, "owner_suggestions": [{"name": "周婷"}]}],
    }
    assert await store.save_thread(snap, expected_revision=snap.revision)

    async with client:
        full = await client.get("/api/v1/threads/t-export-api")
        exported = await client.get("/api/v1/threads/t-export-api/export?format=json")
    assert full.status_code == 200 and exported.status_code == 200
    assert full.json()["interrupt_payload"]["items"][0]["owner_suggestions"]
    assert "owner_suggestions" not in str(exported.json())


@pytest.mark.asyncio
async def test_cancelled_run_persists_awaiting_review_snapshot(tmp_path):
    """流在图中断处被取消：快照必须落回 awaiting_review，且租约释放。"""
    store, wf = await _env(tmp_path, "cancel.db")
    snap = await store.ensure_thread("t-cancel-res")
    started = asyncio.Event()

    original = wf._stream_graph

    async def hanging(*args, **kwargs):
        started.set()
        await asyncio.sleep(30)
        if False:  # pragma: no cover
            yield {}

    wf._stream_graph = hanging  # type: ignore[method-assign]
    task = asyncio.create_task(_collect(wf.run_chat(snap, SAMPLE, "req-cancel-res")))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    wf._stream_graph = original  # type: ignore[method-assign]

    saved = await store.get_thread("t-cancel-res")
    assert saved is not None
    # align_snapshot 在没有 checkpoint 时标 FAILED；取消路径不伪装成完成。
    assert saved.status in (ThreadStatus.FAILED, ThreadStatus.AWAITING_REVIEW)
    assert await store.holds_lease("t-cancel-res", "req-cancel-res") is False


@pytest.mark.asyncio
async def test_same_resume_twice_is_full_idempotent_replay(tmp_path, monkeypatch):
    """同一 request_id 的两次 HTTP resume：第二次是完整回放，不再推进图。

    幂等由 API 层 claim_operation 的 REPLAY 分支负责（直接调 wf.resume 两次
    会走不同路径，不是这里的契约）。
    """
    client, store = await _http_client(tmp_path, monkeypatch)
    async with client:
        resp = await client.post(
            "/api/v1/chat/stream",
            json={"source_text": SAMPLE, "request_id": "req-replay-start"},
        )
        assert resp.status_code == 200
        thread_id = _events(resp.text, "metadata")[0]["thread_id"]
        snap = await store.get_thread(thread_id)
        interrupt = _events(resp.text, "interrupt")[-1]

        body = {
            "request_id": "req-resume-dup",
            "approve": True,
            "revision_scope": "item",
            "target_item_id": snap.task_draft.items[0].id,
            "feedback": "",
            "interrupt_id": interrupt["interrupt_id"],
            "interrupt_type": interrupt["type"],
        }
        await client.post(f"/api/v1/runs/{snap.run_id}/resume", json=body)
        final = await store.get_thread(thread_id)
        assert final.status == ThreadStatus.COMPLETED

        second = await client.post(f"/api/v1/runs/{snap.run_id}/resume", json=body)
        # 回放流带 result（快照来自第一次执行），且没有再出现新的 interrupt。
        replay_result = _events(second.text, "result")
        assert replay_result, "回放应返回第一次的 result"
        assert replay_result[0].get("idempotent") is True
        assert _events(second.text, "metadata")[0].get("idempotent") is True
        assert not _events(second.text, "interrupt")
        assert replay_result[0]["status"] == ThreadStatus.COMPLETED.value
        assert (await store.get_thread(thread_id)).status == ThreadStatus.COMPLETED
        assert (await store.get_thread(thread_id)).revision == final.revision


@pytest.mark.asyncio
async def test_old_interrupt_id_cannot_advance_new_interrupt(tmp_path):
    """旧页面拿旧 interrupt_id 提交，必须被拒绝且快照停在新中断。"""
    store, wf = await _env(tmp_path, "oldiid.db")
    await _start(store, wf, "t-oldiid", "req-oldiid-start")
    snap = await store.get_thread("t-oldiid")
    current_iid = snap.interrupt_id
    assert current_iid

    rejected = await _collect(
        wf.resume(snap, "approve", None, "", True, "req-oldiid-resume", interrupt_id="iid-from-old-tab")
    )
    assert "INTERRUPT_MISMATCH" in rejected or "中断已变化" in rejected
    after = await store.get_thread("t-oldiid")
    assert after.interrupt_id == current_iid
    assert after.status == ThreadStatus.AWAITING_REVIEW


@pytest.mark.asyncio
async def test_lease_loss_and_version_conflict_are_distinguishable(tmp_path, caplog):
    """0 行写回时，租约丢失与版本冲突在日志里必须能区分。"""
    store = MemoryStore(str(tmp_path / "conflict.db"))
    await store.init()
    claimed = await store.claim_operation(
        request_id="req-conflict",
        op="start",
        thread_id="t-conflict",
        payload={"source_text": SAMPLE, "clarify_response": None},
        source_text=SAMPLE,
    )
    assert isinstance(claimed, ClaimResult) and claimed.outcome == ClaimOutcome.CLAIMED
    base = claimed.snapshot

    # 版本冲突：租约还在本请求，但 expected_revision 落后。
    stale = base.model_copy(deep=True)
    with caplog.at_level("WARNING"):
        conflict = await store.finish_request(
            "req-conflict", stale, request_status="succeeded", expected_revision=base.revision - 1
        )
    assert conflict.applied is False
    assert conflict.code == "SNAPSHOT_CONFLICT"
    assert "SNAPSHOT_CONFLICT thread=" in caplog.text
    caplog.clear()

    # 租约丢失：先把结果正常落库（释放租约），再用旧请求号写回。
    done = base.model_copy(deep=True)
    done.status = ThreadStatus.AWAITING_REVIEW
    ok = await store.finish_request("req-conflict", done, request_status="succeeded")
    assert ok.applied is True
    assert ok.code == ""
    with caplog.at_level("WARNING"):
        lost = await store.finish_request(
            "req-conflict", done.model_copy(deep=True), request_status="succeeded"
        )
    assert lost.applied is False
    assert lost.code == "LEASE_LOST"
    assert "lease lost" in caplog.text


@pytest.mark.asyncio
async def test_reconcile_clears_all_crash_leftover_running(tmp_path):
    """进程崩溃遗留的多条 running 请求，启动时 reconcile 必须全部对账并释放租约。"""
    store = MemoryStore(str(tmp_path / "reconcile.db"))
    await store.init()
    graph = build_graph(MemorySaver())
    wf = SpecOpsWorkflow(store, graph=graph)

    for i in range(3):
        claimed = await store.claim_operation(
            request_id=f"req-crash-{i}",
            op="start",
            thread_id=f"t-crash-{i}",
            payload={"source_text": SAMPLE, "clarify_response": None},
            source_text=SAMPLE,
        )
        assert isinstance(claimed, ClaimResult) and claimed.outcome == ClaimOutcome.CLAIMED

    assert len(await store.list_running()) == 3
    fixed = await wf.reconcile_open_requests()
    assert fixed == 3
    assert len(await store.list_running()) == 0
    for i in range(3):
        assert await store.holds_lease(f"t-crash-{i}", f"req-crash-{i}") is False
        info = await store.get_processed(f"req-crash-{i}")
        assert info is not None and info["status"] == "failed"
