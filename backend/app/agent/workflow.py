"""HTTP 运行时：把图的 updates 编成 SSE，并把快照写入 SQLite。

clarify 走 /chat/stream + clarify_response（同一 interrupt 续跑）；
分配/审核走 /runs/{id}/resume。graph_ns 必须跨 HITL 稳定，否则 checkpoint 对不上。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator

from langgraph.types import Command

from app.agent.events import (
    emit_done,
    emit_error,
    emit_interrupt,
    emit_metadata,
    emit_progress,
    emit_result,
    emit_tool_result,
)
from app.agent.graph import get_compiled_graph
from app.agent.prompts import PROMPT_VERSION
from app.agent.state import snapshot_to_state, state_to_snapshot
from app.domain.models import ThreadSnapshot, ThreadStatus
from app.memory.store import ClaimOutcome, ClaimResult, MemoryStore
from app.obs import bind_request, flush_metrics

logger = logging.getLogger(__name__)
_CLEANUP_SECONDS = 5


class SpecOpsWorkflow:
    """把图的 updates 编成 SSE，并把快照写入 SQLite。"""

    def __init__(self, store: MemoryStore, graph=None):
        self.store = store
        self._graph = graph

    @property
    def graph(self):
        """延迟取编译图，让 import 期的 API 单例能吃到 lifespan 绑定的 checkpointer。"""
        if self._graph is None:
            self._graph = get_compiled_graph()
        return self._graph

    @graph.setter
    def graph(self, value) -> None:
        self._graph = value

    def _config(self, graph_ns: str) -> dict[str, Any]:
        """LangGraph configurable.thread_id 必须跨 HITL 稳定，否则 checkpoint 对不上。"""
        return {"configurable": {"thread_id": graph_ns}}

    async def run_chat(
        self,
        snapshot: ThreadSnapshot,
        source_text: str,
        request_id: str,
        clarify_response: str | None = None,
        eval_mode: str | None = None,
        interrupt_id: str | None = None,
        interrupt_type: str | None = None,
    ) -> AsyncIterator[str]:
        """开跑或澄清续跑。澄清用 Command(resume)，graph_ns 沿用，不新开 checkpoint。

        eval_mode 只给评测。不传时节点保持原行为。
        """
        op = "clarify" if clarify_response else "start"
        payload = {"source_text": source_text, "clarify_response": clarify_response}
        if op == "clarify":
            payload["interrupt_id"] = interrupt_id if interrupt_id is not None else snapshot.interrupt_id
            payload["interrupt_type"] = interrupt_type or str(
                (snapshot.interrupt_payload or {}).get("type") or ""
            )
        claimed = await self._claim_for_run(
            snapshot,
            request_id,
            op,
            payload,
            interrupt_id=str(payload.get("interrupt_id") or ""),
            interrupt_type=str(payload.get("interrupt_type") or ""),
            source_text=source_text,
        )
        if isinstance(claimed, ClaimResult):
            async for event in self._reject_stream(claimed):
                yield event
            return
        snapshot = claimed
        run_id = snapshot.run_id
        resuming_clarify = bool(clarify_response and snapshot.graph_ns and snapshot.interrupt_id)
        started = time.perf_counter()

        with bind_request(request_id, run_id):
            logger.info("chat start")
            try:
                yield emit_metadata(
                    {"run_id": run_id, "thread_id": snapshot.thread_id, "request_id": request_id}
                )
                if resuming_clarify:
                    resume_payload = {
                        "clarify_response": clarify_response,
                        "feedback": clarify_response,
                        "goal": clarify_response,
                    }
                    async for event in self._stream_graph(
                        snapshot,
                        Command(resume=resume_payload),
                        request_id=request_id,
                        run_id=run_id,
                        started=started,
                    ):
                        yield event
                    return

                initial = snapshot_to_state(
                    snapshot,
                    source_text=snapshot.source_text,
                    revision_notes=list(snapshot.revision_notes),
                    run_id=run_id,
                    status=ThreadStatus.PLANNING.value,
                    clarify_response=clarify_response,
                    graph_ns=snapshot.graph_ns,
                    pending_events=[],
                    interrupt_id="",
                    interrupt_payload=None,
                    review_action=None,
                    prompt_version=PROMPT_VERSION,
                )
                initial["prompt_version"] = PROMPT_VERSION
                snapshot.prompt_version = PROMPT_VERSION
                if eval_mode:
                    initial["eval_mode"] = eval_mode
                async for event in self._stream_graph(
                    snapshot,
                    initial,
                    request_id=request_id,
                    run_id=run_id,
                    started=started,
                ):
                    yield event
            except asyncio.CancelledError:
                await self._finish_cancelled(snapshot, request_id, run_id, started)
                raise
            except Exception as exc:
                logger.exception("run_chat failed")
                await self._persist_aligned(snapshot, request_id, run_id, started)
                yield emit_error(str(exc))
                yield emit_done()

    async def resume(
        self,
        snapshot: ThreadSnapshot,
        revision_scope: str,
        target_item_id: str | None,
        feedback: str,
        approve: bool,
        request_id: str,
        assignments: list[dict[str, Any]] | None = None,
        drop_item_ids: list[str] | None = None,
        interrupt_id: str | None = None,
        interrupt_type: str | None = None,
        item_patch: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """分配/审核续跑。assignments 只在人确认后进入图，建议字段不会从这里写入主责。"""
        iid = snapshot.interrupt_id if interrupt_id is None else interrupt_id
        itype = interrupt_type or str((snapshot.interrupt_payload or {}).get("type") or "")
        payload = {
            "revision_scope": revision_scope,
            "target_item_id": target_item_id,
            "feedback": feedback,
            "approve": approve,
            "assignments": assignments or [],
            "drop_item_ids": drop_item_ids or [],
            "interrupt_id": iid,
            "interrupt_type": itype,
            "item_patch": item_patch,
        }
        claimed = await self._claim_for_run(
            snapshot,
            request_id,
            "resume",
            payload,
            interrupt_id=iid,
            interrupt_type=itype,
        )
        if isinstance(claimed, ClaimResult):
            async for event in self._reject_stream(claimed):
                yield event
            return
        snapshot = claimed
        run_id = snapshot.run_id
        started = time.perf_counter()
        with bind_request(request_id, run_id):
            logger.info("resume start")
            try:
                yield emit_metadata(
                    {"run_id": run_id, "thread_id": snapshot.thread_id, "request_id": request_id}
                )
                graph_payload = {
                    "approve": approve,
                    "revision_scope": revision_scope,
                    "target_item_id": target_item_id,
                    "feedback": feedback,
                    "assignments": assignments or [],
                    "drop_item_ids": drop_item_ids or [],
                    "item_patch": item_patch,
                }
                async for event in self._stream_graph(
                    snapshot,
                    Command(resume=graph_payload),
                    request_id=request_id,
                    run_id=run_id,
                    started=started,
                ):
                    yield event
            except asyncio.CancelledError:
                await self._finish_cancelled(snapshot, request_id, run_id, started)
                raise
            except Exception as exc:
                logger.exception("resume failed")
                await self._persist_aligned(snapshot, request_id, run_id, started)
                yield emit_error(str(exc))
                yield emit_done()

    async def _claim_for_run(
        self,
        snapshot: ThreadSnapshot,
        request_id: str,
        op: str,
        payload: dict[str, Any],
        *,
        interrupt_id: str,
        interrupt_type: str,
        source_text: str | None = None,
    ) -> ThreadSnapshot | ClaimResult:
        """API 已认领则复用；测试直接调 workflow 时在这里认领。"""
        if snapshot.thread_id and await self.store.holds_lease(snapshot.thread_id, request_id):
            current = await self.store.get_thread(snapshot.thread_id)
            return current or snapshot
        result = await self.store.claim_operation(
            request_id=request_id,
            op=op,  # type: ignore[arg-type]
            thread_id=snapshot.thread_id or None,
            payload=payload,
            interrupt_id=interrupt_id,
            interrupt_type=interrupt_type,
            source_text=source_text,
        )
        if result.outcome != ClaimOutcome.CLAIMED or result.snapshot is None:
            return result
        return result.snapshot

    async def _reject_stream(self, result: ClaimResult) -> AsyncIterator[str]:
        yield emit_error(result.message or result.code or "请求被拒绝")
        yield emit_done()

    async def _persist_aligned(
        self,
        snapshot: ThreadSnapshot,
        request_id: str,
        run_id: str,
        started: float,
    ) -> None:
        """按 checkpoint 修复业务快照。中间节点失败标 failed，不从该节点续跑。"""
        try:
            aligned = await self.align_snapshot(snapshot)
        except Exception:
            logger.exception("align snapshot failed")
            aligned = snapshot.model_copy(deep=True)
            aligned.status = ThreadStatus.FAILED
        aligned.run_id = run_id or aligned.run_id
        aligned.wall_ms = int((time.perf_counter() - started) * 1000)
        flush_metrics(aligned)
        await self.store.finish_request(request_id, aligned, request_status="failed")

    async def _finish_cancelled(
        self,
        snapshot: ThreadSnapshot,
        request_id: str,
        run_id: str,
        started: float,
    ) -> None:
        """取消域里先放开本次取消，再在时限内把占用写回去，然后继续向外抛出取消。"""
        current = asyncio.current_task()
        if current is not None:
            while current.cancelling():
                current.uncancel()
        work = asyncio.create_task(self._persist_aligned(snapshot, request_id, run_id, started))
        try:
            await asyncio.wait_for(asyncio.shield(work), _CLEANUP_SECONDS)
        except TimeoutError:
            work.cancel()
            logger.exception("cancel cleanup timed out")
        except Exception:
            logger.exception("cancel cleanup failed")

    async def align_snapshot(self, snapshot: ThreadSnapshot) -> ThreadSnapshot:
        """checkpoint 决定位置。原中断、新中断、已完成、中间失败各自写回。"""
        ns = snapshot.graph_ns
        if not ns:
            out = snapshot.model_copy(deep=True)
            out.status = ThreadStatus.FAILED
            return out
        graph_state = await self.graph.aget_state(self._config(ns))
        values = dict(graph_state.values or {})
        interrupts = self._extract_interrupts(graph_state)
        nxt = tuple(getattr(graph_state, "next", ()) or ())
        if interrupts:
            payload = interrupts[0]
            values["status"] = ThreadStatus.AWAITING_REVIEW.value
            values["interrupt_id"] = payload.get("interrupt_id") or values.get("interrupt_id") or ""
            values["interrupt_payload"] = payload
            values["thread_id"] = snapshot.thread_id
            values["graph_ns"] = ns
            snap = state_to_snapshot(values, snapshot)
            snap.interrupt_payload = payload
            snap.status = ThreadStatus.AWAITING_REVIEW
            snap.graph_ns = ns
            return snap
        if nxt or not values:
            out = snapshot.model_copy(deep=True)
            out.status = ThreadStatus.FAILED
            return out
        values["thread_id"] = snapshot.thread_id
        values["graph_ns"] = ns
        snap = state_to_snapshot(values, snapshot)
        snap.graph_ns = ns
        if values.get("status") == ThreadStatus.COMPLETED.value or snap.markdown_output:
            snap.status = ThreadStatus.COMPLETED
        else:
            snap.status = ThreadStatus.FAILED
        return snap

    async def reconcile_open_requests(self) -> int:
        """启动时把遗留 running 按 checkpoint 对账，并解除仍属于该请求的占用。"""
        fixed = 0
        for row in await self.store.list_running():
            snap = await self.store.get_thread(row.thread_id)
            if snap is None:
                continue
            aligned = await self.align_snapshot(snap)
            aligned.run_id = row.run_id or aligned.run_id
            await self.store.finish_request(row.request_id, aligned, request_status="failed")
            fixed += 1
        return fixed

    async def _stream_graph(
        self,
        snapshot: ThreadSnapshot,
        input_data: Any,
        *,
        request_id: str,
        run_id: str,
        started: float,
    ) -> AsyncIterator[str]:
        """astream(updates) 把节点 pending_events 编成 SSE；检测到 interrupt 发 waiting。"""
        ns = snapshot.graph_ns or snapshot.thread_id
        config = self._config(ns)
        last_state: dict[str, Any] = {}

        from langgraph.types import Command as ResumeCommand

        if not isinstance(input_data, ResumeCommand):
            yield emit_progress("intake", "解析会议输入", status="running")

        async for chunk in self.graph.astream(input_data, config, stream_mode="updates"):
            if not isinstance(chunk, dict):
                continue
            for _node_name, update in chunk.items():
                if not isinstance(update, dict):
                    continue
                last_state.update(update)
                for ev in update.get("pending_events") or []:
                    sse = self._pending_to_sse(ev)
                    if sse:
                        yield sse

        graph_state = await self.graph.aget_state(config)
        values = dict(graph_state.values or {})
        values.update({k: v for k, v in last_state.items() if k != "pending_events"})
        values["run_id"] = run_id
        values["thread_id"] = snapshot.thread_id
        values["graph_ns"] = ns

        interrupts = self._extract_interrupts(graph_state)
        if interrupts:
            payload = interrupts[0]
            values["interrupt_id"] = payload.get("interrupt_id") or values.get("interrupt_id") or ""
            values["status"] = ThreadStatus.AWAITING_REVIEW.value
            values["interrupt_payload"] = payload
            snap = state_to_snapshot(values, snapshot)
            snap.graph_ns = ns
            snap.interrupt_payload = payload
            flush_metrics(snap)
            snap.wall_ms = int((time.perf_counter() - started) * 1000)
            await self.store.finish_request(request_id, snap, request_status="succeeded")
            hitl_node = str(payload.get("type") or "review")
            yield emit_progress(hitl_node, str(payload.get("message") or "等待人工操作"), status="waiting")
            yield emit_interrupt(payload)
            yield emit_result(snap.model_dump(mode="json"))
            yield emit_done()
            return

        snap = state_to_snapshot(values, snapshot)
        snap.graph_ns = ns
        flush_metrics(snap)
        snap.wall_ms = int((time.perf_counter() - started) * 1000)
        await self.store.finish_request(request_id, snap, request_status="succeeded")
        yield emit_result(snap.model_dump(mode="json"))
        yield emit_done()

    @staticmethod
    def _pending_to_sse(ev: dict[str, Any]) -> str | None:
        """节点 pending_events → SSE。未知 kind 丢掉，避免把内部字段推到前端。"""
        kind = ev.get("kind")
        if kind == "progress":
            status = ev.get("status")
            return emit_progress(
                str(ev.get("node", "")),
                str(ev.get("message", "")),
                status=status if status in ("running", "done", "skipped", "waiting") else None,
            )
        if kind == "tool_result":
            data = {k: v for k, v in ev.items() if k != "kind"}
            return emit_tool_result(data)
        return None

    @staticmethod
    def _extract_interrupts(graph_state: Any) -> list[dict[str, Any]]:
        """兼容 tasks[].interrupts 与顶层 interrupts 两种 LangGraph 形态。"""
        out: list[dict[str, Any]] = []
        tasks = getattr(graph_state, "tasks", None) or ()
        for task in tasks:
            for intr in getattr(task, "interrupts", None) or ():
                val = getattr(intr, "value", None)
                if isinstance(val, dict):
                    out.append(val)
                elif val is not None:
                    out.append({"message": str(val)})
        direct = getattr(graph_state, "interrupts", None) or ()
        for intr in direct:
            val = getattr(intr, "value", intr)
            if isinstance(val, dict):
                out.append(val)
        return out
