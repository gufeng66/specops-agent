"""认领事务、摘要冲突、失败对账和旧库补列。"""

import asyncio
import sqlite3

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.agent.graph import build_graph
from app.agent.workflow import SpecOpsWorkflow
from app.domain.models import ThreadSnapshot, ThreadStatus
from app.memory.store import ClaimOutcome, MemoryStore


@pytest.mark.asyncio
async def test_same_request_in_progress_and_conflict(tmp_path):
    store = MemoryStore(str(tmp_path / "c.db"))
    await store.init()
    await store.ensure_thread("t")
    payload = {"source_text": "会议目标：甲"}
    first = await store.claim_operation(
        request_id="r1", op="start", thread_id="t", payload=payload, source_text=payload["source_text"]
    )
    assert first.outcome == ClaimOutcome.CLAIMED
    running = await store.claim_operation(
        request_id="r1", op="start", thread_id="t", payload=payload, source_text=payload["source_text"]
    )
    assert running.outcome == ClaimOutcome.IN_PROGRESS
    assert running.snapshot is None
    other = await store.claim_operation(
        request_id="r1",
        op="start",
        thread_id="t",
        payload={"source_text": "会议目标：乙"},
        source_text="会议目标：乙",
    )
    assert other.outcome == ClaimOutcome.CONFLICT


@pytest.mark.asyncio
async def test_concurrent_claims_one_winner(tmp_path):
    store = MemoryStore(str(tmp_path / "race.db"))
    await store.init()
    await store.ensure_thread("t")

    async def claim(i: int):
        return await store.claim_operation(
            request_id=f"r{i}",
            op="start",
            thread_id="t",
            payload={"source_text": "会议目标：并发"},
            source_text="会议目标：并发",
        )

    results = await asyncio.gather(claim(1), claim(2))
    outcomes = [item.outcome for item in results]
    assert outcomes.count(ClaimOutcome.CLAIMED) == 1
    assert outcomes.count(ClaimOutcome.REJECTED) == 1


@pytest.mark.asyncio
async def test_interrupt_mismatch_and_clarify(tmp_path):
    store = MemoryStore(str(tmp_path / "hitl.db"))
    await store.init()
    snap = ThreadSnapshot(
        thread_id="t",
        status=ThreadStatus.AWAITING_REVIEW,
        interrupt_id="int-1",
        interrupt_payload={"type": "clarify", "interrupt_id": "int-1"},
        source_text="原文",
    )
    await store.save_thread(snap)
    bad = await store.claim_operation(
        request_id="c1",
        op="clarify",
        thread_id="t",
        payload={"source_text": "原文", "clarify_response": "目标", "interrupt_id": "old", "interrupt_type": "clarify"},
        interrupt_id="old",
        interrupt_type="clarify",
        source_text="原文",
    )
    assert bad.code == "INTERRUPT_MISMATCH"
    ok = await store.claim_operation(
        request_id="c2",
        op="clarify",
        thread_id="t",
        payload={"source_text": "原文", "clarify_response": "目标", "interrupt_id": "int-1", "interrupt_type": "clarify"},
        interrupt_id="int-1",
        interrupt_type="clarify",
        source_text="原文",
    )
    assert ok.outcome == ClaimOutcome.CLAIMED
    assert ok.snapshot is not None
    assert ok.snapshot.source_text == "原文"
    assert ok.snapshot.status == ThreadStatus.PLANNING


@pytest.mark.asyncio
async def test_finish_does_not_overwrite_lost_lease(tmp_path):
    store = MemoryStore(str(tmp_path / "lease.db"))
    await store.init()
    await store.ensure_thread("t")
    claimed = await store.claim_operation(
        request_id="owner",
        op="start",
        thread_id="t",
        payload={"source_text": "会议目标：租约"},
        source_text="会议目标：租约",
    )
    assert claimed.snapshot is not None
    async with __import__("aiosqlite").connect(store.db_path) as db:
        await db.execute("UPDATE threads SET lease_request_id = 'other' WHERE thread_id = 't'")
        await db.commit()
    stolen = claimed.snapshot.model_copy(deep=True)
    stolen.status = ThreadStatus.COMPLETED
    stolen.markdown_output = "不该写上"
    wrote = await store.finish_request("owner", stolen, request_status="succeeded")
    assert wrote is False
    current = await store.get_thread("t")
    assert current is not None
    assert current.markdown_output != "不该写上"


