from __future__ import annotations

from app.domain.models import (
    ActionItem,
    EvidenceKind,
    IssueSeverity,
    MeetingBrief,
    TaskDraft,
    ValidationIssue,
    ValidationResult,
)


def validate_task_draft(
    draft: TaskDraft,
    brief: MeetingBrief,
    source_text: str,
) -> ValidationResult:
    issues: list[ValidationIssue] = []

    if not draft.items:
        issues.append(
            ValidationIssue(
                code="EMPTY_TASK_LIST",
                message="任务列表为空，无法进入审核",
                severity=IssueSeverity.ERROR,
            )
        )

    stakeholders = set(brief.stakeholders)

    for item in draft.items:
        if not item.title.strip():
            issues.append(
                ValidationIssue(
                    code="MISSING_TITLE",
                    message=f"任务 {item.id} 缺少标题",
                    severity=IssueSeverity.ERROR,
                    item_id=item.id,
                    field="title",
                )
            )
        if not item.acceptance_criteria.strip():
            issues.append(
                ValidationIssue(
                    code="MISSING_ACCEPTANCE",
                    message=f"任务「{item.title or item.id}」缺少验收标准",
                    severity=IssueSeverity.ERROR,
                    item_id=item.id,
                    field="acceptance_criteria",
                )
            )
        if item.evidence_kind == EvidenceKind.SOURCE:
            quote = item.evidence_quote.strip()
            if not quote:
                issues.append(
                    ValidationIssue(
                        code="MISSING_EVIDENCE_QUOTE",
                        message=f"任务「{item.title or item.id}」缺少原文引用",
                        severity=IssueSeverity.ERROR,
                        item_id=item.id,
                        field="evidence_quote",
                    )
                )
            elif quote not in source_text:
                issues.append(
                    ValidationIssue(
                        code="INVALID_EVIDENCE_QUOTE",
                        message=f"任务「{item.title or item.id}」的引用不在原文中",
                        severity=IssueSeverity.ERROR,
                        item_id=item.id,
                        field="evidence_quote",
                    )
                )
        elif item.evidence_kind == EvidenceKind.INFERENCE:
            if not item.inference_reason.strip():
                issues.append(
                    ValidationIssue(
                        code="MISSING_INFERENCE_REASON",
                        message=f"任务「{item.title or item.id}」缺少推断理由",
                        severity=IssueSeverity.ERROR,
                        item_id=item.id,
                        field="inference_reason",
                    )
                )

        owner = item.owner.strip()
        if owner and stakeholders and owner not in stakeholders:
            issues.append(
                ValidationIssue(
                    code="INVALID_OWNER",
                    message=f"负责人「{owner}」不在干系人列表中",
                    severity=IssueSeverity.ERROR,
                    item_id=item.id,
                    field="owner",
                )
            )
        if not owner:
            issues.append(
                ValidationIssue(
                    code="MISSING_OWNER",
                    message=f"任务「{item.title or item.id}」未指定负责人",
                    severity=IssueSeverity.ADVISORY,
                    item_id=item.id,
                    field="owner",
                )
            )
        if not item.deadline.strip():
            issues.append(
                ValidationIssue(
                    code="MISSING_DEADLINE",
                    message=f"任务「{item.title or item.id}」未指定截止日期",
                    severity=IssueSeverity.ADVISORY,
                    item_id=item.id,
                    field="deadline",
                )
            )

    has_errors = any(i.severity == IssueSeverity.ERROR for i in issues)
    return ValidationResult(valid=not has_errors, issues=issues)


def repair_item(item: ActionItem, issue: ValidationIssue, source_text: str) -> ActionItem:
    patched = item.model_copy(deep=True)
    code = issue.code

    if code == "MISSING_ACCEPTANCE":
        patched.acceptance_criteria = f"完成「{patched.title or '任务'}」并经过干系人确认"
    elif code == "MISSING_EVIDENCE_QUOTE" and source_text:
        sentences = [s.strip() for s in source_text.replace("。", "。\n").split("\n") if s.strip()]
        if sentences:
            patched.evidence_quote = sentences[0][:120]
            patched.evidence_kind = EvidenceKind.SOURCE
    elif code == "INVALID_EVIDENCE_QUOTE":
        if patched.title and patched.title in source_text:
            patched.evidence_quote = patched.title
        else:
            patched.evidence_kind = EvidenceKind.INFERENCE
            patched.inference_reason = patched.inference_reason or "根据会议上下文推断"
            patched.evidence_quote = ""
    elif code == "INVALID_OWNER":
        patched.owner = "待指定"
    elif code == "MISSING_TITLE":
        patched.title = patched.description[:40] if patched.description else "待补充任务"

    return patched


def apply_repairs(
    draft: TaskDraft,
    validation: ValidationResult,
    source_text: str,
) -> TaskDraft:
    items_by_id = {item.id: item for item in draft.items}
    error_issues = validation.error_issues

    for issue in error_issues:
        if issue.code == "EMPTY_TASK_LIST":
            continue
        if issue.item_id and issue.item_id in items_by_id:
            items_by_id[issue.item_id] = repair_item(items_by_id[issue.item_id], issue, source_text)

    return TaskDraft(items=list(items_by_id.values()), version=draft.version + 1)
