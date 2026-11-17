"""数据层守卫：schema 版本拒绝、save_thread 强制 CAS、真并发写入、过期快照写 '{}'。

对应 m01951 第 5/6/7 项与 m02334 第 6/7 项。这些用例只验证跨模块契约，
不跑完整图，构造都直接落在 MemoryStore 与 SQLite 上。
"""

import asyncio

import aiosqlite
import pytest

from app.domain.models import ThreadStatus
from app.memory.store import (
    ClaimOutcome,
    ClaimResult,
    FinishResult,
    MemoryStore,
    PersistedStateTooNew,
    StaleSnapshotError,
    operation_digest,
)

PAYLOAD = {"source_text": "原文", "clarify_response": None}


async def _store(tmp_path, name="guards.db"):
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
async def test_schema_version_too_new_is_refused_on_write_and_read(tmp_path):
    """schema_version=99 不能被旧代码写回库，也不能被旧代码读出来当正常快照。"""
    store = await _store(tmp_path, "version.db")
    claimed = await _claim(store, "t-version", "req-version")
    done = claimed.model_copy(deep=True)
    done.status = ThreadStatus.AWAITING_REVIEW
    done.markdown_output = "原始内容"
    assert (await store.finish_request("req-version", done, request_status="succeeded")).applied is True
    baseline = await store.get_thread("t-version")

    too_new = baseline.model_copy(deep=True)
    too_new.schema_version = 99
    too_new.markdown_output = "未来版本覆盖"

    # 写路径 1：save_thread 拒绝，且库内原值不变。
    with pytest.raises(PersistedStateTooNew) as exc:
        await store.save_thread(too_new, expected_revision=baseline.revision)
    assert "schema_version=99" in str(exc.value)
    after_save = await store.get_thread("t-version")
    assert after_save.markdown_output == "原始内容"
    assert after_save.revision == baseline.revision

    # 写路径 2：finish_request 也拒绝，且库内原值不变。
    with pytest.raises(PersistedStateTooNew):
        await store.finish_request(
            "req-version", too_new, request_status="succeeded", expected_revision=baseline.revision
        )
    after_finish = await store.get_thread("t-version")
    assert after_finish.markdown_output == "原始内容"
    assert after_finish.revision == baseline.revision

    # 读路径：库里若真有一条未来版本记录，get_thread 不能当成正常快照返回。
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE threads SET snapshot_json = ? WHERE thread_id = ?",
            (too_new.model_dump_json(), "t-version"),
        )
        await db.commit()
    with pytest.raises(PersistedStateTooNew):
        await store.get_thread("t-version")


@pytest.mark.asyncio
async def test_save_thread_without_revision_cannot_update_existing(tmp_path):
    """省略 expected_revision 只允许新建；更新已有线程必须走 CAS。"""
    store = await _store(tmp_path, "stale.db")
    claimed = await _claim(store, "t-stale", "req-stale")

    dup = claimed.model_copy(deep=True)
    dup.markdown_output = "无版本覆盖"
    with pytest.raises(StaleSnapshotError) as exc:
        await store.save_thread(dup)
    assert "expected_revision" in str(exc.value)

    current = await store.get_thread("t-stale")
    assert current.markdown_output == ""


@pytest.mark.asyncio
async def test_save_thread_stale_expected_revision_returns_false(tmp_path):
    """已有 revision=5，旧快照 expected_revision=4 返回 False，库仍 revision=5。"""
    store = await _store(tmp_path, "rev.db")
    snap = await store.ensure_thread("t-rev")  # revision 0
    for _ in range(5):
        snap.markdown_output = f"内容-v{snap.revision}"
        assert await store.save_thread(snap, expected_revision=snap.revision) is True
        snap = await store.get_thread("t-rev")
    assert snap.revision == 5
    assert snap.markdown_output == "内容-v4"

    stale = snap.model_copy(deep=True)
    stale.markdown_output = "旧写入"
    assert await store.save_thread(stale, expected_revision=4) is False

    final = await store.get_thread("t-rev")
    assert final.revision == 5
    assert final.markdown_output == "内容-v4"


