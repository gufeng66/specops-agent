"""规则路径抽取：无 API Key 时仍能跑完整条流水线。

LLM 结果与这里的底稿 merge，而不是替换。代词（他/她）命中且句中无干系人姓名时
主责留空并填 unresolved_ref，禁止写「待指定」。
Markdown 导出按人分组，不输出 owner_suggestions。
"""

from __future__ import annotations

import re

from app.config import settings
from app.domain.models import (
    ActionItem,
    EvidenceKind,
    MeetingBrief,
    TaskDraft,
    ValidationResult,
)

PRIMARY_ROLE_MARKERS = ("负责", "统筹", "主责", "牵头")
SECONDARY_ROLE_MARKERS = ("配合", "协助", "推进", "支持", "参与")
PRONOUNS = ("他们", "她们", "它们", "他", "她", "它")
ACTION_KEYWORDS = (
    "负责",
    "完成",
    "跟进",
    "提交",
    "制定",
    "上线",
    "修复",
    "确认",
    "安排",
    "推进",
    "部署",
    "测试",
    "统筹",
    "配合",
    "协助",
)


def _has_action_keyword(sent: str) -> bool:
    """句子是否含行动动词。「负责人」是职称，不当成「负责」命中。"""
    for kw in ACTION_KEYWORDS:
        if kw == "负责":
            if re.search(r"负责(?!人)", sent):
                return True
            continue
        if kw in sent:
            return True
    return False


DEADLINE_PATTERN = (
    r"(\d{4}年\d{1,2}月\d{1,2}日(?:\d{1,2}:\d{2})?|\d{1,2}月\d{1,2}日|\d{4}-\d{2}-\d{2}|下周五|本周|月底)"
)
TITLE_MARKERS = ("由",) + PRIMARY_ROLE_MARKERS + SECONDARY_ROLE_MARKERS
TITLE_SUFFIXES = ("工作", "任务", "事项", "安排")
TITLE_PREFIXES = ("本次", "这次", "关于")
DEADLINE_NEIGHBORS = ("之前", "之内", "前", "内", "后")
GENERIC_ACCEPTANCE_RE = re.compile(
    r"^「.+」已完成并通过确认$|^完成「.+」并经过干系人确认$"
)


def _is_generic_acceptance(text: str) -> bool:
    """规则修复填的套话验收，校验时记 ADVISORY，不当最终标准。"""
    return bool(GENERIC_ACCEPTANCE_RE.match((text or "").strip()))


def _strip_deadline(text: str, deadline: str) -> str:
    """从标题候选里去掉截止日期及其前后「之前/之内」，避免标题带时间尾巴。"""
    if not deadline:
        return text
    idx = text.find(deadline)
    if idx < 0:
        return text
    start, end = idx, idx + len(deadline)
    before = text[:start]
    for neighbor in DEADLINE_NEIGHBORS:
        if before.endswith(neighbor):
            start -= len(neighbor)
            before = text[:start]
            break
    after = text[end:]
    for neighbor in DEADLINE_NEIGHBORS:
        if after.startswith(neighbor):
            end += len(neighbor)
            after = text[end:]
            break
    stripped = (text[:start] + text[end:]).strip()
    stripped = re.sub(r"截止时间为?", "", stripped)
    return stripped.strip(" ，,、；;")


def _earliest_marker_index(text: str) -> int | None:
    """标题截断点：最早出现的「由/负责/配合…」，跳过「负责人」。"""
    found: list[int] = []
    for marker in TITLE_MARKERS:
        start = 0
        while True:
            pos = text.find(marker, start)
            if pos < 0:
                break
            if marker == "负责" and text[pos : pos + 3] == "负责人":
                start = pos + 1
                continue
            found.append(pos)
            break
    if not found:
        return None
    return min(found)


