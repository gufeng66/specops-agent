"""线程快照 + 请求幂等。与 LangGraph checkpointer 分工：这里给人看的状态，那边存图游标。"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import aiosqlite

from app.config import settings
from app.domain.models import ThreadSnapshot, ThreadStatus
from app.errors import ThreadBusyError


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryStore:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or settings.database_path

    async def init(self) -> None:
        """建 threads / processed_requests 表。可重复调用。"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS threads (
                    thread_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_requests (
                    request_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            await db.commit()

    async def get_thread(self, thread_id: str) -> ThreadSnapshot | None:
        """按 thread_id 读快照；不存在返回 None。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT snapshot_json FROM threads WHERE thread_id = ?", (thread_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return ThreadSnapshot.model_validate_json(row["snapshot_json"])

    async def save_thread(self, snapshot: ThreadSnapshot) -> None:
        """upsert 整份快照。created_at 只在首次写入。"""
        now = _now_iso()
        if not snapshot.created_at:
            snapshot.created_at = now
        snapshot.updated_at = now
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO threads (thread_id, status, snapshot_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    status = excluded.status,
                    snapshot_json = excluded.snapshot_json,
                    updated_at = excluded.updated_at
                """,
                (
                    snapshot.thread_id,
                    snapshot.status.value,
                    snapshot.model_dump_json(),
                    snapshot.created_at,
                    snapshot.updated_at,
                ),
            )
            await db.commit()

    async def claim_request(
        self, request_id: str, thread_id: str | None = None
    ) -> tuple[bool, ThreadSnapshot | None]:
        """原子认领 request_id。重复请求返回 (False, 第一次快照)；忙碌线程抛 ThreadBusyError。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT snapshot_json FROM processed_requests WHERE request_id = ?",
                (request_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    snap = ThreadSnapshot.model_validate_json(row["snapshot_json"])
                    return False, snap

            if thread_id:
                async with db.execute(
                    "SELECT status FROM threads WHERE thread_id = ?", (thread_id,)
                ) as cursor:
                    trow = await cursor.fetchone()
                    if trow and trow["status"] in (
                        ThreadStatus.PLANNING.value,
                        ThreadStatus.AWAITING_REVIEW.value,
                    ):
                        raise ThreadBusyError(thread_id)

            return True, None

    async def find_by_run_id(self, run_id: str) -> ThreadSnapshot | None:
        """resume 用 run_id 反查线程。表无索引，线程量小可接受全表扫。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT snapshot_json FROM threads") as cursor:
                async for row in cursor:
                    snap = ThreadSnapshot.model_validate_json(row["snapshot_json"])
                    if snap.run_id == run_id:
                        return snap
        return None

    async def complete_request(
        self, request_id: str, thread_id: str, run_id: str, snapshot: ThreadSnapshot
    ) -> None:
        """把本次结果写入幂等表。INSERT OR IGNORE，重放不会覆盖第一次。"""
        now = _now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO processed_requests
                (request_id, thread_id, run_id, snapshot_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (request_id, thread_id, run_id, snapshot.model_dump_json(), now),
            )
            await db.commit()

    async def ensure_thread(self, thread_id: str | None = None) -> ThreadSnapshot:
        """有则返回，无则新建 idle 线程。"""
        tid = thread_id or str(uuid4())
        existing = await self.get_thread(tid)
        if existing:
            return existing
        snap = ThreadSnapshot(thread_id=tid, created_at=_now_iso(), updated_at=_now_iso())
        await self.save_thread(snap)
        return snap


memory_store = MemoryStore()
