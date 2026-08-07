"""LangGraph node implementations: LLM nodes + tool nodes + HITL interrupts."""

from __future__ import annotations

import logging
from typing import Any, Literal
from uuid import uuid4

from langgraph.types import interrupt

from app.agent.state import AgentState
from app.agent.tools import (
    export_markdown_tool,
    repair_task_draft_tool,
    search_playbooks,
    validate_task_draft_tool,
)
from app.domain.extractors import (
    needs_clarification,
    patch_target_item,
    rule_decompose,
    rule_extract_brief,
)
from app.domain.models import MeetingBrief, TaskDraft, ThreadStatus, ValidationResult
from app.llm.client import llm_client

logger = logging.getLogger(__name__)

MAX_REPAIR = 2

_ROUTE_LABELS = {
    "rules": "规则回退",
    "deepseek": "DeepSeek",
    "deepseek+rules": "DeepSeek + 规则",
}


def _route_label(used: str) -> str:
    return _ROUTE_LABELS.get(used, used)


def _events(*items: dict[str, Any]) -> list[dict[str, Any]]:
    return list(items)


def _progress(node: str, message: str) -> dict[str, Any]:
    return {"kind": "progress", "node": node, "message": message}


def _tool_result(data: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "tool_result", **data}


async def intake_node(state: AgentState) -> dict[str, Any]:
    source = state.get("source_text") or ""
    events = _events(_progress("intake", "解析会议输入 (DeepSeek / 规则回退)"))

    brief = rule_extract_brief(source)
    used = "rules"
    if llm_client.enabled:
        llm_brief = await llm_client.structured_extract_brief(source)
        if llm_brief and llm_brief.brief.goal:
            brief = llm_brief.brief
            used = "deepseek"
        elif llm_brief and not brief.goal and llm_brief.brief.model_dump():
            # Prefer LLM fields when rule goal empty
            merged = brief.model_dump()
            for k, v in llm_brief.brief.model_dump().items():
                if v:
                    merged[k] = v
            brief = MeetingBrief.model_validate(merged)
            used = "deepseek+rules"

    clarify = state.get("clarify_response")
    if clarify and not brief.goal:
        brief.goal = clarify

    events.append(_progress("intake", f"会议摘要完成 · {_route_label(used)}"))
    return {
        "meeting_brief": brief.model_dump(mode="json"),
        "status": ThreadStatus.PLANNING.value,
        "pending_events": events,
        "clarify_response": None,
    }


def route_after_intake(state: AgentState) -> Literal["clarify", "decompose"]:
    brief = MeetingBrief.model_validate(state.get("meeting_brief") or {})
    if needs_clarification(brief):
        return "clarify"
    return "decompose"


async def clarify_node(state: AgentState) -> dict[str, Any]:
    interrupt_id = state.get("interrupt_id") or str(uuid4())
    payload = {
        "type": "clarify",
        "interrupt_id": interrupt_id,
        "message": "缺少明确的会议目标，请补充",
        "fields": ["goal"],
    }
    # HITL pause — resume value is the clarify text or {"clarify_response": "..."}
    raw = interrupt(payload)
    if isinstance(raw, dict):
        text = str(raw.get("clarify_response") or raw.get("feedback") or raw.get("goal") or "")
    else:
        text = str(raw or "")

    brief = MeetingBrief.model_validate(state.get("meeting_brief") or {})
    if text.strip():
        brief.goal = text.strip()

    return {
        "meeting_brief": brief.model_dump(mode="json"),
        "interrupt_id": interrupt_id,
        "interrupt_payload": None,
        "status": ThreadStatus.PLANNING.value,
        "pending_events": _events(_progress("clarify", "已收到澄清，继续拆解")),
    }


async def decompose_node(state: AgentState) -> dict[str, Any]:
    source = state.get("source_text") or ""
    brief = MeetingBrief.model_validate(state.get("meeting_brief") or {})
    events = _events(_progress("decompose", "分解行动项 (DeepSeek / 规则回退)"))

    draft = rule_decompose(source, brief)
    used = "rules"
    if llm_client.enabled:
        llm_items = await llm_client.structured_decompose(source, brief.model_dump_json())
        if llm_items and llm_items.items:
            draft = TaskDraft(items=llm_items.items, version=draft.version)
            used = "deepseek"

    events.append(_progress("decompose", f"得到 {len(draft.items)} 项 · {_route_label(used)}"))
    return {
        "task_draft": draft.model_dump(mode="json"),
        "pending_events": events,
    }


async def retrieve_playbooks_node(state: AgentState) -> dict[str, Any]:
    brief = MeetingBrief.model_validate(state.get("meeting_brief") or {})
    source = state.get("source_text") or ""
    query = brief.goal or source[:100]
    events = _events(_progress("retrieve_playbooks", "Playbook Lookup"))

    results = search_playbooks(query)
    events.append(
        _tool_result({"tool": "search_playbooks", "query": query, "results": results})
    )
    return {
        "retrieve_results": results,
        "pending_events": events,
    }


