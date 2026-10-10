"""HTTP 运行时：把图的 updates 编成 SSE，并把快照写入 SQLite。

clarify 走 /chat/stream + clarify_response（同一 interrupt 续跑）；
分配/审核走 /runs/{id}/resume。graph_ns 必须跨 HITL 稳定，否则 checkpoint 对不上。
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator
from uuid import uuid4

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
from app.memory.store import MemoryStore
from app.obs import bind_request, flush_metrics

logger = logging.getLogger(__name__)


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
    ) -> AsyncIterator[str]:
        """开跑或澄清续跑。澄清用 Command(resume)，graph_ns 沿用，不新开 checkpoint。

        eval_mode 只给评测。不传时节点保持原行为。
        """
        run_id = str(uuid4())
        snapshot.run_id = run_id
        snapshot.source_text = source_text
        snapshot.status = ThreadStatus.PLANNING
        snapshot.prompt_version = PROMPT_VERSION

        resuming_clarify = bool(clarify_response and snapshot.graph_ns and snapshot.interrupt_id)
        if not resuming_clarify:
            snapshot.graph_ns = f"{snapshot.thread_id}:{run_id}"

        with bind_request(request_id, run_id):
            logger.info("chat start")
            await self.store.save_thread(snapshot)

            yield emit_metadata(
                {"run_id": run_id, "thread_id": snapshot.thread_id, "request_id": request_id}
            )

            try:
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
                    ):
                        yield event
                    return

                initial = snapshot_to_state(
                    snapshot,
                    source_text=source_text,
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
                if eval_mode:
                    initial["eval_mode"] = eval_mode
                async for event in self._stream_graph(
                    snapshot, initial, request_id=request_id, run_id=run_id
                ):
                    yield event
            except Exception as exc:
                logger.exception("run_chat failed")
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
    ) -> AsyncIterator[str]:
        """分配/审核续跑。assignments 只在人确认后进入图，建议字段不会从这里写入主责。"""
        run_id = str(uuid4())
        snapshot.run_id = run_id
        snapshot.status = ThreadStatus.PLANNING
        if not snapshot.graph_ns:
            snapshot.graph_ns = f"{snapshot.thread_id}:{run_id}"
        with bind_request(request_id, run_id):
            logger.info("resume start")
            await self.store.save_thread(snapshot)

            yield emit_metadata(
                {"run_id": run_id, "thread_id": snapshot.thread_id, "request_id": request_id}
            )

            try:
                payload: dict[str, Any] = {
                    "approve": approve,
                    "revision_scope": revision_scope,
                    "target_item_id": target_item_id,
                    "feedback": feedback,
                    "assignments": assignments or [],
                    "drop_item_ids": drop_item_ids or [],
                }
                async for event in self._stream_graph(
                    snapshot,
                    Command(resume=payload),
                    request_id=request_id,
                    run_id=run_id,
                ):
                    yield event
            except Exception as exc:
                logger.exception("resume failed")
                yield emit_error(str(exc))
                yield emit_done()

    async def _stream_graph(
        self,
        snapshot: ThreadSnapshot,
        input_data: Any,
        *,
        request_id: str,
        run_id: str,
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
            await self.store.save_thread(snap)
            await self.store.complete_request(request_id, snap.thread_id, run_id, snap)
            hitl_node = str(payload.get("type") or "review")
            yield emit_progress(hitl_node, str(payload.get("message") or "等待人工操作"), status="waiting")
            yield emit_interrupt(payload)
            yield emit_result(snap.model_dump(mode="json"))
            yield emit_done()
            return

        snap = state_to_snapshot(values, snapshot)
        snap.graph_ns = ns
        flush_metrics(snap)
        if snap.status != ThreadStatus.COMPLETED and snap.markdown_output:
            snap.status = ThreadStatus.COMPLETED
        await self.store.save_thread(snap)
        await self.store.complete_request(request_id, snap.thread_id, run_id, snap)
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
