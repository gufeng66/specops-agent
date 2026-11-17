"""线程快照 + 请求认领。业务库与 LangGraph checkpoint 分开，这里只保证本库事务。"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

import aiosqlite

from app.config import settings
from app.domain.models import (
    SNAPSHOT_SCHEMA_VERSION,
    ReviewInfo,
    ThreadSnapshot,
    ThreadStatus,
)

logger = logging.getLogger(__name__)

# 请求路径清理节流：每 N 次认领才做一次，且单批很小，避免把维护成本叠加到每次请求。
_PRUNE_EVERY = 20
_PRUNE_BATCH = 500
_PRUNE_MAX_SECONDS = 10.0
_claim_counter = 0


class PersistedStateTooNew(RuntimeError):
    """库内快照的 schema_version 高于本进程能理解的版本，拒绝写入/回放。"""


class StaleSnapshotError(RuntimeError):
    """写入版本落后于库内版本，或省略版本号却想更新已有线程。"""


def _assert_schema_version(snapshot: ThreadSnapshot) -> None:
    """写回或回放之前统一检查：旧程序不能覆盖未来版本的结构。"""
    if int(snapshot.schema_version) > SNAPSHOT_SCHEMA_VERSION:
        raise PersistedStateTooNew(
            f"快照 schema_version={snapshot.schema_version} 高于当前程序支持的 "
            f"{SNAPSHOT_SCHEMA_VERSION}；请升级服务后再读写，避免旧代码覆盖新结构"
        )


def _load_snapshot(blob: str) -> ThreadSnapshot:
    """读库统一入口：解析后立刻校验结构版本，未来版本不让旧代码继续处理。"""
    snap = ThreadSnapshot.model_validate_json(blob)
    _assert_schema_version(snap)
    return snap


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


@dataclass(frozen=True)
class FinishResult:
    """finish_request 的类型化结果：除了是否写入，还能区分冲突原因。

    applied=False 时 code 为 SNAPSHOT_CONFLICT（版本被推进）或 LEASE_LOST（租约已被别的
    请求接手）；找不到请求记录时 code 为空串。__bool__ 返回 applied，兼容旧代码的
    `if held:` / `if not held:` 写法。
    """

    applied: bool
    code: Literal["", "SNAPSHOT_CONFLICT", "LEASE_LOST"] = ""

    def __bool__(self) -> bool:
        return self.applied


_START_OK = {ThreadStatus.IDLE.value, ThreadStatus.COMPLETED.value, ThreadStatus.FAILED.value}
_HITL_OPS = {"clarify", "resume"}


class MemoryStore:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or settings.database_path

    async def ping(self) -> bool:
        """明确的数据库健康探测：只验证能连上并执行 SELECT 1，不读业务表。

        readiness 专用；异常返回 False 并记日志，不向外抛。与 list_running() 的业务
        查询语义分开，避免把「某张表能查」误当成「数据库整体可用」。
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("SELECT 1")
            return True
        except Exception:
            logger.exception("business db ping failed (db_path=%s)", self.db_path)
            return False

    async def schema_status(self) -> dict[str, Any]:
        """只读检查快照兼容性；单条坏数据不阻断其它线程。"""
        status: dict[str, Any] = {
            "ok": True,
            "degraded": False,
            "supported_version": SNAPSHOT_SCHEMA_VERSION,
            "max_version": 0,
            "future_count": 0,
            "corrupt_count": 0,
            "corrupt_thread_ids": [],
            "detail": "",
        }
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("SELECT thread_id, snapshot_json FROM threads") as cursor:
                    rows = await cursor.fetchall()
        except Exception as exc:
            status.update(ok=False, detail=f"读取业务快照失败：{exc}")
            return status

        corrupt_ids: list[str] = []
        for row in rows:
            thread_id = str(row["thread_id"])
            try:
                raw = json.loads(row["snapshot_json"])
                if not isinstance(raw, dict):
                    raise ValueError("snapshot JSON must be an object")
                version = int(raw.get("schema_version", 1))
                status["max_version"] = max(int(status["max_version"]), version)
                if version > SNAPSHOT_SCHEMA_VERSION:
                    status["future_count"] = int(status["future_count"]) + 1
                    continue
                ThreadSnapshot.model_validate(raw)
            except Exception:
                corrupt_ids.append(thread_id)

        status["corrupt_count"] = len(corrupt_ids)
        status["corrupt_thread_ids"] = corrupt_ids[:10]
        status["ok"] = int(status["future_count"]) == 0
        status["degraded"] = bool(corrupt_ids)
        details: list[str] = []
        if status["future_count"]:
            details.append(
                f"发现 {status['future_count']} 条未来 schema 快照（最大 {status['max_version']}）"
            )
        if corrupt_ids:
            details.append(f"发现 {len(corrupt_ids)} 条损坏快照，已局部隔离")
        status["detail"] = "；".join(details)
        return status

    async def init(self) -> None:
        """建表并补列。新增 run_id/graph_ns/snapshot_revision 列并回填，建索引。"""
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
            await _ensure_column(db, "threads", "run_id", "TEXT")
            await _ensure_column(db, "threads", "graph_ns", "TEXT")
            await _ensure_column(db, "threads", "snapshot_revision", "INTEGER NOT NULL DEFAULT 0")
            await _ensure_column(db, "threads", "checkpoint_id", "TEXT")
            await _ensure_column(db, "processed_requests", "status", "TEXT")
            await _ensure_column(db, "processed_requests", "digest", "TEXT")
            await _ensure_column(db, "processed_requests", "op", "TEXT")
            await _ensure_column(db, "processed_requests", "target_run_id", "TEXT")
            await _ensure_column(db, "processed_requests", "failure_code", "TEXT")
            await db.execute(
                "UPDATE processed_requests SET status = 'succeeded' WHERE status IS NULL"
            )
            # 旧库回填：run_id 之前只存在 snapshot_json 里。坏 JSON 跳过，不让迁移整体失败。
            async with db.execute(
                "SELECT thread_id, snapshot_json FROM threads WHERE run_id IS NULL OR run_id = ''"
            ) as cursor:
                legacy = await cursor.fetchall()
            backfilled = 0
            skipped = 0
            for thread_id, blob in legacy:
                try:
                    snap = _load_snapshot(blob)
                except Exception:
                    skipped += 1
                    logger.warning("skip run_id backfill for %s: unreadable snapshot", thread_id)
                    continue
                await db.execute(
                    "UPDATE threads SET run_id = ?, graph_ns = ?, snapshot_revision = ? "
                    "WHERE thread_id = ?",
                    (snap.run_id, snap.graph_ns, int(snap.revision), thread_id),
                )
                backfilled += 1
            if legacy:
                logger.info(
                    "run_id backfill complete total=%s backfilled=%s skipped=%s",
                    len(legacy),
                    backfilled,
                    skipped,
                )
            await db.execute("CREATE INDEX IF NOT EXISTS idx_threads_run_id ON threads(run_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_threads_status ON threads(status)")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_processed_requests_thread "
                "ON processed_requests(thread_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_processed_requests_status_created "
                "ON processed_requests(status, created_at)"
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
                return _load_snapshot(row["snapshot_json"])

    async def holds_lease(self, thread_id: str, request_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT lease_request_id FROM threads WHERE thread_id = ?", (thread_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return bool(row and row[0] == request_id)

    async def save_thread(
        self, snapshot: ThreadSnapshot, *, expected_revision: int | None = None
    ) -> bool:
        """写入整份快照。不改租约。created_at 只在首次写入。

        数据层契约：更新「已有线程」必须带 expected_revision 走 CAS，禁止无版本覆盖。
        expected_revision 给定时只有线程当前 snapshot_revision 与之相等才写入，成功后把
        逻辑版本推进到 current + 1（单调不回退，blob 与列保持一致），不匹配返回 False。
        expected_revision 省略时只允许「新建线程」；若线程已存在则抛 StaleSnapshotError，
        避免旧快照绕过 CAS 覆盖新状态。生产更新统一走 finish_request()。
        """
        _assert_schema_version(snapshot)
        now = _now_iso()
        if not snapshot.created_at:
            snapshot.created_at = now
        snapshot.updated_at = now
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                if expected_revision is not None:
                    async with db.execute(
                        "SELECT snapshot_revision FROM threads WHERE thread_id = ?",
                        (snapshot.thread_id,),
                    ) as cursor:
                        row = await cursor.fetchone()
                    if row is None:
                        # 只有「明确说这是新建」才允许插入；否则视为期望落空。
                        if int(expected_revision) != 0:
                            await db.commit()
                            return False
                        current = 0
                    else:
                        current = int(row[0])
                    if current != int(expected_revision):
                        await db.commit()
                        return False
                    snapshot.revision = current + 1
                else:
                    # 无版本号：只用于首次建线程。已有线程必须走 CAS，防止无版本覆盖。
                    async with db.execute(
                        "SELECT 1 FROM threads WHERE thread_id = ?",
                        (snapshot.thread_id,),
                    ) as cursor:
                        exists = await cursor.fetchone()
                    if exists:
                        await db.rollback()
                        raise StaleSnapshotError(
                            f"线程 {snapshot.thread_id} 已存在；更新必须传 expected_revision 走 CAS，"
                            "或用 finish_request() / claim_operation()"
                        )
                await db.execute(
                    """
                    INSERT INTO threads
                    (thread_id, status, snapshot_json, created_at, updated_at, run_id, graph_ns,
                     snapshot_revision)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(thread_id) DO UPDATE SET
                        status = excluded.status,
                        snapshot_json = excluded.snapshot_json,
                        updated_at = excluded.updated_at,
                        run_id = excluded.run_id,
                        graph_ns = excluded.graph_ns,
                        snapshot_revision = excluded.snapshot_revision
                    """,
                    (
                        snapshot.thread_id,
                        snapshot.status.value,
                        snapshot.model_dump_json(),
                        snapshot.created_at,
                        snapshot.updated_at,
                        snapshot.run_id,
                        snapshot.graph_ns,
                        int(snapshot.revision),
                    ),
                )
                await db.commit()
                return True
            except Exception:
                await db.rollback()
                raise

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
        # 长跑进程没有重启也要继续释放过期请求快照；节流 + 小批量，失败不影响认领。
        await self.maybe_prune_on_request()
        digest = operation_digest(op, thread_id, payload)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT thread_id, run_id, snapshot_json, status, digest, created_at
                    FROM processed_requests WHERE request_id = ?
                    """,
                    (request_id,),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing:
                    if _request_is_expired(existing["created_at"], existing["status"]):
                        await db.execute(
                            """
                            UPDATE processed_requests
                            SET status = 'expired', snapshot_json = '{}', failure_code = NULL
                            WHERE request_id = ? AND status != 'running'
                            """,
                            (request_id,),
                        )
                        await db.commit()
                        return ClaimResult(
                            outcome=ClaimOutcome.REJECTED,
                            code="REQUEST_EXPIRED",
                            message="该请求的结果已超出保留期，不能回放；请重新发起",
                        )
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
                    snap = _load_snapshot(trow["snapshot_json"])
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
                # 每次认领都推进逻辑版本：后续写入必须带上这个值才能覆盖。
                snap.revision = int(snap.revision) + 1
                snap.updated_at = now
                if not snap.created_at:
                    snap.created_at = now
                blob = snap.model_dump_json()
                if trow:
                    await db.execute(
                        """
                        UPDATE threads
                        SET status = ?, snapshot_json = ?, lease_request_id = ?, updated_at = ?,
                            run_id = ?, graph_ns = ?, snapshot_revision = ?
                        WHERE thread_id = ?
                        """,
                        (
                            snap.status.value,
                            blob,
                            request_id,
                            now,
                            snap.run_id,
                            snap.graph_ns,
                            snap.revision,
                            tid,
                        ),
                    )
                else:
                    await db.execute(
                        """
                        INSERT INTO threads
                        (thread_id, status, snapshot_json, created_at, updated_at, lease_request_id,
                         run_id, graph_ns, snapshot_revision)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            tid,
                            snap.status.value,
                            blob,
                            snap.created_at,
                            now,
                            request_id,
                            snap.run_id,
                            snap.graph_ns,
                            snap.revision,
                        ),
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
        expected_revision: int | None = None,
    ) -> FinishResult:
        """同一事务写快照、请求结果，并且只在租约仍属于本请求时释放。

        expected_revision 给定时做 CAS：旧写入不能覆盖新快照（迟到 SSE、跨标签页、
        重启后的迟到收尾都会带旧版本号）。失败返回带 code 的 FinishResult，
        区分 SNAPSHOT_CONFLICT / LEASE_LOST，且不写任何东西。
        """
        _assert_schema_version(snapshot)
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
                    return FinishResult(applied=False)
                tid = req["thread_id"]
                if expected_revision is None:
                    cur = await db.execute(
                        """
                        UPDATE threads
                        SET status = ?, snapshot_json = ?, lease_request_id = NULL, updated_at = ?,
                            run_id = ?, graph_ns = ?, snapshot_revision = snapshot_revision + 1
                        WHERE thread_id = ? AND lease_request_id = ?
                        """,
                        (
                            snapshot.status.value,
                            blob,
                            now,
                            snapshot.run_id,
                            snapshot.graph_ns,
                            tid,
                            request_id,
                        ),
                    )
                else:
                    cur = await db.execute(
                        """
                        UPDATE threads
                        SET status = ?, snapshot_json = ?, lease_request_id = NULL, updated_at = ?,
                            run_id = ?, graph_ns = ?, snapshot_revision = snapshot_revision + 1
                        WHERE thread_id = ? AND lease_request_id = ? AND snapshot_revision = ?
                        """,
                        (
                            snapshot.status.value,
                            blob,
                            now,
                            snapshot.run_id,
                            snapshot.graph_ns,
                            tid,
                            request_id,
                            expected_revision,
                        ),
                    )
                held = cur.rowcount == 1
                if held:
                    # 列自增和 JSON 里的 revision 必须是同一个值，否则读回来的是旧版本，
                    # 后续 CAS 会一直撞不上（线程行与 blob 不一致）。
                    async with db.execute(
                        "SELECT snapshot_revision FROM threads WHERE thread_id = ?",
                        (tid,),
                    ) as cursor:
                        row = await cursor.fetchone()
                    snapshot.revision = int(row["snapshot_revision"]) if row else 0
                    blob = snapshot.model_dump_json()
                    await db.execute(
                        "UPDATE threads SET snapshot_json = ? WHERE thread_id = ?",
                        (blob, tid),
                    )
                    await db.execute(
                        """
                        UPDATE processed_requests
                        SET status = ?, snapshot_json = ?, run_id = ?, failure_code = NULL
                        WHERE request_id = ?
                        """,
                        (request_status, blob, snapshot.run_id, request_id),
                    )
                else:
                    # 0 行有两种原因：租约已丢（别的请求接手）或版本被推进（旧写入迟到）。
                    # 两种情况都不能覆盖当前快照；由 abort_request() 条件释放租约并记录失败。
                    async with db.execute(
                        "SELECT lease_request_id, snapshot_revision FROM threads WHERE thread_id = ?",
                        (tid,),
                    ) as cursor:
                        row = await cursor.fetchone()
                    if row and row["lease_request_id"] == request_id:
                        code: Literal["", "SNAPSHOT_CONFLICT", "LEASE_LOST"] = "SNAPSHOT_CONFLICT"
                        logger.warning(
                            "SNAPSHOT_CONFLICT thread=%s request=%s expected=%s actual=%s",
                            tid,
                            request_id,
                            expected_revision,
                            row["snapshot_revision"],
                        )
                    else:
                        code = "LEASE_LOST"
                        logger.warning(
                            "SNAPSHOT_CONFLICT lease lost thread=%s request=%s", tid, request_id
                        )
                await db.commit()
                return FinishResult(applied=held, code="" if held else code)
            except Exception:
                await db.rollback()
                raise

    async def abort_request(
        self,
        request_id: str,
        code: Literal["SNAPSHOT_CONFLICT", "LEASE_LOST"],
    ) -> bool:
        """失败收尾只释放本请求的租约，不覆盖当前线程快照。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT thread_id FROM processed_requests WHERE request_id = ?", (request_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                if not row:
                    await db.commit()
                    return False
                thread_id = row["thread_id"]
                cur = await db.execute(
                    """
                    UPDATE threads SET lease_request_id = NULL
                    WHERE thread_id = ? AND lease_request_id = ?
                    """,
                    (thread_id, request_id),
                )
                await db.execute(
                    """
                    UPDATE processed_requests
                    SET status = 'failed', failure_code = ?
                    WHERE request_id = ? AND status = 'running'
                    """,
                    (code, request_id),
                )
                await db.commit()
                return cur.rowcount == 1
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
        """resume 用 run_id 反查线程。走 idx_threads_run_id，不再全表扫。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT snapshot_json, run_id FROM threads WHERE run_id = ? LIMIT 1",
                (run_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row:
            return _load_snapshot(row["snapshot_json"])
        # 迁移前写入的旧行 run_id 列为空，退化成一次带状态过滤的扫描作为兜底。
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT snapshot_json FROM threads WHERE run_id IS NULL OR run_id = ''"
            ) as cursor:
                async for legacy in cursor:
                    try:
                        snap = _load_snapshot(legacy["snapshot_json"])
                    except Exception:
                        continue
                    if snap.run_id == run_id:
                        return snap
        return None

    async def _prune_batch(self, cutoff: str, batch: int) -> int:
        """真正执行过期清理：完整快照换成合法空 JSON 对象 {}，保留元数据供幂等判定。"""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                UPDATE processed_requests
                SET snapshot_json = '{}', status = 'expired'
                WHERE request_id IN (
                    SELECT request_id FROM processed_requests
                    WHERE created_at < ? AND status != 'expired' AND status != 'running'
                    LIMIT ?
                )
                """,
                (cutoff, max(1, int(batch))),
            )
            pruned = cur.rowcount or 0
            await db.commit()
        return pruned

    async def _remaining_expired(self, cutoff: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT COUNT(*) FROM processed_requests
                WHERE created_at < ? AND status != 'expired' AND status != 'running'
                """,
                (cutoff,),
            ) as cursor:
                row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def prune_processed_requests(
        self,
        retention_days: int | None = None,
        batch: int = _PRUNE_BATCH,
        max_seconds: float = _PRUNE_MAX_SECONDS,
    ) -> int:
        """循环清理过期快照正文；保留 request_id 等幂等墓碑。"""
        days = settings.request_retention_days if retention_days is None else retention_days
        if days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
        started = time.monotonic()
        total = 0
        reason = "empty"
        while True:
            pruned = await self._prune_batch(cutoff, batch)
            total += pruned
            if pruned < max(1, int(batch)):
                break
            if time.monotonic() - started >= max_seconds:
                reason = "time_budget"
                break
        remaining = await self._remaining_expired(cutoff)
        logger.info(
            "processed request prune total=%s remaining=%s stop=%s", total, remaining, reason
        )
        return total

    async def maybe_prune_on_request(self) -> int:
        """请求路径轻量维护：每 _PRUNE_EVERY 次认领才触发一次，且带小批量上限，
        避免把全表清理叠加到每次请求上。清理失败只记日志，不影响主流程。"""
        global _claim_counter
        _claim_counter += 1
        if settings.request_retention_days <= 0:
            return 0
        if _claim_counter % _PRUNE_EVERY != 0:
            return 0
        try:
            return await self.prune_processed_requests(
                settings.request_retention_days, _PRUNE_BATCH, max_seconds=0.25
            )
        except Exception:
            logger.exception("请求路径轻量清理过期请求失败，忽略，主流程继续")
            return 0

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
    if status == "expired":
        return ClaimResult(
            outcome=ClaimOutcome.REJECTED,
            code="REQUEST_EXPIRED",
            message="该请求的结果已超出保留期，不能回放；请重新发起",
        )
    snap = None
    raw = existing["snapshot_json"]
    if raw and raw not in ("", "{}"):
        snap = _load_snapshot(raw)
    return ClaimResult(outcome=ClaimOutcome.REPLAY, snapshot=snap)


def _request_is_expired(created_at: object, status: object) -> bool:
    """过期检查使用 UTC；无效旧时间戳按不可安全回放处理。"""
    if settings.request_retention_days <= 0 or str(status or "") == "running":
        return False
    try:
        parsed = datetime.fromisoformat(str(created_at))
        if parsed.tzinfo is None:
            return True
    except (TypeError, ValueError):
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.request_retention_days)
    return parsed.astimezone(timezone.utc) < cutoff


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