def _short_title(sent: str, deadline: str = "") -> str:
    """规则路径标题：先去截止日期，再在职责标记或动词处截断，最长 50 字。"""
    fallback = sent[:50]
    text = _strip_deadline(sent.strip().rstrip("。；;"), deadline)
    cut = _earliest_marker_index(text)
    if cut is not None:
        text = text[:cut].strip()
    else:
        verb_at: int | None = None
        for kw in ACTION_KEYWORDS:
            pos = text.find(kw)
            if pos <= 0:
                continue
            if kw == "负责" and text[pos : pos + 3] == "负责人":
                continue
            if verb_at is None or pos < verb_at:
                verb_at = pos
        if verb_at:
            text = text[:verb_at].strip()
    changed = True
    while changed:
        changed = False
        for suffix in TITLE_SUFFIXES:
            if text.endswith(suffix) and len(text) > len(suffix):
                text = text[: -len(suffix)].strip()
                changed = True
    for prefix in TITLE_PREFIXES:
        if text.startswith(prefix) and len(text) > len(prefix):
            text = text[len(prefix) :].strip()
    text = text.strip(" ，,、的")
    return (text or fallback)[:50]


def rule_extract_brief(source_text: str) -> MeetingBrief:
    """规则 intake：从主题行、参会行、发言人标签、百家姓搭配职责动词抽摘要。"""
    lines = [ln.strip() for ln in source_text.splitlines() if ln.strip()]
    goal = ""
    stakeholders: list[str] = []
    meeting_date = ""
    context_parts: list[str] = []

    goal_patterns = [
        r"(?:会议主题|会议目标|目标|议题)[：:]\s*([^；;]+)",
        r"(?:讨论|关于)[：:]\s*([^；;]+)",
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
        r"(?:会议负责人)[：:]\s*([^；;。]+)",
    ]
    for line in lines:
        for pat in participant_patterns:
            m = re.search(pat, line)
            if m:
                parts = re.split(r"[、,，/\s]+", m.group(1))
                stakeholders.extend(p.strip() for p in parts if p.strip())

    speaker_pat = r"^(?:说话人|发言人)[：:]\s*([^\s：:]{1,8})"
    for line in lines:
        m = re.match(speaker_pat, line)
        if m:
            name = m.group(1).strip()
            if name and name not in stakeholders:
                stakeholders.append(name)
        dialogue_m = re.match(r"^([\u4e00-\u9fff]{2,4})[：:]\s*\S", line)
        if dialogue_m:
            name = dialogue_m.group(1).strip()
            skip = {
                "会议主题",
                "会议目标",
                "参会人",
                "干系人",
                "议题",
                "讨论",
                "会议日期",
                "会议负责人",
                "说话人",
                "发言人",
            }
            if name and name not in skip and name not in stakeholders:
                stakeholders.append(name)

    name_pat = (
        r"([张李王刘陈杨黄赵周吴徐孙马朱胡郭何高林罗郑梁邓][\u4e00-\u9fff]{1,2})"
        r"(?=负责|统筹|配合|牵头|主责|须|和|、|，|,|。|；|;|$)"
    )
    for name in re.findall(name_pat, source_text):
        if name and name not in stakeholders:
            stakeholders.append(name)

    stakeholders = list(dict.fromkeys(s for s in stakeholders if s))[:12]

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
    """goal 为空才澄清；有隐含议题时 intake 模型应写出可核对的目标。"""
    return not brief.goal.strip()


def _split_clause(sent: str, marker: str) -> tuple[str, str]:
    """按配合类标记把句子切成主责半句 / 次责半句。"""
    idx = sent.find(marker)
    if idx < 0:
        return sent, ""
    return sent[: idx + len(marker)], sent[idx + len(marker) :]


