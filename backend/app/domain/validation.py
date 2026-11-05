"""任务草案校验与规则修复；负责人错误禁止自动填「待指定」。

与候选清洗共用 `quote_in_source`，避免任务草稿和 OwnerCandidate 两套引用规则漂移。
"""

from __future__ import annotations

from app.domain.models import (
    ActionItem,
    EvidenceKind,
    IssueSeverity,
    MeetingBrief,
    OwnerCandidate,
    TaskDraft,
    ValidationIssue,
    ValidationResult,
)

# 这些 ERROR 必须进 propose_owners → assign_owners，规则/LLM 修复循环都跳过。
OWNER_ERROR_CODES = frozenset(
    {
        "MISSING_OWNER",
        "INVALID_PRIMARY",
        "INVALID_SECONDARY",
        "PRIMARY_IN_SECONDARY",
        "UNRESOLVED_REFERENCE",
    }
)
UNREPAIRABLE_CODES = OWNER_ERROR_CODES | frozenset(
    {
        "MISSING_EVIDENCE_QUOTE",
        "INVALID_EVIDENCE_QUOTE",
        "MISSING_INFERENCE_REASON",
        "EMPTY_TASK_LIST",
    }
)


def has_owner_errors(validation: ValidationResult) -> bool:
    """是否存在需要人工分配的负责人类 ERROR。"""
    return any(i.code in OWNER_ERROR_CODES for i in validation.error_issues)


def quote_in_source(quote: str, source_text: str) -> bool:
    """引用必须是原文连续子串（strip 后）。空串视为无效。"""
    q = (quote or "").strip()
    return bool(q) and q in (source_text or "")


def candidate_evidence_ok(candidate: OwnerCandidate, source_text: str) -> bool:
    """建议证据是否可定位。不要求候选人姓名出现在原文（否则代词消解全灭）。"""
    if candidate.evidence_kind == EvidenceKind.SOURCE:
        return quote_in_source(candidate.evidence_quote, source_text)
    if candidate.evidence_kind == EvidenceKind.INFERENCE:
        return bool((candidate.inference_reason or "").strip())
    return False


def sanitize_owner_candidates(
    candidates: list[OwnerCandidate],
    source_text: str,
    stakeholders: list[str],
    *,
    max_primary: int = 3,
    max_secondary: int = 3,
    primary_owner: str = "",
    existing_secondary: list[str] | None = None,
) -> list[OwnerCandidate]:
    """清洗模型建议：丢无效证据，保留模型顺序，干系人名单内的排在前面。

    名单外的姓名允许保留（标低档 + reason），但前端「采纳全部」会因此禁用，
    防止一键写入后立刻 INVALID_PRIMARY。
    次责不得与已确定主责同名，也不重复已有次责。
    """
    roster = {name.strip() for name in stakeholders if name and name.strip()}
    primary = (primary_owner or "").strip()
    already_secondary = {n.strip() for n in (existing_secondary or []) if n and str(n).strip()}
    kept: list[OwnerCandidate] = []
    n_primary = 0
    n_secondary = 0
    for raw in candidates:
        cand = raw.model_copy(deep=True)
        if not cand.name:
            continue
        if not candidate_evidence_ok(cand, source_text):
            continue
        role = cand.role if cand.role in ("primary", "secondary") else "primary"
        cand.role = role
        if role == "secondary":
            if cand.name == primary or cand.name in already_secondary:
                continue
            if n_secondary >= max_secondary:
                continue
            n_secondary += 1
        else:
            if n_primary >= max_primary:
                continue
            n_primary += 1
        if roster and cand.name not in roster:
            cand.confidence = min(cand.confidence, 0.33)
            note = "不在干系人名单"
            if note not in cand.reason:
                cand.reason = f"{cand.reason}（{note}）" if cand.reason else note
        kept.append(cand)
    in_roster = [c for c in kept if not roster or c.name in roster]
    out_roster = [c for c in kept if roster and c.name not in roster]
    return in_roster + out_roster


