"""LangGraph 节点：模型抽取、工具校验、HITL interrupt。

SSE 约定：astream(updates) 只在节点结束后 flush，所以不在节点开头发 running。
非 HITL 节点结尾发自己 done + 下一节点 running + 未走分支 skipped。
HITL 不发自己 done；waiting 由 workflow 检测到 interrupt 后覆盖。

建议 ≠ 写入：propose_owners 只填 owner_suggestions；主责/次责只在 HITL 点选后写入。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal
from uuid import uuid4

from langgraph.types import interrupt

from app.agent.prompts import TOOL_LOOP_SYSTEM
from app.agent.state import AgentState
from app.agent.tools import (
    drop_items_tool,
    export_markdown_tool,
    repair_task_draft_tool,
    validate_task_draft_tool,
)
from app.config import settings
from app.domain.chunking import (
    enrich_brief_from_items,
    evidence_windows,
    merge_drafts,
    split_source,
)
from app.domain.extractors import (
    apply_owner_assignments,
    needs_clarification,
    patch_target_item,
    rule_decompose,
    rule_extract_brief,
)
from app.domain.models import MeetingBrief, TaskDraft, ThreadStatus, ValidationResult
from app.domain.review_intent import parse_drop_intent
from app.domain.validation import OWNER_ERROR_CODES, has_owner_errors, sanitize_owner_candidates
from app.llm.client import llm_client
from app.llm.errors import LLMError

logger = logging.getLogger(__name__)

MAX_REPAIR = 2
MAX_ASSIGN = 3

_ROUTE_LABELS = {
    "rules": "规则回退",
    "deepseek": "DeepSeek",
    "deepseek+rules": "DeepSeek + 规则",
}


def _route_label(used: str) -> str:
    return _ROUTE_LABELS.get(used, used)


def _events(*items: dict[str, Any]) -> list[dict[str, Any]]:
    return list(items)


def _progress(node: str, message: str, status: str | None = None) -> dict[str, Any]:
    """节点内部进度事件。status 缺省时前端只记时间线。"""
    ev: dict[str, Any] = {"kind": "progress", "node": node, "message": message}
    if status is not None:
        ev["status"] = status
    return ev


def _tool_result(data: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "tool_result", **data}


def _eval_mode(state: AgentState) -> str:
    """评测档位。空字符串表示线上路径。"""
    return str(state.get("eval_mode") or "")


async def intake_node(state: AgentState) -> dict[str, Any]:
    """规则底稿 + 可选 LLM merge。goal 空则下一跳 clarify。"""
    source = state.get("source_text") or ""
    events: list[dict[str, Any]] = []
    chunks = split_source(source, settings.llm_chunk_chars, settings.llm_chunk_overlap)

    mode = _eval_mode(state)
    if mode == "llm_only":
        brief = MeetingBrief()
        used = "deepseek"
        if llm_client.enabled:
            llm_brief = await llm_client.structured_extract_brief(source)
            if llm_brief is not None:
                brief = llm_brief.brief
    elif mode == "rules":
        brief = rule_extract_brief(source)
        used = "rules"
    else:
        brief = rule_extract_brief(source)
        used = "rules"
        if llm_client.enabled:
            llm_brief = await llm_client.structured_extract_brief(source)
            if llm_brief:
                if llm_brief.brief.goal:
                    brief.goal = llm_brief.brief.goal
                for name in llm_brief.brief.stakeholders:
                    if name and name not in brief.stakeholders:
                        brief.stakeholders.append(name)
                if llm_brief.brief.meeting_date and not brief.meeting_date:
                    brief.meeting_date = llm_brief.brief.meeting_date
                if llm_brief.brief.context:
                    brief.context = llm_brief.brief.context
                used = "deepseek+rules"

    clarify = state.get("clarify_response")
    if clarify and not brief.goal:
        brief.goal = clarify

    next_route = "clarify" if needs_clarification(brief) else "decompose"
    note = f"会议摘要完成 · {_route_label(used)} · 原文 {len(source)} 字"
    if len(source) > settings.source_soft_limit_chars or len(chunks) > 1:
        note += f" · 将分 {len(chunks)} 块"
    events.append(_progress("intake", note, "done"))
    if next_route == "decompose":
        events.append(_progress("clarify", "已有会议目标", "skipped"))
        events.append(_progress("decompose", "分解行动项", "running"))
    else:
        events.append(_progress("clarify", "缺少会议目标，等待澄清", "running"))

    return {
        "meeting_brief": brief.model_dump(mode="json"),
        "status": ThreadStatus.PLANNING.value,
        "pending_events": events,
        "clarify_response": None,
        "assign_attempts": 0,
    }


def route_after_intake(state: AgentState) -> Literal["clarify", "decompose"]:
    """goal 空 → 澄清；否则直接拆解。"""
    brief = MeetingBrief.model_validate(state.get("meeting_brief") or {})
    if needs_clarification(brief):
        return "clarify"
    return "decompose"


async def clarify_node(state: AgentState) -> dict[str, Any]:
    """HITL：补会议目标。interrupt 后把用户文本写入 brief.goal。"""
    interrupt_id = state.get("interrupt_id") or str(uuid4())
    payload = {
        "type": "clarify",
        "interrupt_id": interrupt_id,
        "message": "缺少明确的会议目标，请补充",
        "fields": ["goal"],
    }
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
        "pending_events": _events(
            _progress("clarify", "已收到澄清，继续拆解", "done"),
            _progress("decompose", "分解行动项", "running"),
        ),
    }


async def _llm_decompose(
    chunks: list[str],
    brief: MeetingBrief,
    source: str,
    base: TaskDraft | None = None,
) -> tuple[TaskDraft, bool]:
    """分块抽取。base 为空时不并入规则底稿。"""
    brief_json = brief.model_dump_json()
    limit = max(1, settings.llm_max_concurrency)
    sem = asyncio.Semaphore(limit)

    async def _one(chunk: str):
        async with sem:
            return await llm_client.structured_decompose(chunk, brief_json)

    results = await asyncio.gather(*[_one(chunk) for chunk in chunks], return_exceptions=True)
    llm_items = []
    any_ok = False
    for result in results:
        if isinstance(result, Exception):
            logger.warning("chunk decompose failed: %s", result)
            continue
        if result and result.items:
            any_ok = True
            llm_items.extend(result.items)
    if not any_ok:
        return (base if base is not None else TaskDraft()), False
    return merge_drafts(base if base is not None else TaskDraft(), llm_items, source), True


async def decompose_node(state: AgentState) -> dict[str, Any]:
    """规则扫全文 + LLM 分块并发抽取，再 merge。强制清空 owner_suggestions。"""
    source = state.get("source_text") or ""
    brief = MeetingBrief.model_validate(state.get("meeting_brief") or {})
    chunks = split_source(source, settings.llm_chunk_chars, settings.llm_chunk_overlap)

    mode = _eval_mode(state)
    pre_stakeholders = list(brief.stakeholders)
    if mode == "rules":
        draft = rule_decompose(source, brief)
        used = "rules"
    elif mode == "llm_only":
        draft = TaskDraft()
        used = "deepseek"
        if llm_client.enabled:
            draft, any_ok = await _llm_decompose(chunks, brief, source)
            if not any_ok:
                draft = TaskDraft()
    else:
        draft = rule_decompose(source, brief)
        used = "rules"
        if llm_client.enabled:
            draft, any_ok = await _llm_decompose(chunks, brief, source, base=draft)
            if any_ok:
                used = "deepseek+rules"

    for item in draft.items:
        item.owner_suggestions = []  # 建议只允许 propose_owners 产出

    brief = enrich_brief_from_items(brief, draft.items)
    note = f"得到 {len(draft.items)} 项 · {_route_label(used)}"
    if len(chunks) > 1:
        note += f" · 已处理 {len(chunks)} 块"

    return {
        "task_draft": draft.model_dump(mode="json"),
        "meeting_brief": brief.model_dump(mode="json"),
        "eval_pre_enrich_stakeholders": pre_stakeholders,
        "pending_events": _events(
            _progress("decompose", note, "done"),
            _progress("validate_repair", "验证任务草案", "running"),
        ),
    }


async def validate_repair_node(state: AgentState) -> dict[str, Any]:
    """修标题/验收/证据/假任务，不修负责人。LLM 或 owner ERROR 才进 propose_owners。"""
    source = state.get("source_text") or ""
    brief = MeetingBrief.model_validate(state.get("meeting_brief") or {})
    draft = TaskDraft.model_validate(state.get("task_draft") or {"items": []})
    events: list[dict[str, Any]] = []

    mode = _eval_mode(state)
    if mode in {"rules", "llm", "llm_only"} or not llm_client.enabled:
        draft, validation, events = _rule_validate_repair(draft, brief, source)
    else:
        draft, validation, events = await _llm_tool_validate_repair(draft, brief, source)

    use_llm = llm_client.enabled and mode != "rules"
    go_propose = use_llm or has_owner_errors(validation)
    events.append(_progress("validate_repair", "验证完成", "done"))
    if go_propose:
        events.append(_progress("propose_owners", "生成负责人建议", "running"))
    else:
        events.append(_progress("propose_owners", "无需分配建议", "skipped"))
        events.append(_progress("assign_owners", "负责人已确认", "skipped"))
        events.append(_progress("review", "请审核任务清单", "running"))

    return {
        "task_draft": draft.model_dump(mode="json"),
        "validation": validation.model_dump(mode="json"),
        "repair_attempts": validation.repair_attempts,
        "eval_scored_items": [item.model_dump(mode="json") for item in draft.items],
        "pending_events": events,
    }


def _rule_validate_repair(
    draft: TaskDraft,
    brief: MeetingBrief,
    source: str,
) -> tuple[TaskDraft, ValidationResult, list[dict[str, Any]]]:
    """无 Key：规则修复最多 MAX_REPAIR 轮，从不 drop 条目（假阳性留给审核删除）。"""
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
        repairable = [i for i in validation.error_issues if i.code not in OWNER_ERROR_CODES]
        if not repairable:
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

    repair_attempts = validation.repair_attempts
    validation = validate_task_draft_tool(draft, brief, source)
    validation.repair_attempts = repair_attempts
    return draft, validation, events


def _draft_loop_summary(draft: TaskDraft, validation: ValidationResult) -> str:
    """工具循环 user 消息：只给条目摘要和问题码，避免整篇原文重复进 prompt。"""
    items = []
    for item in draft.items:
        items.append(
            {
                "id": item.id,
                "title": item.title,
                "primary_owner": item.primary_owner,
                "secondary_owners": item.secondary_owners,
                "unresolved_ref": item.unresolved_ref,
                "evidence_quote": (item.evidence_quote or "")[:80],
            }
        )
    return json.dumps(
        {
            "items": items,
            "issues": [i.model_dump(mode="json") for i in validation.issues],
        },
        ensure_ascii=False,
    )


def _execute_loop_tool(
    name: str,
    arguments: dict[str, Any],
    draft: TaskDraft,
    brief: MeetingBrief,
    source: str,
) -> tuple[TaskDraft, dict[str, Any]]:
    """执行模型点名的确定性工具。未知名字抛错，让循环记一次失败。"""
    if name == "validate_task_draft":
        validation = validate_task_draft_tool(draft, brief, source)
        payload = {
            "tool": name,
            "valid": validation.valid,
            "error_count": len(validation.error_issues),
            "codes": [i.code for i in validation.error_issues],
        }
        return draft, payload
    if name == "repair_task_draft":
        validation = validate_task_draft_tool(draft, brief, source)
        draft = repair_task_draft_tool(draft, validation, source, brief)
        payload = {"tool": name, "item_count": len(draft.items)}
        return draft, payload
    if name == "drop_items":
        raw_ids = arguments.get("item_ids") or []
        if not isinstance(raw_ids, list):
            raw_ids = [raw_ids]
        item_ids = [str(i) for i in raw_ids]
        draft = drop_items_tool(draft, item_ids)
        payload = {"tool": name, "dropped": item_ids, "item_count": len(draft.items)}
        return draft, payload
    raise ValueError(f"unknown tool {name}")


async def _llm_tool_validate_repair(
    draft: TaskDraft,
    brief: MeetingBrief,
    source: str,
) -> tuple[TaskDraft, ValidationResult, list[dict[str, Any]]]:
    """有 Key：模型调度 validate / repair / drop，有轮次、失败、stall 上限。"""
    events: list[dict[str, Any]] = []
    validation = validate_task_draft_tool(draft, brief, source)
    events.append(_progress("validate_repair", "LLM 工具循环开始", "running"))
    events.append(
        _tool_result(
            {
                "tool": "validate_task_draft",
                "valid": validation.valid,
                "error_count": len(validation.error_issues),
            }
        )
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": TOOL_LOOP_SYSTEM},
        {
            "role": "user",
            "content": (
                f"会议目标: {brief.goal}\n干系人: {', '.join(brief.stakeholders)}\n"
                f"原文摘录:\n{evidence_windows(source, draft)}\n\n"
                f"当前草案与问题:\n{_draft_loop_summary(draft, validation)}"
            ),
        },
    ]

    max_rounds = settings.max_tool_rounds
    max_fail = settings.max_consecutive_tool_failures
    max_stall = settings.max_tool_stall
    consecutive_failures = 0
    stall = 0
    rounds = 0

    while rounds < max_rounds:
        rounds += 1
        try:
            assistant = await llm_client.chat_tools(messages=messages, node="validate_repair")
        except LLMError as exc:
            logger.warning("tool loop LLM failed [%s]: %s", exc.code, exc.message)
            consecutive_failures += 1
            events.append(_progress("validate_repair", f"模型调用失败 ({consecutive_failures}/{max_fail})"))
            if consecutive_failures >= max_fail:
                break
            continue

        if "role" not in assistant:
            assistant = {**assistant, "role": "assistant"}
        tool_calls = assistant.get("tool_calls") or []
        messages.append(assistant)
        if not tool_calls:
            events.append(_progress("validate_repair", "模型未再调用工具"))
            break

        prev_errors = len(validation.error_issues)
        executed = 0
        for call in tool_calls:
            fn = call.get("function") or {}
            name = str(fn.get("name") or "")
            raw_args = fn.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except (TypeError, json.JSONDecodeError):
                arguments = {}
            call_id = str(call.get("id") or name)
            try:
                draft, payload = _execute_loop_tool(name, arguments, draft, brief, source)
                consecutive_failures = 0
                executed += 1
                events.append(_progress("validate_repair", f"调用 {name}"))
                events.append(_tool_result(payload))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(payload, ensure_ascii=False),
                    }
                )
            except Exception as exc:
                consecutive_failures += 1
                logger.warning("tool %s failed: %s", name, exc)
                events.append(_progress("validate_repair", f"{name} 执行失败"))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps({"error": str(exc)}, ensure_ascii=False),
                    }
                )
                if consecutive_failures >= max_fail:
                    break

        validation = validate_task_draft_tool(draft, brief, source)
        if len(validation.error_issues) >= prev_errors:
            stall += 1
            events.append(_progress("validate_repair", f"校验无进展 stall={stall}/{max_stall}"))
            if stall >= max_stall:
                break
        else:
            stall = 0

        if consecutive_failures >= max_fail:
            break
        if executed == 0:
            break

        messages.append(
            {
                "role": "user",
                "content": f"更新后的草案与问题:\n{_draft_loop_summary(draft, validation)}",
            }
        )

    validation = validate_task_draft_tool(draft, brief, source)
    validation.repair_attempts = rounds
    events.append(
        _tool_result(
            {
                "tool": "validate_task_draft",
                "valid": validation.valid,
                "error_count": len(validation.error_issues),
                "final": True,
            }
        )
    )
    return draft, validation, events


def route_after_validate_repair(state: AgentState) -> Literal["propose_owners", "review"]:
    """有 Key 或负责人类 ERROR → propose_owners；规则路径且无 ERROR 则直达 review。"""
    validation = ValidationResult.model_validate(state.get("validation") or {})
    if llm_client.enabled or has_owner_errors(validation):
        return "propose_owners"
    return "review"


def _owner_error_items(draft: TaskDraft, validation: ValidationResult) -> list[dict[str, Any]]:
    """分配面板用的待办条目，带上已清洗的 owner_suggestions。"""
    by_id = {item.id: item for item in draft.items}
    pending: list[dict[str, Any]] = []
    seen: set[str] = set()
    for issue in validation.error_issues:
        if issue.code not in OWNER_ERROR_CODES or not issue.item_id:
            continue
        if issue.item_id in seen:
            item = next((p for p in pending if p["id"] == issue.item_id), None)
            if item:
                item["issues"].append(issue.model_dump(mode="json"))
            continue
        seen.add(issue.item_id)
        item = by_id.get(issue.item_id)
        pending.append(
            {
                "id": issue.item_id,
                "title": item.title if item else "",
                "description": item.description if item else "",
                "primary_owner": item.primary_owner if item else "",
                "secondary_owners": list(item.secondary_owners) if item else [],
                "evidence_quote": item.evidence_quote if item else "",
                "unresolved_ref": item.unresolved_ref if item else "",
                "owner_suggestions": (
                    [c.model_dump(mode="json") for c in item.owner_suggestions] if item else []
                ),
                "issues": [issue.model_dump(mode="json")],
            }
        )
    return pending


async def propose_owners_node(state: AgentState) -> dict[str, Any]:
    """生成建议，永不写入主责/次责。无 Key / 失败返回空列表。有 owner ERROR 才进分配。"""
    brief = MeetingBrief.model_validate(state.get("meeting_brief") or {})
    draft = TaskDraft.model_validate(state.get("task_draft") or {"items": []})
    source = state.get("source_text") or ""
    validation = ValidationResult.model_validate(state.get("validation") or {})

    suggestion_count = 0
    if llm_client.enabled and draft.items and _eval_mode(state) != "rules":
        pending_json = json.dumps(
            [
                {
                    "id": item.id,
                    "title": item.title,
                    "description": item.description,
                    "primary_owner": item.primary_owner,
                    "secondary_owners": list(item.secondary_owners),
                    "unresolved_ref": item.unresolved_ref,
                    "evidence_quote": item.evidence_quote,
                }
                for item in draft.items
            ],
            ensure_ascii=False,
        )
        try:
            batch = await llm_client.structured_propose_owners(
                brief_json=brief.model_dump_json(),
                pending_json=pending_json,
                windows=evidence_windows(source, draft),
            )
        except Exception as exc:
            logger.warning("propose_owners failed: %s — empty suggestions", exc)
            batch = None
        by_id: dict[str, list] = {}
        if batch:
            for row in batch.items:
                by_id[row.item_id] = list(row.suggestions)
        new_items = []
        for item in draft.items:
            patched = item.model_copy(deep=True)
            raw = by_id.get(item.id) or []
            patched.owner_suggestions = sanitize_owner_candidates(
                raw,
                source,
                brief.stakeholders,
                primary_owner=item.primary_owner,
                existing_secondary=list(item.secondary_owners),
            )
            suggestion_count += len(patched.owner_suggestions)
            new_items.append(patched)
        draft = TaskDraft(items=new_items, version=draft.version)

    go_assign = has_owner_errors(validation)
    note = f"负责人建议完成 · {suggestion_count} 条候选"
    if not llm_client.enabled:
        note = "负责人建议完成 · 规则路径无模型建议"
    events = [_progress("propose_owners", note, "done")]
    if go_assign:
        events.append(_progress("assign_owners", "需要人工分配负责人", "running"))
    else:
        events.append(_progress("assign_owners", "负责人已确认", "skipped"))
        events.append(_progress("review", "请审核任务清单", "running"))
    return {
        "task_draft": draft.model_dump(mode="json"),
        "pending_events": events,
    }


def route_after_propose(state: AgentState) -> Literal["assign_owners", "review"]:
    """仍有负责人类 ERROR → assign_owners；否则带建议进 review。"""
    validation = ValidationResult.model_validate(state.get("validation") or {})
    if has_owner_errors(validation):
        return "assign_owners"
    return "review"


async def assign_owners_node(state: AgentState) -> dict[str, Any]:
    """HITL 分配。最多 MAX_ASSIGN 次；仍非法则带着 ERROR 进 review，finalize 不再拦截。"""
    attempts = int(state.get("assign_attempts") or 0)
    brief = MeetingBrief.model_validate(state.get("meeting_brief") or {})
    draft = TaskDraft.model_validate(state.get("task_draft") or {"items": []})
    source = state.get("source_text") or ""
    validation = ValidationResult.model_validate(state.get("validation") or {})
    interrupt_id = str(uuid4())
    pending_items = _owner_error_items(draft, validation)
    payload = {
        "type": "assign_owners",
        "interrupt_id": interrupt_id,
        "message": "请为无法确认负责人的任务分配主责/次责",
        "stakeholders": list(brief.stakeholders),
        "items": pending_items,
        "attempt": attempts + 1,
        "max_attempts": MAX_ASSIGN,
    }
    raw = interrupt(payload)
    assignments: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        assignments = list(raw.get("assignments") or [])
    draft = apply_owner_assignments(draft, assignments)
    validation = validate_task_draft_tool(draft, brief, source)
    attempts += 1

    still_bad = has_owner_errors(validation)
    if still_bad and attempts < MAX_ASSIGN:
        return {
            "task_draft": draft.model_dump(mode="json"),
            "validation": validation.model_dump(mode="json"),
            "assign_attempts": attempts,
            "assign_action": {"route": "assign_owners"},
            "interrupt_id": interrupt_id,
            "interrupt_payload": None,
            "status": ThreadStatus.PLANNING.value,
            "pending_events": _events(
                _progress("assign_owners", f"分配仍不完整，请继续（{attempts}/{MAX_ASSIGN}）")
            ),
        }

    note = "负责人已确认" if not still_bad else f"已达 {MAX_ASSIGN} 次，携带未解决问题进入审核"
    return {
        "task_draft": draft.model_dump(mode="json"),
        "validation": validation.model_dump(mode="json"),
        "assign_attempts": attempts,
        "assign_action": {"route": "review"},
        "interrupt_id": interrupt_id,
        "interrupt_payload": None,
        "status": ThreadStatus.PLANNING.value,
        "pending_events": _events(
            _progress("assign_owners", note, "done"),
            _progress("review", "请审核任务清单", "running"),
        ),
    }


def route_after_assign(state: AgentState) -> Literal["assign_owners", "review"]:
    """分配仍不完整且未达上限 → 再 interrupt；否则进审核。"""
    action = state.get("assign_action") or {}
    route = action.get("route") or "review"
    if route == "assign_owners":
        return "assign_owners"
    return "review"


async def review_node(state: AgentState) -> dict[str, Any]:
    """HITL 审核：批准 / 单项修订 / 删除 / 全文重跑。删除走全局序号或 UUID。"""
    interrupt_id = str(uuid4())
    validation = ValidationResult.model_validate(state.get("validation") or {})
    draft = TaskDraft.model_validate(state.get("task_draft") or {"items": []})
    payload = {
        "type": "review",
        "interrupt_id": interrupt_id,
        "message": "请审核任务清单" if draft.items else "请审核任务清单（当前 0 条）",
        "task_count": len(draft.items),
        "stakeholders": list(
            MeetingBrief.model_validate(state.get("meeting_brief") or {}).stakeholders
        ),
        "items": [
            {
                "id": item.id,
                "index": idx,
                "title": item.title,
                "primary_owner": item.primary_owner,
                "secondary_owners": list(item.secondary_owners),
                "evidence_quote": item.evidence_quote,
                "unresolved_ref": item.unresolved_ref,
                "owner_suggestions": [
                    c.model_dump(mode="json") for c in item.owner_suggestions
                ],
            }
            for idx, item in enumerate(draft.items, 1)
        ],
        "unresolved_errors": [i.model_dump(mode="json") for i in validation.error_issues],
        "advisory_issues": [i.model_dump(mode="json") for i in validation.advisory_issues],
    }
    action = interrupt(payload)
    if not isinstance(action, dict):
        action = {
            "approve": bool(action),
            "revision_scope": "item",
            "feedback": "",
            "target_item_id": None,
        }

    approve = bool(action.get("approve"))
    revision_scope = action.get("revision_scope") or "item"
    feedback = str(action.get("feedback") or "")
    target_item_id = action.get("target_item_id")
    raw_drop_ids = action.get("drop_item_ids") or []
    if not isinstance(raw_drop_ids, list):
        raw_drop_ids = [raw_drop_ids]
    drop_item_ids = [str(i) for i in raw_drop_ids if str(i).strip()]

    review = {
        "feedback": feedback,
        "revision_scope": revision_scope,
        "target_item_id": target_item_id,
        "approved": approve,
        "dropped_item_ids": [],
    }

    if approve:
        assignments: list[dict[str, Any]] = []
        if isinstance(action, dict):
            assignments = list(action.get("assignments") or [])
        if assignments:
            draft = apply_owner_assignments(draft, assignments)
            brief = MeetingBrief.model_validate(state.get("meeting_brief") or {})
            source = state.get("source_text") or ""
            validation = validate_task_draft_tool(draft, brief, source)
        return {
            "task_draft": draft.model_dump(mode="json"),
            "validation": validation.model_dump(mode="json"),
            "interrupt_id": interrupt_id,
            "interrupt_payload": None,
            "review_action": {"route": "finalize"},
            "review": review,
            "status": ThreadStatus.PLANNING.value,
            "pending_events": _events(
                _progress("review", "已批准", "done"),
                _progress("finalize", "生成导出", "running"),
            ),
        }

    parsed = parse_drop_intent(
        feedback=feedback,
        draft=draft,
        revision_scope=str(revision_scope),
        target_item_id=str(target_item_id) if target_item_id else None,
        drop_item_ids=drop_item_ids,
    )
    if parsed.matched:
        if parsed.error or not parsed.item_ids:
            review["revision_scope"] = "drop"
            return {
                "interrupt_id": interrupt_id,
                "interrupt_payload": None,
                "review_action": {"route": "review"},
                "review": review,
                "status": ThreadStatus.PLANNING.value,
                "pending_events": _events(
                    _progress("review", parsed.error or "无法解析要删除的条目，重新进入审核")
                ),
            }
        brief = MeetingBrief.model_validate(state.get("meeting_brief") or {})
        source = state.get("source_text") or ""
        dropped = drop_items_tool(draft, parsed.item_ids)
        validation = validate_task_draft_tool(dropped, brief, source)
        review["revision_scope"] = "drop"
        review["dropped_item_ids"] = parsed.item_ids
        return {
            "task_draft": dropped.model_dump(mode="json"),
            "validation": validation.model_dump(mode="json"),
            "interrupt_id": interrupt_id,
            "interrupt_payload": None,
            "review_action": {
                "route": "review",
                "dropped_ids": parsed.item_ids,
                "feedback": feedback,
            },
            "review": review,
            "status": ThreadStatus.PLANNING.value,
            "pending_events": _events(
                _progress("review", f"已删除 {len(parsed.item_ids)} 条，重新进入审核")
            ),
        }

    if revision_scope == "document":
        source = (state.get("source_text") or "").rstrip()
        source = f"{source}\n\n[文档修订] {feedback}".strip()
        return {
            "source_text": source,
            "interrupt_id": interrupt_id,
            "interrupt_payload": None,
            "review_action": {"route": "intake"},
            "review": review,
            "status": ThreadStatus.PLANNING.value,
            "assign_attempts": 0,
            "pending_events": _events(
                _progress("review", "文档修订，重跑 intake", "done"),
                _progress("intake", "重新解析会议输入", "running"),
            ),
        }

    if not target_item_id:
        return {
            "interrupt_id": interrupt_id,
            "interrupt_payload": None,
            "review_action": {"route": "review"},
            "review": review,
            "status": ThreadStatus.PLANNING.value,
            "pending_events": _events(_progress("review", "缺少 target_item_id，重新进入审核")),
        }

    patched = patch_target_item(draft, str(target_item_id), feedback)
    return {
        "task_draft": patched.model_dump(mode="json"),
        "interrupt_id": interrupt_id,
        "interrupt_payload": None,
        "review_action": {"route": "validate_repair"},
        "review": review,
        "status": ThreadStatus.PLANNING.value,
        "assign_attempts": 0,
        "pending_events": _events(
            _progress("review", "已应用单项修订，重新验证", "done"),
            _progress("validate_repair", "验证任务草案", "running"),
        ),
    }


def route_after_review(
    state: AgentState,
) -> Literal["finalize", "intake", "validate_repair", "review"]:
    """批准→finalize；全文重跑→intake；单项修订→validate_repair；删除后再审→review。"""
    action = state.get("review_action") or {}
    route = action.get("route") or "finalize"
    if route in ("finalize", "intake", "validate_repair", "review"):
        return route  # type: ignore[return-value]
    return "finalize"


async def finalize_node(state: AgentState) -> dict[str, Any]:
    """生成 Markdown。人工已在 review 拍板，此处不再因 owner ERROR 拦截。"""
    brief = MeetingBrief.model_validate(state.get("meeting_brief") or {})
    draft = TaskDraft.model_validate(state.get("task_draft") or {"items": []})
    validation = ValidationResult.model_validate(state.get("validation") or {})
    source = state.get("source_text") or ""

    md = export_markdown_tool(brief, draft, validation, source)
    review = dict(state.get("review") or {})
    review["approved"] = True

    return {
        "markdown_output": md,
        "status": ThreadStatus.COMPLETED.value,
        "review": review,
        "review_action": None,
        "pending_events": _events(
            _tool_result({"tool": "export_markdown", "chars": len(md)}),
            _progress("finalize", "导出完成", "done"),
        ),
    }
