"""打分：召回 / 精确率 / 主责 / 次责 / 证据 / 负责人幻觉 / HITL。

黄金集门槛只用召回、主责、次责、HITL。新指标由调用方另设下限。
"""

from __future__ import annotations

import re
from typing import Any

from app.domain.validation import quote_in_source
from evals.runner import _keyword_score, match_item

RECALL_THRESHOLD = 0.9
PRIMARY_THRESHOLD = 0.8
SECONDARY_THRESHOLD = 0.8
HITL_THRESHOLD = 1.0
# 2026-11-04 规则模式黄金集实测后冻结：精确率 1，重复率 0，可定位率 1，标注匹配率 0.947。
PRECISION_THRESHOLD = 1.0
DUPLICATE_THRESHOLD = 0.0
QUOTE_LOCATABLE_THRESHOLD = 1.0
ANNOTATED_MATCH_THRESHOLD = 0.947


def score_case(
    case: dict[str, Any],
    snapshot,
    seen: list[str],
    items,
    stakeholders: list[str] | None = None,
) -> dict[str, Any]:
    """召回、精确率、主责、次责、证据、幻觉都打在传入的 items 上。"""
    expect = case.get("expect") or {}
    gold_items = list(expect.get("items") or [])
    extracted = list(items)
    used: set[str] = set()
    matched = 0
    primary_ok = 0
    secondary_ok = 0
    paired = 0

    for gold in gold_items:
        item = match_item(extracted, gold.get("title_keywords") or [], used)
        if item is None:
            continue
        matched += 1
        paired += 1
        if (item.primary_owner or "") == (gold.get("primary_owner") or ""):
            primary_ok += 1
        gold_sec = set(gold.get("secondary_owners") or [])
        got_sec = set(item.secondary_owners or [])
        if gold_sec == got_sec:
            secondary_ok += 1

    recall = (matched / len(gold_items)) if gold_items else 1.0
    if not extracted:
        precision = 1.0
    elif not gold_items:
        precision = 0.0
    else:
        precision = matched / len(extracted)
    primary_acc = (primary_ok / paired) if paired else 1.0
    secondary_acc = (secondary_ok / paired) if paired else 1.0
    source = getattr(snapshot, "source_text", "") or ""
    evidence_valid_rate = _quote_locatable_rate(extracted, source)
    annotated_match_rate = _annotated_match_rate(extracted, gold_items)
    duplicate_rate = _duplicate_rate(extracted, gold_items)
    quote_keyword_hit_rate = _quote_keyword_hit_rate(extracted, gold_items)
    owner_hallucination_rate = _owner_hallucination_rate(extracted, stakeholders or [])
    human_edit_ratio = _human_edit_ratio(snapshot)

    saw_clarify = "clarify" in seen
    saw_assign = "assign_owners" in seen
    hitl_ok = (bool(expect.get("needs_clarify")) == saw_clarify) and (
        bool(expect.get("needs_assignment")) == saw_assign
    )

    return {
        "id": case.get("id"),
        "recall": recall,
        "precision": precision,
        "duplicate_rate": duplicate_rate,
        "evidence_valid_rate": evidence_valid_rate,
        "quote_locatable_rate": evidence_valid_rate,
        "annotated_match_rate": annotated_match_rate,
        "quote_keyword_hit_rate": quote_keyword_hit_rate,
        "unmatched_outputs": len(extracted) - matched,
        "human_edit_ratio": human_edit_ratio,
        "owner_hallucination_rate": owner_hallucination_rate,
        "primary_acc": primary_acc,
        "secondary_acc": secondary_acc,
        "hitl_ok": hitl_ok,
        "seen": seen,
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """宏平均后对照门槛。任一项不达标 passed=False。"""
    n = len(rows) or 1
    recall = sum(r["recall"] for r in rows) / n
    primary_acc = sum(r["primary_acc"] for r in rows) / n
    secondary_acc = sum(r["secondary_acc"] for r in rows) / n
    hitl = sum(1.0 if r["hitl_ok"] else 0.0 for r in rows) / n
    precision = sum(r["precision"] for r in rows) / n
    duplicate_rate = sum(r["duplicate_rate"] for r in rows) / n
    quote_locatable_rate = sum(r["quote_locatable_rate"] for r in rows) / n
    annotated_match_rate = sum(r["annotated_match_rate"] for r in rows) / n
    passed = (
        recall >= RECALL_THRESHOLD
        and primary_acc >= PRIMARY_THRESHOLD
        and secondary_acc >= SECONDARY_THRESHOLD
        and hitl >= HITL_THRESHOLD
        and precision >= PRECISION_THRESHOLD
        and duplicate_rate <= DUPLICATE_THRESHOLD
        and quote_locatable_rate >= QUOTE_LOCATABLE_THRESHOLD
        and annotated_match_rate >= ANNOTATED_MATCH_THRESHOLD
    )
    return {
        "n": len(rows),
        "recall": recall,
        "precision": precision,
        "duplicate_rate": duplicate_rate,
        "quote_locatable_rate": quote_locatable_rate,
        "annotated_match_rate": annotated_match_rate,
        "primary_acc": primary_acc,
        "secondary_acc": secondary_acc,
        "hitl": hitl,
        "passed": passed,
        "thresholds": {
            "recall": RECALL_THRESHOLD,
            "precision": PRECISION_THRESHOLD,
            "duplicate_rate": DUPLICATE_THRESHOLD,
            "quote_locatable_rate": QUOTE_LOCATABLE_THRESHOLD,
            "annotated_match_rate": ANNOTATED_MATCH_THRESHOLD,
            "primary_acc": PRIMARY_THRESHOLD,
            "secondary_acc": SECONDARY_THRESHOLD,
            "hitl": HITL_THRESHOLD,
        },
        "rows": rows,
    }


def _kind(item) -> str:
    kind = getattr(item.evidence_kind, "value", item.evidence_kind)
    return str(kind or "")


def _quote_locatable_rate(items: list, source: str) -> float:
    """引用可定位率。分母是全部输出。inference 没有原文引用时不算可定位。"""
    if not items:
        return 1.0
    ok = 0
    for item in items:
        if _kind(item) == "source" and quote_in_source(item.evidence_quote or "", source):
            ok += 1
    return ok / len(items)


def _annotated_match_rate(items: list, gold_items: list[dict[str, Any]]) -> float:
    """标注证据匹配率。未匹配输出和没有标注片段的 inference 都进分母。"""
    if not items:
        return 1.0
    ok = 0
    for item in items:
        gold = _gold_for_item(item, gold_items)
        spans = list((gold or {}).get("evidence_spans") or [])
        accepted = list((gold or {}).get("acceptable_quotes") or [])
        if _span_supports(item, spans, accepted):
            ok += 1
    return ok / len(items)


def _gold_for_item(item, gold_items: list[dict[str, Any]]) -> dict[str, Any] | None:
    blob = f"{item.title} {item.description}"
    best = None
    best_score = 0
    for gold in gold_items:
        score = _keyword_score(blob, gold.get("title_keywords") or [])
        if score > best_score:
            best_score = score
            best = gold
    return best if best_score > 0 else None


def _compact(text: str) -> str:
    return re.sub(r"[\s。！？，,；;：:、]", "", text or "")


def _span_supports(item, spans: list[str], accepted: list[str] | None = None) -> bool:
    """引用必须盖住整段标注。更短的引用只有写在 acceptable_quotes 里才算。"""
    if not spans:
        return False
    quote = item.evidence_quote or ""
    reason = item.inference_reason or ""
    compact_quote = _compact(quote)
    allowed = {_compact(frag) for frag in accepted or [] if _compact(frag)}
    for span in spans:
        compact_span = _compact(span)
        if not compact_span:
            continue
        if compact_quote and compact_span in compact_quote:
            return True
        if compact_quote and compact_quote in allowed:
            return True
        if _kind(item) == "inference" and compact_span in _compact(reason):
            return True
    return False


def _duplicate_rate(items: list, gold_items: list[dict[str, Any]]) -> float:
    """同一黄金任务被多条输出命中的比例。"""
    if not gold_items:
        return 0.0
    dup = 0
    for gold in gold_items:
        keywords = gold.get("title_keywords") or []
        hits = 0
        for item in items:
            blob = f"{item.title} {item.description}"
            if _keyword_score(blob, keywords) > 0:
                hits += 1
        if hits > 1:
            dup += 1
    return dup / len(gold_items)


def _quote_keyword_hit_rate(items: list, gold_items: list[dict[str, Any]]) -> float:
    """引用里出现标题关键词。这不是证据支持。"""
    if not items:
        return 1.0
    ok = 0
    for item in items:
        gold = _gold_for_item(item, gold_items)
        if gold and _keyword_score(item.evidence_quote or "", gold.get("title_keywords") or []) > 0:
            ok += 1
    return ok / len(items)


def _human_edit_ratio(snapshot) -> float | None:
    """批准前实际改过的不同任务数 / 进入审核的任务数。没有审核时为空。"""
    review = getattr(snapshot, "review", None)
    reviewed = int(getattr(review, "reviewed_task_count", 0) or 0)
    if not reviewed:
        return None
    edited = len(getattr(review, "edited_item_ids", []) or [])
    return edited / reviewed


def _owner_hallucination_rate(items: list, stakeholders: list[str]) -> float:
    """主责非空且不在补名单前的干系人里。没有主责时记 0。"""
    named = [item for item in items if (item.primary_owner or "").strip()]
    if not named:
        return 0.0
    allowed = set(stakeholders)
    bad = sum(1 for item in named if item.primary_owner.strip() not in allowed)
    return bad / len(named)