def validate_task_draft(
    draft: TaskDraft,
    brief: MeetingBrief,
    source_text: str,
) -> ValidationResult:
    """确定性校验。空清单是合法终态（审核后可导出「本次无任务」），仍记 EMPTY_TASK_LIST。"""
    issues: list[ValidationIssue] = []

    if not draft.items:
        issues.append(
            ValidationIssue(
                code="EMPTY_TASK_LIST",
                message="任务列表为空",
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
            elif not quote_in_source(quote, source_text):
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

        primary = item.primary_owner.strip()
        secondaries = [s.strip() for s in item.secondary_owners if s.strip()]
        unresolved = (item.unresolved_ref or "").strip()
        # 有未消解代词时只报 UNRESOLVED_REFERENCE，避免和 MISSING_OWNER 叠两条。
        if unresolved and not primary:
            issues.append(
                ValidationIssue(
                    code="UNRESOLVED_REFERENCE",
                    message=(
                        f"任务「{item.title or item.id}」中的「{unresolved}」无法从原文确认指代谁"
                    ),
                    severity=IssueSeverity.ERROR,
                    item_id=item.id,
                    field="primary_owner",
                )
            )
        elif not primary:
            issues.append(
                ValidationIssue(
                    code="MISSING_OWNER",
                    message=f"任务「{item.title or item.id}」未指定主要负责人",
                    severity=IssueSeverity.ERROR,
                    item_id=item.id,
                    field="primary_owner",
                )
            )
        elif primary not in stakeholders:
            issues.append(
                ValidationIssue(
                    code="INVALID_PRIMARY",
                    message=f"主要负责人「{primary}」不在干系人列表中",
                    severity=IssueSeverity.ERROR,
                    item_id=item.id,
                    field="primary_owner",
                )
            )
        for secondary in secondaries:
            if secondary not in stakeholders:
                issues.append(
                    ValidationIssue(
                        code="INVALID_SECONDARY",
                        message=f"次要负责人「{secondary}」不在干系人列表中",
                        severity=IssueSeverity.ERROR,
                        item_id=item.id,
                        field="secondary_owners",
                    )
                )
        if primary and primary in secondaries:
            issues.append(
                ValidationIssue(
                    code="PRIMARY_IN_SECONDARY",
                    message=f"主要负责人「{primary}」不能同时出现在次要负责人中",
                    severity=IssueSeverity.ERROR,
                    item_id=item.id,
                    field="secondary_owners",
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
    """修非负责人类错误。OWNER_ERROR 原样返回，禁止猜人。"""
    patched = item.model_copy(deep=True)
    code = issue.code

    if code == "MISSING_ACCEPTANCE":
        patched.acceptance_criteria = f"完成「{patched.title or '任务'}」并经过干系人确认"
        patched.acceptance_origin = "system"
    elif code in UNREPAIRABLE_CODES:
        return item
    elif code == "MISSING_TITLE":
        patched.title = patched.description[:40] if patched.description else "待补充任务"

    return patched


def apply_repairs(
    draft: TaskDraft,
    validation: ValidationResult,
    source_text: str,
) -> TaskDraft:
    """按 item_id 应用 repair_item。空清单和负责人错误都不在这里处理。"""
    items_by_id = {item.id: item for item in draft.items}
    error_issues = validation.error_issues

    changed = False
    for issue in error_issues:
        if issue.code in UNREPAIRABLE_CODES:
            continue
        if issue.item_id and issue.item_id in items_by_id:
            repaired = repair_item(items_by_id[issue.item_id], issue, source_text)
            if repaired.model_dump() != items_by_id[issue.item_id].model_dump():
                items_by_id[issue.item_id] = repaired
                changed = True

    if not changed:
        return draft
    return TaskDraft(items=list(items_by_id.values()), version=draft.version + 1)