async def validate_repair_node(state: AgentState) -> dict[str, Any]:
    source = state.get("source_text") or ""
    brief = MeetingBrief.model_validate(state.get("meeting_brief") or {})
    draft = TaskDraft.model_validate(state.get("task_draft") or {"items": []})
    events: list[dict[str, Any]] = []
    validation = ValidationResult()

    for attempt in range(MAX_REPAIR + 1):
        events.append(_progress("validate_repair", f"验证任务草案 (attempt {attempt})"))
        validation = validate_task_draft_tool(draft, brief, source)
        validation.repair_attempts = attempt
        events.append(
            _tool_result(
                {
                    "tool": "validate_task_draft",
                    "valid": validation.valid,
                    "error_count": len(validation.error_issues),
                    "advisory_count": len(validation.advisory_issues),
                }
            )
        )
        if not validation.has_blocking_errors:
            break
        if attempt >= MAX_REPAIR:
            break

        events.append(_progress("validate_repair", f"规则修复 (第 {attempt + 1} 次)"))
        draft = repair_task_draft_tool(draft, validation, source, brief)
        events.append(
            _tool_result(
                {
                    "tool": "repair_task_draft",
                    "attempt": attempt + 1,
                    "item_count": len(draft.items),
                }
            )
        )

    return {
        "task_draft": draft.model_dump(mode="json"),
        "validation": validation.model_dump(mode="json"),
        "repair_attempts": validation.repair_attempts,
        "pending_events": events,
    }


async def review_node(state: AgentState) -> dict[str, Any]:
    interrupt_id = str(uuid4())
    validation = ValidationResult.model_validate(state.get("validation") or {})
    draft = TaskDraft.model_validate(state.get("task_draft") or {"items": []})
    payload = {
        "type": "review",
        "interrupt_id": interrupt_id,
        "message": "请审核任务清单",
        "task_count": len(draft.items),
        "unresolved_errors": [i.model_dump(mode="json") for i in validation.error_issues],
        "advisory_issues": [i.model_dump(mode="json") for i in validation.advisory_issues],
    }
    action = interrupt(payload)
    if not isinstance(action, dict):
        action = {"approve": bool(action), "revision_scope": "item", "feedback": "", "target_item_id": None}

    approve = bool(action.get("approve"))
    revision_scope = action.get("revision_scope") or "item"
    feedback = str(action.get("feedback") or "")
    target_item_id = action.get("target_item_id")

    events = _events(_progress("review", "处理人工审核结果"))
    review = {
        "feedback": feedback,
        "revision_scope": revision_scope,
        "target_item_id": target_item_id,
        "approved": approve,
    }

    if approve:
        return {
            "interrupt_id": interrupt_id,
            "review_action": {"route": "finalize"},
            "review": review,
            "status": ThreadStatus.PLANNING.value,
            "pending_events": events,
        }

    if revision_scope == "document":
        source = (state.get("source_text") or "").rstrip()
        source = f"{source}\n\n[文档修订] {feedback}".strip()
        return {
            "source_text": source,
            "interrupt_id": interrupt_id,
            "review_action": {"route": "intake"},
            "review": review,
            "status": ThreadStatus.PLANNING.value,
            "pending_events": events + _events(_progress("review", "文档修订，重跑 intake")),
        }

    # item scope
    if not target_item_id:
        events.append(_progress("review", "缺少 target_item_id，重新进入审核"))
        return {
            "interrupt_id": interrupt_id,
            "review_action": {"route": "review"},
            "review": review,
            "pending_events": events,
        }

    patched = patch_target_item(draft, str(target_item_id), feedback)
    return {
        "task_draft": patched.model_dump(mode="json"),
        "interrupt_id": interrupt_id,
        "review_action": {"route": "validate_repair"},
        "review": review,
        "status": ThreadStatus.PLANNING.value,
        "pending_events": events
        + _events(_progress("review", "已应用单项修订，重新验证")),
    }


def route_after_review(state: AgentState) -> Literal["finalize", "intake", "validate_repair", "review"]:
    action = state.get("review_action") or {}
    route = action.get("route") or "finalize"
    if route in ("finalize", "intake", "validate_repair", "review"):
        return route  # type: ignore[return-value]
    return "finalize"


async def finalize_node(state: AgentState) -> dict[str, Any]:
    brief = MeetingBrief.model_validate(state.get("meeting_brief") or {})
    draft = TaskDraft.model_validate(state.get("task_draft") or {"items": []})
    validation = ValidationResult.model_validate(state.get("validation") or {})
    source = state.get("source_text") or ""
    events = _events(_progress("finalize", "生成导出"))

    md = export_markdown_tool(brief, draft, validation, source)
    events.append(_tool_result({"tool": "export_markdown", "chars": len(md)}))

    review = dict(state.get("review") or {})
    review["approved"] = True

    return {
        "markdown_output": md,
        "status": ThreadStatus.COMPLETED.value,
        "sync_status": "not_synced",
        "review": review,
        "review_action": None,
        "pending_events": events,
    }
