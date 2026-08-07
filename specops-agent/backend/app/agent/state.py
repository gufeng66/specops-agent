"""LangGraph agent state ↔ ThreadSnapshot mapping."""

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
    return right


class AgentState(TypedDict, total=False):
    thread_id: str
    run_id: str
    source_text: str
    meeting_brief: dict
    task_draft: dict
    validation: dict
    retrieve_results: list
    markdown_output: str
    status: str
    interrupt_id: str
    interrupt_payload: dict | None
    clarify_response: str | None
    review_action: dict | None
    repair_attempts: int
    pending_events: Annotated[list, _replace]
    sync_status: str
    review: dict
    graph_ns: str


def snapshot_to_state(snapshot: ThreadSnapshot, **overrides: Any) -> AgentState:
    state: AgentState = {
        "thread_id": snapshot.thread_id,
        "run_id": snapshot.run_id,
        "source_text": snapshot.source_text,
        "meeting_brief": snapshot.meeting_brief.model_dump(mode="json"),
        "task_draft": snapshot.task_draft.model_dump(mode="json"),
        "validation": snapshot.validation.model_dump(mode="json"),
        "retrieve_results": list(snapshot.retrieve_results),
        "markdown_output": snapshot.markdown_output,
        "status": snapshot.status.value,
        "interrupt_id": snapshot.interrupt_id,
        "interrupt_payload": None,
        "clarify_response": None,
        "review_action": None,
        "repair_attempts": snapshot.validation.repair_attempts,
        "pending_events": [],
        "sync_status": snapshot.sync_status or "not_synced",
        "review": snapshot.review.model_dump(mode="json"),
        "graph_ns": snapshot.graph_ns,
    }
    state.update(overrides)  # type: ignore[typeddict-item]
    return state


def state_to_snapshot(state: AgentState, base: ThreadSnapshot | None = None) -> ThreadSnapshot:
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
    snap.retrieve_results = list(state.get("retrieve_results") or [])
    snap.markdown_output = state.get("markdown_output") or snap.markdown_output
    status = state.get("status")
    if status:
        snap.status = ThreadStatus(status)
    snap.interrupt_id = state.get("interrupt_id") or ""
    snap.sync_status = state.get("sync_status") or "not_synced"
    snap.graph_ns = state.get("graph_ns") or snap.graph_ns
    if state.get("review") is not None:
        snap.review = ReviewInfo.model_validate(state["review"])
    return snap