def extract_owners_from_sentence(sent: str, stakeholders: list[str]) -> tuple[str, list[str]]:
    """句中干系人：负责/统筹 → 主责，配合/协助 → 次责。名单外的名字不认。"""
    mentioned = [name for name in stakeholders if name and name in sent]
    if not mentioned:
        return "", []

    primary = ""
    secondaries: list[str] = []

    has_primary_marker = any(m in sent for m in PRIMARY_ROLE_MARKERS)
    has_secondary_marker = any(m in sent for m in SECONDARY_ROLE_MARKERS)

    if has_primary_marker and has_secondary_marker:
        primary_clause = sent
        secondary_clause = ""
        for marker in SECONDARY_ROLE_MARKERS:
            if marker in sent:
                primary_clause, secondary_clause = _split_clause(sent, marker)
                break
        primary_hits = [n for n in mentioned if n in primary_clause]
        secondary_hits = [n for n in mentioned if n in secondary_clause]
        if primary_hits:
            primary = primary_hits[0]
            secondaries = [n for n in secondary_hits if n != primary]
            leftover = [n for n in mentioned if n != primary and n not in secondaries]
            secondaries.extend(leftover)
        elif secondary_hits:
            leftover = [n for n in mentioned if n not in secondary_hits]
            if leftover:
                primary = leftover[0]
                secondaries = [n for n in mentioned if n != primary]
            else:
                primary = secondary_hits[0]
                secondaries = [n for n in secondary_hits[1:]]
        else:
            primary = mentioned[0]
            secondaries = mentioned[1:]
    elif has_primary_marker:
        primary = mentioned[0]
        secondaries = mentioned[1:]
    elif has_secondary_marker and len(mentioned) > 1:
        primary = mentioned[0]
        secondaries = mentioned[1:]
    elif len(mentioned) == 1:
        primary = mentioned[0]
    else:
        primary = mentioned[0]
        secondaries = mentioned[1:]

    secondaries = list(dict.fromkeys(s for s in secondaries if s and s != primary))
    return primary, secondaries


def detect_unresolved_ref(sent: str, stakeholders: list[str]) -> str:
    """句中有他/她等代词且没有干系人姓名时返回该代词，供人工分配，禁止猜人。"""
    mentioned = [name for name in stakeholders if name and name in sent]
    if mentioned:
        return ""
    for pronoun in PRONOUNS:
        if pronoun in sent:
            return pronoun
    return ""


def rule_decompose(source_text: str, brief: MeetingBrief) -> TaskDraft:
    """规则拆解：只把含行动动词的句子做成任务；引用就是原句，不编造。"""
    items: list[ActionItem] = []

    header_prefixes = (
        "会议主题",
        "会议目标",
        "目标",
        "议题",
        "参会人",
        "参与人",
        "干系人",
        "与会",
        "出席",
        "会议负责人",
        "会议日期",
        "日期",
    )
    sentences: list[str] = []
    for line in source_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if any(line.startswith(p) for p in header_prefixes):
            continue
        stripped = re.sub(r"^(?:说话人|发言人)[：:]\s*", "", line)
        stripped = re.sub(r"^[\u4e00-\u9fff]{2,4}[：:]\s*", "", stripped)
        for part in re.split(r"。+", stripped if stripped else line):
            part = part.strip()
            if part:
                sentences.append(part)

    for sent in sentences:
        if not _has_action_keyword(sent):
            continue
        if len(sent) < 4:
            continue

        primary, secondaries = extract_owners_from_sentence(sent, brief.stakeholders)
        unresolved_ref = detect_unresolved_ref(sent, brief.stakeholders)
        if unresolved_ref:
            primary = ""
            secondaries = []

        deadline_m = re.search(DEADLINE_PATTERN, sent)
        deadline = deadline_m.group(1) if deadline_m else ""

        title = _short_title(sent, deadline)
        quote = sent
        if quote not in source_text:
            for chunk in [sent, sent.rstrip("。"), sent + "。"]:
                if chunk in source_text:
                    quote = chunk
                    break
            else:
                for line in source_text.splitlines():
                    if any(kw in line for kw in ACTION_KEYWORDS if kw in sent):
                        quote = line.strip()
                        break

        items.append(
            ActionItem(
                title=title,
                description=sent,
                primary_owner=primary,
                secondary_owners=secondaries,
                deadline=deadline,
                acceptance_criteria=f"「{title}」已完成并通过确认",
                evidence_kind=EvidenceKind.SOURCE,
                evidence_quote=quote,
                unresolved_ref=unresolved_ref,
            )
        )

    return TaskDraft(items=items)


def patch_target_item(draft: TaskDraft, item_id: str, feedback: str) -> TaskDraft:
    """审核单项修订：只改目标条目描述，其它 UUID 保持不变。"""
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


