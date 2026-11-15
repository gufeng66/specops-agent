"""确定性工具：校验、规则修复、删条、导出。模型只能调这些，不能直接改 JSON 猜人。"""

from __future__ import annotations

from app.domain.extractors import render_markdown
from app.domain.models import (
    ExtractionInfo,
    MeetingBrief,
    ReviewInfo,
    TaskDraft,
    ValidationResult,
)
from app.domain.validation import apply_repairs, validate_task_draft, with_extraction_issues


def validate_task_draft_tool(
    draft: TaskDraft,
    brief: MeetingBrief,
    source_text: str,
) -> ValidationResult:
    """确定性校验入口。evidence_quote 必须是原文连续子串。"""
    return validate_task_draft(draft, brief, source_text)


def validate_and_annotate(
    draft: TaskDraft,
    brief: MeetingBrief,
    source_text: str,
    extraction: ExtractionInfo | None,
) -> ValidationResult:
    """校验 + 重新挂上抽取告警。所有重算 validation 的路径都必须走这里。

    直接调 validate_task_draft_tool 会丢掉 PARTIAL_EXTRACTION / RULE_FALLBACK_ONLY，
    因为它们是按 extraction 派生、不属于确定性校验本身。
    """
    result = validate_task_draft(draft, brief, source_text)
    return with_extraction_issues(result, extraction)


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
    review: ReviewInfo | None = None,
    extraction: ExtractionInfo | None = None,
) -> str:
    """导出 Markdown；不读 owner_suggestions。review 用于记录人工放行。"""
    return render_markdown(
        brief,
        draft,
        validation,
        source_text,
        review=review,
        extraction=extraction,
    )
