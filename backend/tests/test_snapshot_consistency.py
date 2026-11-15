"""快照一致性：CAS 旧版本不覆盖新快照；过保留期请求返回 REQUEST_EXPIRED。"""

import aiosqlite
import pytest

from app.domain.models import ThreadSnapshot, ThreadStatus
from app.memory.store import ClaimOutcome, ClaimResult, MemoryStore

PAYLOAD = {"source_text": "原文", "clarify_response": None}


async def _store(tmp_path, name="cas.db"):
    store = MemoryStore(str(tmp_path / name))
    await store.init()
    return store


async def _claim(store, thread_id: str, request_id: str):
    snap = await store.ensure_thread(thread_id)
    result = await store.claim_operation(
        request_id=request_id,
        op="start",
        thread_id=snap.thread_id,
        payload=PAYLOAD,
        source_text="原文",
    )
    assert isinstance(result, ClaimResult)
    assert result.outcome == ClaimOutcome.CLAIMED, result.message
    return result.snapshot


@pytest.mark.asyncio
async def test_stale_revision_cannot_overwrite(tmp_path):
    store = await _store(tmp_path)
    claimed = await _claim(store, "t-cas", "req-1")
    held_rev = claimed.revision

    first = claimed.model_copy(deep=True)
    first.status = ThreadStatus.AWAITING_REVIEW
    first.interrupt_id = "iid-1"
    assert (await store.finish_request("req-1", first, request_status="succeeded")).applied is True

    current = await store.get_thread("t-cas")
    assert current.status == ThreadStatus.AWAITING_REVIEW
    assert current.interrupt_id == "iid-1"
    assert current.revision > held_rev

    stale = current.model_copy(deep=True)
    stale.status = ThreadStatus.COMPLETED
    stale.interrupt_id = ""
    stale.markdown_output = "旧写入"
    held = await store.finish_request(
        "req-1", stale, request_status="succeeded", expected_revision=held_rev
    )
    assert held.applied is False
    # 第一次成功写入已经释放租约，所以这条迟到写入被判为 LEASE_LOST（lease 丢失优先于
    # 版本比较）；纯粹的 SNAPSHOT_CONFLICT 由 test_resilience.py 在租约仍持有时验证。
    assert held.code == "LEASE_LOST"

    after = await store.get_thread("t-cas")
    assert after.status == ThreadStatus.AWAITING_REVIEW
    assert after.interrupt_id == "iid-1"
    assert after.markdown_output == ""


@pytest.mark.asyncio
async def test_expired_request_cannot_replay(tmp_path):
    store = await _store(tmp_path, "expired.db")
    claimed = await _claim(store, "t-exp", "req-exp")
    done = claimed.model_copy(deep=True)
    done.status = ThreadStatus.AWAITING_REVIEW
    await store.finish_request("req-exp", done, request_status="succeeded")

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE processed_requests SET created_at = ? WHERE request_id = ?",
            ("2000-01-01T00:00:00+00:00", "req-exp"),
        )
        await db.commit()

    pruned = await store.prune_processed_requests(retention_days=7)
    assert pruned >= 1
    info = await store.get_processed("req-exp")
    assert info is not None
    assert info["status"] == "expired"

    again = await store.claim_operation(
        request_id="req-exp",
        op="start",
        thread_id="t-exp",
        payload=PAYLOAD,
        source_text="原文",
    )
    assert isinstance(again, ClaimResult)
    assert again.outcome == ClaimOutcome.REJECTED
    assert again.code == "REQUEST_EXPIRED"


@pytest.mark.asyncio
async def test_expired_request_is_rejected_even_before_maintenance_runs(tmp_path):
    store = await _store(tmp_path, "logical-expired.db")
    claimed = await _claim(store, "t-logical-exp", "req-logical-exp")
    done = claimed.model_copy(deep=True)
    done.status = ThreadStatus.AWAITING_REVIEW
    assert (await store.finish_request("req-logical-exp", done, request_status="succeeded")).applied

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE processed_requests SET created_at = ? WHERE request_id = ?",
            ("2000-01-01T00:00:00+00:00", "req-logical-exp"),
        )
        await db.commit()

    again = await store.claim_operation(
        request_id="req-logical-exp",
        op="start",
        thread_id="t-logical-exp",
        payload=PAYLOAD,
        source_text="原文",
    )
    assert again.code == "REQUEST_EXPIRED"
    async with aiosqlite.connect(store.db_path) as db:
        async with db.execute(
            "SELECT status, snapshot_json FROM processed_requests WHERE request_id = ?",
            ("req-logical-exp",),
        ) as cursor:
            row = await cursor.fetchone()
    assert row == ("expired", "{}")


@pytest.mark.asyncio
async def test_blank_run_id_is_backfilled_and_findable(tmp_path):
    db_path = tmp_path / "blank-run.db"
    store = MemoryStore(str(db_path))
    await store.init()
    snap = await store.ensure_thread("t-blank-run")
    snap.run_id = "legacy-run"
    await store.save_thread(snap, expected_revision=snap.revision)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE threads SET run_id = '' WHERE thread_id = ?", (snap.thread_id,))
        await db.commit()

    migrated = MemoryStore(str(db_path))
    await migrated.init()
    found = await migrated.find_by_run_id("legacy-run")
    assert found is not None
    assert found.thread_id == "t-blank-run"


@pytest.mark.asyncio
async def test_find_by_run_id_reads_run_column(tmp_path):
    store = await _store(tmp_path, "idx.db")
    claimed = await _claim(store, "t-idx", "req-idx")
    found = await store.find_by_run_id(claimed.run_id)
    assert isinstance(found, ThreadSnapshot)
    assert found.thread_id == "t-idx"
    assert await store.find_by_run_id("no-such-run") is None


@pytest.mark.asyncio
async def test_lost_lease_is_distinguishable(tmp_path, caplog):
    """租约被别的请求接手时不能覆盖快照，日志要能和版本冲突区分开。"""
    store = await _store(tmp_path, "lease.db")
    claimed = await _claim(store, "t-lease", "req-a")
    snap = claimed.model_copy(deep=True)
    snap.status = ThreadStatus.AWAITING_REVIEW
    assert (await store.finish_request("req-a", snap, request_status="succeeded")).applied is True

    stale = snap.model_copy(deep=True)
    stale.status = ThreadStatus.COMPLETED
    with caplog.at_level("WARNING"):
        lost = await store.finish_request("req-a", stale, request_status="succeeded")
    assert lost.applied is False
    assert lost.code == "LEASE_LOST"
    assert "lease lost" in caplog.text
    current = await store.get_thread("t-lease")
    assert current.status == ThreadStatus.AWAITING_REVIEW