@pytest.mark.asyncio
async def test_concurrent_writes_same_expected_revision_only_one_wins(tmp_path):
    """两个协程抢同一 expected_revision：一个成功一个失败，库里只留成功者内容，
    失败者不释放成功者的租约，也不覆盖 processed_requests。"""
    store = await _store(tmp_path, "concurrent.db")
    claimed = await _claim(store, "t-conc", "req-conc")
    base_rev = claimed.revision

    w1 = claimed.model_copy(deep=True)
    w1.markdown_output = "writer-A"
    w2 = claimed.model_copy(deep=True)
    w2.markdown_output = "writer-B"

    results = await asyncio.gather(
        store.save_thread(w1, expected_revision=base_rev),
        store.save_thread(w2, expected_revision=base_rev),
    )
    assert sorted(results) == [False, True]

    winner = "writer-A" if results[0] else "writer-B"
    final = await store.get_thread("t-conc")
    assert final.markdown_output == winner
    assert final.revision == base_rev + 1

    # 失败者没有把成功者的租约释放掉，也没有动请求记录。
    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT lease_request_id, snapshot_revision FROM threads WHERE thread_id = ?",
            ("t-conc",),
        ) as cur:
            row = await cur.fetchone()
        assert row["lease_request_id"] == "req-conc"
        assert row["snapshot_revision"] == base_rev + 1
        async with db.execute(
            "SELECT status FROM processed_requests WHERE request_id = ?", ("req-conc",)
        ) as cur:
            preq = await cur.fetchone()
        assert preq["status"] == "running"


@pytest.mark.asyncio
async def test_prune_writes_empty_json_object(tmp_path):
    """过期请求快照必须换成合法空对象 '{}'，而不是空串，避免误调 model_validate_json('')。"""
    store = await _store(tmp_path, "prune.db")
    claimed = await _claim(store, "t-prune", "req-prune")
    done = claimed.model_copy(deep=True)
    done.status = ThreadStatus.AWAITING_REVIEW
    await store.finish_request("req-prune", done, request_status="succeeded")

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE processed_requests SET created_at = ? WHERE request_id = ?",
            ("2000-01-01T00:00:00+00:00", "req-prune"),
        )
        await db.commit()

    pruned = await store.prune_processed_requests(retention_days=7)
    assert pruned >= 1
    async with aiosqlite.connect(store.db_path) as db:
        async with db.execute(
            "SELECT snapshot_json, status FROM processed_requests WHERE request_id = ?",
            ("req-prune",),
        ) as cur:
            row = await cur.fetchone()
    assert row[0] == "{}"
    assert row[1] == "expired"


@pytest.mark.asyncio
async def test_prune_loops_past_one_batch_without_deleting_tombstones(tmp_path):
    store = await _store(tmp_path, "many-prune.db")
    async with aiosqlite.connect(store.db_path) as db:
        await db.executemany(
            """
            INSERT INTO processed_requests
            (request_id, thread_id, run_id, snapshot_json, created_at, status, digest, op, target_run_id)
            VALUES (?, ?, ?, ?, ?, 'succeeded', ?, 'start', '')
            """,
            [
                (f"old-{i}", "t-old", "r-old", '{"payload":true}', "2000-01-01T00:00:00+00:00", f"d-{i}")
                for i in range(501)
            ],
        )
        await db.commit()

    assert await store.prune_processed_requests(retention_days=7, batch=500) == 501
    async with aiosqlite.connect(store.db_path) as db:
        async with db.execute("SELECT COUNT(*), MIN(snapshot_json), MAX(status) FROM processed_requests WHERE request_id LIKE 'old-%'") as cursor:
            count, snapshot_json, status = await cursor.fetchone()
    assert count == 501
    assert snapshot_json == "{}"
    assert status == "expired"


def test_operation_digest_is_stable_under_key_order():
    """digest 只取决于内容，不因 dict 插入顺序漂移（供 chat.py strip 断言复用）。"""
    a = {"source_text": "原文", "clarify_response": "目标", "interrupt_id": "iid-1", "interrupt_type": "clarify"}
    b = {"interrupt_type": "clarify", "interrupt_id": "iid-1", "clarify_response": "目标", "source_text": "原文"}
    assert operation_digest("clarify", None, a) == operation_digest("clarify", None, b)


def test_finish_result_bool_matches_applied():
    """FinishResult.__bool__ 与 applied 一致，旧代码 `if held` 语义不变。"""
    assert bool(FinishResult(applied=True, code="")) is True
    assert bool(FinishResult(applied=False, code="SNAPSHOT_CONFLICT")) is False
    assert bool(FinishResult(applied=False, code="LEASE_LOST")) is False