@pytest.mark.asyncio
async def test_migrate_existing_requests_to_succeeded(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE threads (
            thread_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE processed_requests (
            request_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO processed_requests VALUES ('old', 't', 'run', '{}', '2020-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    store = MemoryStore(str(path))
    await store.init()
    async with __import__("aiosqlite").connect(store.db_path) as db:
        async with db.execute("SELECT status FROM processed_requests WHERE request_id = 'old'") as cursor:
            row = await cursor.fetchone()
    assert row[0] == "succeeded"


@pytest.mark.asyncio
async def test_graph_exception_releases_thread(tmp_path):
    store = MemoryStore(str(tmp_path / "boom.db"))
    await store.init()
    graph = build_graph(MemorySaver())
    wf = SpecOpsWorkflow(store, graph=graph)

    async def explode(*_args, **_kwargs):
        raise RuntimeError("node failed")
        yield  # pragma: no cover

    graph.astream = explode  # type: ignore[method-assign]
    snap = await store.ensure_thread("t-boom")
    async for _ in wf.run_chat(snap, "会议目标：失败恢复\n参会人：张三\n张三负责收尾。", "req-boom"):
        pass
    saved = await store.get_thread("t-boom")
    assert saved is not None
    assert saved.status == ThreadStatus.FAILED
    nxt = await store.claim_operation(
        request_id="req-boom-2",
        op="start",
        thread_id="t-boom",
        payload={"source_text": "会议目标：再来"},
        source_text="会议目标：再来",
    )
    assert nxt.outcome == ClaimOutcome.CLAIMED


@pytest.mark.asyncio
async def test_cancel_releases_lease_without_restart(tmp_path):
    store = MemoryStore(str(tmp_path / "cancel.db"))
    await store.init()
    graph = build_graph(MemorySaver())
    started = asyncio.Event()

    async def hang(*_args, **_kwargs):
        started.set()
        await asyncio.sleep(30)
        if False:
            yield {}

    graph.astream = hang  # type: ignore[method-assign]
    wf = SpecOpsWorkflow(store, graph=graph)
    snap = await store.ensure_thread("t-cancel")

    async def consume():
        async for _event in wf.run_chat(snap, "会议目标：发布\n张三负责完成部署。", "req-cancel"):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = await store.get_thread("t-cancel")
    assert saved is not None
    assert saved.status == ThreadStatus.FAILED
    assert await store.holds_lease("t-cancel", "req-cancel") is False
    nxt = await store.claim_operation(
        request_id="req-cancel-2",
        op="start",
        thread_id="t-cancel",
        payload={"source_text": "会议目标：再来"},
        source_text="会议目标：再来",
    )
    assert nxt.outcome == ClaimOutcome.CLAIMED


@pytest.mark.asyncio
async def test_failed_run_keeps_llm_usage(tmp_path):
    from app.obs import add_llm_usage

    store = MemoryStore(str(tmp_path / "usage.db"))
    await store.init()
    graph = build_graph(MemorySaver())

    async def boom(*_args, **_kwargs):
        add_llm_usage(11, 7, 5)
        raise RuntimeError("node failed")
        yield {}

    graph.astream = boom  # type: ignore[method-assign]
    wf = SpecOpsWorkflow(store, graph=graph)
    snap = await store.ensure_thread("t-usage-fail")
    async for _event in wf.run_chat(snap, "会议目标：发布\n张三负责完成部署。", "req-usage-fail"):
        pass
    saved = await store.get_thread("t-usage-fail")
    assert saved is not None
    assert saved.token_in == 11
    assert saved.token_out == 7
    assert saved.latency_ms == 5