def apply_owner_assignments(
    draft: TaskDraft,
    assignments: list[dict],
) -> TaskDraft:
    """HITL 提交后唯一允许改主责的入口。未出现在 assignments 里的条目保持原样。"""
    by_id = {item.id: item.model_copy(deep=True) for item in draft.items}
    for raw in assignments:
        item_id = str(raw.get("item_id") or "")
        if item_id not in by_id:
            continue
        patched = by_id[item_id]
        if "primary_owner" in raw:
            patched.primary_owner = str(raw.get("primary_owner") or "").strip()
            if patched.primary_owner:
                patched.unresolved_ref = ""
        if "secondary_owners" in raw:
            owners = raw.get("secondary_owners") or []
            if isinstance(owners, str):
                owners = [owners]
            patched.secondary_owners = [str(s).strip() for s in owners if str(s).strip()]
        by_id[item_id] = patched
    return TaskDraft(items=list(by_id.values()), version=draft.version + 1)


def render_markdown(
    brief: MeetingBrief,
    draft: TaskDraft,
    validation: ValidationResult,
    source_text: str,
) -> str:
    """按人看板导出。不读 owner_suggestions；待分配单独成节。"""
    task_count = len(draft.items)
    lines = [
        "# 会议纪要",
        "",
        f"**会议目标**: {brief.goal or '（未明确）'}",
        f"**会议日期**: {brief.meeting_date or '（未识别）'}",
        f"**干系人**: {'、'.join(brief.stakeholders) if brief.stakeholders else '（未识别）'}",
        f"**任务数**: {task_count}",
        "",
    ]
    if task_count == 0:
        lines.extend(["## 本次无任务", "", "审核后确认本次会议没有可执行任务。", ""])

    people: list[str] = []
    for name in brief.stakeholders:
        if name and name not in people:
            people.append(name)
    for item in draft.items:
        if item.primary_owner and item.primary_owner not in people:
            people.append(item.primary_owner)
        for name in item.secondary_owners:
            if name and name not in people:
                people.append(name)

    unassigned = [item for item in draft.items if not (item.primary_owner or "").strip()]
    assigned_ids: set[str] = set()

    def primary_line(index: int, item: ActionItem) -> str:
        parts = [f"{index}. **{item.title}**"]
        parts.append(item.deadline or "待定")
        if item.secondary_owners:
            parts.append("配合：" + "、".join(item.secondary_owners))
        return " · ".join(parts)

    def collab_line(item: ActionItem) -> str:
        owner = item.primary_owner or "—"
        return f"配合 · {item.title}（主责{owner}）"

    if people:
        for name in people:
            primary_items = [
                item for item in draft.items if (item.primary_owner or "").strip() == name
            ]
            secondary_items = [
                item
                for item in draft.items
                if name in item.secondary_owners and (item.primary_owner or "").strip() != name
            ]
            if not primary_items and not secondary_items:
                continue
            if primary_items:
                lines.append(f"## {name}（主责 {len(primary_items)}）")
            else:
                lines.append(f"## {name}（配合 {len(secondary_items)}）")
            lines.append("")
            for idx, item in enumerate(primary_items, 1):
                assigned_ids.add(item.id)
                lines.append(primary_line(idx, item))
            for item in secondary_items:
                assigned_ids.add(item.id)
                lines.append(collab_line(item))
            lines.append("")
    else:
        numbered = [item for item in draft.items if (item.primary_owner or "").strip()]
        if numbered:
            lines.extend(["## 任务清单", ""])
            for idx, item in enumerate(numbered, 1):
                assigned_ids.add(item.id)
                lines.append(primary_line(idx, item))
            lines.append("")

    leftover = [item for item in unassigned if item.id not in assigned_ids]
    if leftover:
        lines.extend(["## 待分配", ""])
        for item in leftover:
            extra = f"（未消解：{item.unresolved_ref}）" if item.unresolved_ref else ""
            lines.append(f"- **{item.title}**{extra}")
        lines.append("")

    if validation.issues:
        lines.extend(["## 未解决问题", ""])
        for issue in validation.issues:
            marker = "⚠️" if issue.severity.value == "advisory" else "❌"
            lines.append(f"- {marker} [{issue.code}] {issue.message}")
        lines.append("")

    source = source_text or ""
    summary = f"共 {task_count} 条任务 / 原文 {len(source)} 字"
    if len(source) > settings.source_soft_limit_chars:
        summary += "，已分块抽取"
    lines.append(summary)
    lines.append("")
    lines.append("<details>")
    lines.append("<summary>原文</summary>")
    lines.append("")
    lines.append(source)
    lines.append("")
    lines.append("</details>")
    return "\n".join(lines)
