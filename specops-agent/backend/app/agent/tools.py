"""Deterministic agent tools — no LLM. Evidence hard-checks stay here."""

from __future__ import annotations

from pathlib import Path

from app.domain.extractors import render_markdown, rule_decompose
from app.domain.models import MeetingBrief, TaskDraft, ValidationResult
from app.domain.validation import apply_repairs, validate_task_draft

PLAYBOOK_DIR = Path(__file__).resolve().parent.parent / "knowledge"


def search_playbooks(query: str, limit: int = 5) -> list[dict]:
    """Keyword lookup over local playbook markdown under app/knowledge/. Not vector RAG."""
    query_lower = query.lower()
    results: list[dict] = []

    for md_file in PLAYBOOK_DIR.glob("**/*.md"):
        if md_file.name.startswith("_"):
            continue
        text = md_file.read_text(encoding="utf-8")
        rel_path = str(md_file.relative_to(PLAYBOOK_DIR))
        score = 0
        matched_lines: list[str] = []
        tokens = [kw for kw in query.split() if len(kw) > 1]
        for line in text.splitlines():
            line_stripped = line.strip()
            if not line_stripped or line_stripped.startswith("#"):
                continue
            if any(kw in line_stripped for kw in tokens):
                score += 2
                matched_lines.append(line_stripped)
            elif any(kw in line_stripped.lower() for kw in query_lower.split() if len(kw) > 1):
                score += 1
                matched_lines.append(line_stripped)

        if score > 0:
            results.append(
                {
                    "source": rel_path,
                    "score": score,
                    "excerpt": matched_lines[0][:200] if matched_lines else "",
                    "note": "Playbook 参考（工具查询），非当前会议原文证据",
                }
            )

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]


def validate_task_draft_tool(
    draft: TaskDraft,
    brief: MeetingBrief,
    source_text: str,
) -> ValidationResult:
    """Deterministic validation; enforces evidence_quote ⊆ source_text."""
    return validate_task_draft(draft, brief, source_text)


def repair_task_draft_tool(
    draft: TaskDraft,
    validation: ValidationResult,
    source_text: str,
    brief: MeetingBrief | None = None,
) -> TaskDraft:
    """Rule-based repair only (not LLM). EMPTY_TASK_LIST re-runs rule decompose."""
    if any(i.code == "EMPTY_TASK_LIST" for i in validation.error_issues):
        if brief is not None:
            return rule_decompose(source_text, brief)
    return apply_repairs(draft, validation, source_text)


def export_markdown_tool(
    brief: MeetingBrief,
    draft: TaskDraft,
    validation: ValidationResult,
    source_text: str,
) -> str:
    return render_markdown(brief, draft, validation, source_text)
