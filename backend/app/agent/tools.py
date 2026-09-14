"""确定性工具：校验、规则修复、删条、导出。模型只能调这些，不能直接改 JSON 猜人。"""

from __future__ import annotations

from app.domain.extractors import render_markdown
from app.domain.models import MeetingBrief, TaskDraft, ValidationResult
from app.domain.validation import apply_repairs, validate_task_draft


def validate_task_draft_tool(
    draft: TaskDraft,
    brief: MeetingBrief,
    source_text: str,
) -> ValidationResult:
    """确定性校验入口。evidence_quote 必须是原文连续子串。"""
    return validate_task_draft(draft, brief, source_text)


def repair_task_draft_tool(
    draft: TaskDraft,
    validation: ValidationResult,
    source_text: str,
    brief: MeetingBrief | None = None,
) -> TaskDraft:
    """只跑规则修复。EMPTY_TASK_LIST 是合法终态，禁止因此重新拆解。"""
    _ = brief
    return apply_repairs(draft, validation, source_text)


def drop_items_tool(draft: TaskDraft, item_ids: list[str]) -> TaskDraft:
    """按 UUID 删条并递增 version。审核删除和工具循环共用。"""
    drop = {str(i) for i in item_ids if i}
    remaining = [item for item in draft.items if item.id not in drop]
    return TaskDraft(items=remaining, version=draft.version + 1)


def export_markdown_tool(
    brief: MeetingBrief,
    draft: TaskDraft,
    validation: ValidationResult,
    source_text: str,
) -> str:
    """导出 Markdown；不读 owner_suggestions。"""
    return render_markdown(brief, draft, validation, source_text)
