"""LangGraph 状态 ↔ ThreadSnapshot。

pending_events 用 _replace 归约：后写覆盖前写，避免节点输出被 append 拼脏。
clarify/review/assign 动作字段每次从快照还原时清空，防止上次 HITL 残留。
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from app.domain.models import (
    MeetingBrief,
    ReviewInfo,
    TaskDraft,
    ThreadSnapshot,
    ThreadStatus,
    ValidationResult,
)


def _replace(_left: Any, right: Any) -> Any:
    """LangGraph 通道归约：后写覆盖，避免 pending_events 被 append 拼脏。"""
    return right


class AgentState(TypedDict, total=False):
    """图状态。pending_events 用 _replace；HITL 动作字段每次进图清空。"""
    thread_id: str
    run_id: str
    source_text: str
    meeting_brief: dict
    task_draft: dict
    validation: dict
    markdown_output: str
    status: str
    interrupt_id: str
    interrupt_payload: dict | None
    clarify_response: str | None
    review_action: dict | None
    assign_action: dict | None
    repair_attempts: int
    assign_attempts: int
    pending_events: Annotated[list, _replace]
    review: dict
    graph_ns: str
    eval_mode: str
    eval_pre_enrich_stakeholders: Annotated[list, _replace]
    eval_scored_items: Annotated[list, _replace]
    prompt_version: str


def snapshot_to_state(snapshot: ThreadSnapshot, **overrides: Any) -> AgentState:
    """快照进图。HITL 动作字段故意清空，防止上次澄清/审核残留触发错误路由。"""
    state: AgentState = {
        "thread_id": snapshot.thread_id,
        "run_id": snapshot.run_id,
        "source_text": snapshot.source_text,
        "meeting_brief": snapshot.meeting_brief.model_dump(mode="json"),
        "task_draft": snapshot.task_draft.model_dump(mode="json"),
        "validation": snapshot.validation.model_dump(mode="json"),
        "markdown_output": snapshot.markdown_output,
        "status": snapshot.status.value,
        "interrupt_id": snapshot.interrupt_id,
        "interrupt_payload": snapshot.interrupt_payload,
        "clarify_response": None,
        "review_action": None,
        "assign_action": None,
        "repair_attempts": snapshot.validation.repair_attempts,
        "assign_attempts": 0,
        "pending_events": [],
        "review": snapshot.review.model_dump(mode="json"),
        "graph_ns": snapshot.graph_ns,
    }
    state.update(overrides)  # type: ignore[typeddict-item]
    return state


def state_to_snapshot(state: AgentState, base: ThreadSnapshot | None = None) -> ThreadSnapshot:
    """图画完写回快照。base 保留 created_at 等图状态里没有的字段。"""
    thread_id = state.get("thread_id") or (base.thread_id if base else "")
    snap = base.model_copy(deep=True) if base else ThreadSnapshot(thread_id=thread_id)
    snap.thread_id = thread_id
    snap.run_id = state.get("run_id") or snap.run_id
    snap.source_text = state.get("source_text") or snap.source_text
    if state.get("meeting_brief") is not None:
        snap.meeting_brief = MeetingBrief.model_validate(state["meeting_brief"])
    if state.get("task_draft") is not None:
        snap.task_draft = TaskDraft.model_validate(state["task_draft"])
    if state.get("validation") is not None:
        snap.validation = ValidationResult.model_validate(state["validation"])
    snap.markdown_output = state.get("markdown_output") or snap.markdown_output
    status = state.get("status")
    if status:
        snap.status = ThreadStatus(status)
    snap.interrupt_id = state.get("interrupt_id") or ""
    if "interrupt_payload" in state:
        snap.interrupt_payload = state.get("interrupt_payload")
    snap.graph_ns = state.get("graph_ns") or snap.graph_ns
    snap.prompt_version = state.get("prompt_version") or snap.prompt_version
    if state.get("review") is not None:
        snap.review = ReviewInfo.model_validate(state["review"])
    return snap
