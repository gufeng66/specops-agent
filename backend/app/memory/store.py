"""线程快照 + 请求认领。业务库与 LangGraph checkpoint 分开，这里只保证本库事务。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

import aiosqlite

from app.config import settings
from app.domain.models import ReviewInfo, ThreadSnapshot, ThreadStatus


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def operation_digest(op: str, thread_id: str | None, payload: dict[str, Any]) -> str:
    """相同 request_id 必须带着相同操作摘要，否则是冲突而不是回放。"""
    raw = json.dumps(
        {"op": op, "thread_id": thread_id, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


class ClaimOutcome(str, Enum):
    CLAIMED = "claimed"
    REPLAY = "replay"
    IN_PROGRESS = "in_progress"
    CONFLICT = "conflict"
    REJECTED = "rejected"


@dataclass
class ClaimResult:
    outcome: ClaimOutcome
    snapshot: ThreadSnapshot | None = None
    code: str = ""
    message: str = ""


@dataclass
class RunningRequest:
    request_id: str
    thread_id: str
    run_id: str


_START_OK = {ThreadStatus.IDLE.value, ThreadStatus.COMPLETED.value, ThreadStatus.FAILED.value}
_HITL_OPS = {"clarify", "resume"}


class MemoryStore:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or settings.database_path

    async def init(self) -> None:
        """建表并补列。已有请求行标为 succeeded。迁移失败直接抛出。"""
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
            await _ensure_column(db, "threads", "lease_request_id", "TEXT")
            await _ensure_column(db, "processed_requests", "status", "TEXT")
            await _ensure_column(db, "processed_requests", "digest", "TEXT")
            await _ensure_column(db, "processed_requests", "op", "TEXT")
            await _ensure_column(db, "processed_requests", "target_run_id", "TEXT")
            await db.execute(
                "UPDATE processed_requests SET status = 'succeeded' WHERE status IS NULL"
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

    async def holds_lease(self, thread_id: str, request_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT lease_request_id FROM threads WHERE thread_id = ?", (thread_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return bool(row and row[0] == request_id)

    async def save_thread(self, snapshot: ThreadSnapshot) -> None:
        """upsert 整份快照。不改租约。created_at 只在首次写入。"""
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

    async def claim_operation(
        self,
        *,
        request_id: str,
        op: Literal["start", "clarify", "resume"],
        thread_id: str | None,
        payload: dict[str, Any],
        interrupt_id: str = "",
        interrupt_type: str = "",
        source_text: str | None = None,
        target_run_id: str = "",
    ) -> ClaimResult:
        """一次事务：校验请求、状态和中断，通过后才写入 running 并占用线程。"""
        digest = operation_digest(op, thread_id, payload)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT thread_id, run_id, snapshot_json, status, digest
                    FROM processed_requests WHERE request_id = ?
                    """,
                    (request_id,),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing:
                    await db.commit()
                    stored = existing["digest"]
                    if stored and stored != digest:
                        return ClaimResult(
                            outcome=ClaimOutcome.CONFLICT,
                            code="REQUEST_CONFLICT",
                            message="相同 request_id 对应了不同的线程或操作",
                        )
                    return _existing_claim(existing, digest)

                tid = thread_id or str(uuid4())
                async with db.execute(
                    "SELECT snapshot_json, lease_request_id FROM threads WHERE thread_id = ?",
                    (tid,),
                ) as cursor:
                    trow = await cursor.fetchone()

                if trow:
                    snap = ThreadSnapshot.model_validate_json(trow["snapshot_json"])
                    lease = trow["lease_request_id"]
                elif op == "start":
                    now = _now_iso()
                    snap = ThreadSnapshot(thread_id=tid, created_at=now, updated_at=now)
                    lease = None
                else:
                    await db.commit()
                    return ClaimResult(
                        outcome=ClaimOutcome.REJECTED,
                        code="THREAD_NOT_FOUND",
                        message=f"线程 {tid} 不存在",
                    )

                rejected = _reject_state(op, snap, lease, interrupt_id, interrupt_type)
                if rejected:
                    await db.commit()
                    return rejected

                run_id = str(uuid4())
                now = _now_iso()
                snap = _prepare_snapshot(snap, op, run_id, source_text)
                snap.updated_at = now
                if not snap.created_at:
                    snap.created_at = now
                blob = snap.model_dump_json()
                if trow:
                    await db.execute(
                        """
                        UPDATE threads
                        SET status = ?, snapshot_json = ?, lease_request_id = ?, updated_at = ?
                        WHERE thread_id = ?
                        """,
                        (snap.status.value, blob, request_id, now, tid),
                    )
                else:
                    await db.execute(
                        """
                        INSERT INTO threads
                        (thread_id, status, snapshot_json, created_at, updated_at, lease_request_id)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (tid, snap.status.value, blob, snap.created_at, now, request_id),
                    )
                await db.execute(
                    """
                    INSERT INTO processed_requests
                    (request_id, thread_id, run_id, snapshot_json, created_at, status, digest, op, target_run_id)
                    VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?)
                    """,
                    (request_id, tid, run_id, blob, now, digest, op, target_run_id),
                )
                await db.commit()
                return ClaimResult(outcome=ClaimOutcome.CLAIMED, snapshot=snap)
            except Exception:
                await db.rollback()
                raise

    async def finish_request(
        self,
        request_id: str,
        snapshot: ThreadSnapshot,
        *,
        request_status: Literal["succeeded", "failed"],
    ) -> bool:
        """同一事务写快照、请求结果，并且只在租约仍属于本请求时释放。"""
        now = _now_iso()
        snapshot.updated_at = now
        blob = snapshot.model_dump_json()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT thread_id FROM processed_requests WHERE request_id = ?",
                    (request_id,),
                ) as cursor:
                    req = await cursor.fetchone()
                if not req:
                    await db.commit()
                    return False
                tid = req["thread_id"]
                cur = await db.execute(
                    """
                    UPDATE threads
                    SET status = ?, snapshot_json = ?, lease_request_id = NULL, updated_at = ?
                    WHERE thread_id = ? AND lease_request_id = ?
                    """,
                    (snapshot.status.value, blob, now, tid, request_id),
                )
                held = cur.rowcount == 1
                if held:
                    await db.execute(
                        """
                        UPDATE processed_requests
                        SET status = ?, snapshot_json = ?, run_id = ?
                        WHERE request_id = ?
                        """,
                        (request_status, blob, snapshot.run_id, request_id),
                    )
                else:
                    failed = snapshot.model_copy(deep=True)
                    failed.status = ThreadStatus.FAILED
                    await db.execute(
                        """
                        UPDATE processed_requests
                        SET status = 'failed', snapshot_json = ?
                        WHERE request_id = ? AND status = 'running'
                        """,
                        (failed.model_dump_json(), request_id),
                    )
                await db.commit()
                return held
            except Exception:
                await db.rollback()
                raise

    async def list_running(self) -> list[RunningRequest]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT request_id, thread_id, run_id
                FROM processed_requests WHERE status = 'running'
                """
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            RunningRequest(request_id=row["request_id"], thread_id=row["thread_id"], run_id=row["run_id"])
            for row in rows
        ]

    async def get_processed(self, request_id: str) -> dict[str, str] | None:
        """按请求号读原操作和原目标运行。重放不能先拿当前 run_id 查线程。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT thread_id, op, target_run_id, status
                FROM processed_requests WHERE request_id = ?
                """,
                (request_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if not row:
            return None
        return {
            "thread_id": row["thread_id"],
            "op": row["op"] or "",
            "target_run_id": row["target_run_id"] or "",
            "status": row["status"] or "",
        }

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

    async def ensure_thread(self, thread_id: str | None = None) -> ThreadSnapshot:
        """有则返回，无则新建 idle 线程。"""
        tid = thread_id or str(uuid4())
        existing = await self.get_thread(tid)
        if existing:
            return existing
        snap = ThreadSnapshot(thread_id=tid, created_at=_now_iso(), updated_at=_now_iso())
        await self.save_thread(snap)
        return snap


def _existing_claim(existing: aiosqlite.Row, digest: str) -> ClaimResult:
    status = existing["status"] or "succeeded"
    if status == "running":
        return ClaimResult(
            outcome=ClaimOutcome.IN_PROGRESS,
            code="REQUEST_IN_PROGRESS",
            message="该请求仍在执行，还没有可回放的结果",
        )
    snap = None
    raw = existing["snapshot_json"]
    if raw and raw not in ("", "{}"):
        snap = ThreadSnapshot.model_validate_json(raw)
    return ClaimResult(outcome=ClaimOutcome.REPLAY, snapshot=snap)


def _reject_state(
    op: str,
    snap: ThreadSnapshot,
    lease: str | None,
    interrupt_id: str,
    interrupt_type: str,
) -> ClaimResult | None:
    if lease:
        return ClaimResult(
            outcome=ClaimOutcome.REJECTED,
            code="THREAD_BUSY",
            message=f"线程 {snap.thread_id} 正在处理中，请等待当前流程完成后再试",
            snapshot=snap,
        )
    if op == "start":
        if snap.status.value not in _START_OK:
            return ClaimResult(
                outcome=ClaimOutcome.REJECTED,
                code="THREAD_BUSY",
                message=f"线程 {snap.thread_id} 正在处理中，请等待当前流程完成后再试",
                snapshot=snap,
            )
        return None
    if op not in _HITL_OPS:
        return ClaimResult(
            outcome=ClaimOutcome.REJECTED,
            code="BAD_OPERATION",
            message=f"未知操作 {op}",
        )
    if snap.status != ThreadStatus.AWAITING_REVIEW:
        return ClaimResult(
            outcome=ClaimOutcome.REJECTED,
            code="THREAD_BUSY",
            message=f"线程 {snap.thread_id} 不在可恢复的审核状态",
            snapshot=snap,
        )
    expected = ""
    if isinstance(snap.interrupt_payload, dict):
        expected = str(snap.interrupt_payload.get("type") or "")
    if interrupt_id != snap.interrupt_id or (expected and interrupt_type != expected):
        return ClaimResult(
            outcome=ClaimOutcome.REJECTED,
            code="INTERRUPT_MISMATCH",
            message="当前中断已变化，请按最新面板重新提交",
            snapshot=snap,
        )
    return None


def _prepare_snapshot(
    snap: ThreadSnapshot,
    op: str,
    run_id: str,
    source_text: str | None,
) -> ThreadSnapshot:
    out = snap.model_copy(deep=True)
    out.run_id = run_id
    out.status = ThreadStatus.PLANNING
    if op == "start":
        out.graph_ns = f"{out.thread_id}:{run_id}"
        out.revision_notes = []
        out.source_text = source_text or ""
        out.markdown_output = ""
        out.interrupt_id = ""
        out.interrupt_payload = None
        out.task_draft.items = []
        out.validation.issues = []
        out.review = ReviewInfo()
    return out


async def _column_names(db: aiosqlite.Connection, table: str) -> set[str]:
    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        rows = await cursor.fetchall()
    return {row[1] for row in rows}


async def _ensure_column(db: aiosqlite.Connection, table: str, name: str, decl: str) -> None:
    if name in await _column_names(db, table):
        return
    await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


memory_store = MemoryStore()
