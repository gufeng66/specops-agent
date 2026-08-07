from __future__ import annotations

import re
from datetime import datetime

from app.domain.models import (
    ActionItem,
    EvidenceKind,
    MeetingBrief,
    TaskDraft,
    ValidationResult,
)


def rule_extract_brief(source_text: str) -> MeetingBrief:
    """Rule-based intake: extract goal, stakeholders, dates from meeting notes."""
    lines = [ln.strip() for ln in source_text.splitlines() if ln.strip()]
    goal = ""
    stakeholders: list[str] = []
    meeting_date = ""
    context_parts: list[str] = []

    goal_patterns = [
        r"(?:会议目标|目标|议题)[：:]\s*(.+)",
        r"(?:讨论|关于)[：:]\s*(.+)",
    ]
    for line in lines:
        for pat in goal_patterns:
            m = re.search(pat, line)
            if m and not goal:
                goal = m.group(1).strip()
        date_m = re.search(r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)", line)
        if date_m and not meeting_date:
            meeting_date = date_m.group(1).replace("年", "-").replace("月", "-").replace("日", "")

    participant_patterns = [
        r"(?:参会人|参与人|干系人|与会)[：:]\s*(.+)",
        r"(?:出席)[：:]\s*(.+)",
    ]
    for line in lines:
        for pat in participant_patterns:
            m = re.search(pat, line)
            if m:
                parts = re.split(r"[、,，/\s]+", m.group(1))
                stakeholders.extend(p.strip() for p in parts if p.strip())

    if not stakeholders:
        name_pat = r"([张李王刘陈杨黄赵周吴徐孙马朱胡郭何高林罗郑梁][^\s,，、]{1,3})"
        name_matches = re.findall(name_pat, source_text)
        stakeholders = list(dict.fromkeys(name_matches))[:6]

    for line in lines:
        if not any(kw in line for kw in ("参会", "目标", "议题", "日期", "时间")):
            context_parts.append(line)

    return MeetingBrief(
        goal=goal,
        stakeholders=stakeholders,
        meeting_date=meeting_date,
        context="\n".join(context_parts[:5]),
    )


def needs_clarification(brief: MeetingBrief) -> bool:
    return not brief.goal.strip()


def rule_decompose(source_text: str, brief: MeetingBrief) -> TaskDraft:
    """Rule-based decompose: only evidence-backed sentences become tasks."""
    items: list[ActionItem] = []
    action_keywords = ("负责", "完成", "跟进", "提交", "制定", "上线", "修复", "确认", "安排", "推进", "部署", "测试")

    sentences: list[str] = []
    for line in source_text.splitlines():
        line = line.strip()
        if not line:
            continue
        for part in re.split(r"[。；;]", line):
            part = part.strip()
            if part:
                sentences.append(part)

    for sent in sentences:
        if not any(kw in sent for kw in action_keywords):
            continue
        if len(sent) < 4:
            continue

        owner = ""
        for name in brief.stakeholders:
            if name in sent:
                owner = name
                break

        deadline_m = re.search(r"(\d{1,2}月\d{1,2}日|\d{4}-\d{2}-\d{2}|下周五|本周|月底)", sent)
        deadline = deadline_m.group(1) if deadline_m else ""

        title = sent[:50] + ("..." if len(sent) > 50 else "")
        # Ensure evidence_quote is a substring of source_text
        quote = sent
        if quote not in source_text:
            for chunk in [sent, sent.rstrip("。"), sent + "。"]:
                if chunk in source_text:
                    quote = chunk
                    break
            else:
                # Find longest matching substring from source line containing key chars
                for line in source_text.splitlines():
                    if any(kw in line for kw in action_keywords if kw in sent):
                        quote = line.strip()
                        break

        items.append(
            ActionItem(
                title=title,
                description=sent,
                owner=owner,
                deadline=deadline,
                acceptance_criteria=f"「{title}」已完成并通过确认",
                evidence_kind=EvidenceKind.SOURCE,
                evidence_quote=quote,
            )
        )

    return TaskDraft(items=items)


def patch_target_item(draft: TaskDraft, item_id: str, feedback: str) -> TaskDraft:
    """Patch a single item based on human feedback, preserving other item IDs."""
    new_items: list[ActionItem] = []
    for item in draft.items:
        if item.id != item_id:
            new_items.append(item.model_copy(deep=True))
            continue
        patched = item.model_copy(deep=True)
        if feedback.strip():
            patched.description = f"{patched.description}\n[修订反馈] {feedback.strip()}".strip()
            if not patched.acceptance_criteria.endswith("（已修订）"):
                patched.acceptance_criteria = f"{patched.acceptance_criteria}（已修订）"
        new_items.append(patched)
    return TaskDraft(items=new_items, version=draft.version + 1)


def render_markdown(
    brief: MeetingBrief,
    draft: TaskDraft,
    validation: ValidationResult,
    source_text: str,
) -> str:
    lines = [
        "# SpecOps 任务导出",
        "",
        f"**会议目标**: {brief.goal or '（未明确）'}",
        f"**干系人**: {', '.join(brief.stakeholders) if brief.stakeholders else '（未识别）'}",
        f"**会议日期**: {brief.meeting_date or '（未识别）'}",
        "**同步状态**: not_synced",
        "",
        "## 任务清单",
        "",
    ]

    for idx, item in enumerate(draft.items, 1):
        lines.extend(
            [
                f"### {idx}. {item.title}",
                "",
                f"- **负责人**: {item.owner or '待指定'}",
                f"- **截止日期**: {item.deadline or '待定'}",
                f"- **验收标准**: {item.acceptance_criteria}",
                f"- **证据类型**: {item.evidence_kind.value}",
            ]
        )
        if item.evidence_kind == EvidenceKind.SOURCE:
            lines.append(f"- **原文引用**: {item.evidence_quote}")
        else:
            lines.append(f"- **推断理由**: {item.inference_reason}")
        lines.append("")

    if validation.issues:
        lines.extend(["## 未解决问题", ""])
        for issue in validation.issues:
            marker = "⚠️" if issue.severity.value == "advisory" else "❌"
            lines.append(f"- {marker} [{issue.code}] {issue.message}")
        lines.append("")

    lines.extend(["## 原文摘要", "", source_text[:500] + ("..." if len(source_text) > 500 else ""), ""])
    lines.append(f"_Generated at {datetime.now().strftime('%Y-%m-%d %H:%M')}_")
    return "\n".join(lines)
