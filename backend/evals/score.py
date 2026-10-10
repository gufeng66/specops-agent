"""打分：召回 / 精确率 / 主责 / 次责 / 证据 / 负责人幻觉 / HITL。

黄金集门槛只用召回、主责、次责、HITL。新指标由调用方另设下限。
"""

from __future__ import annotations

from typing import Any

from app.domain.validation import quote_in_source
from evals.runner import match_item

RECALL_THRESHOLD = 0.9
PRIMARY_THRESHOLD = 0.8
SECONDARY_THRESHOLD = 0.8
HITL_THRESHOLD = 1.0


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
    evidence_valid_rate = _evidence_valid_rate(extracted, getattr(snapshot, "source_text", "") or "")
    owner_hallucination_rate = _owner_hallucination_rate(extracted, stakeholders or [])

    saw_clarify = "clarify" in seen
    saw_assign = "assign_owners" in seen
    hitl_ok = (bool(expect.get("needs_clarify")) == saw_clarify) and (
        bool(expect.get("needs_assignment")) == saw_assign
    )

    return {
        "id": case.get("id"),
        "recall": recall,
        "precision": precision,
        "evidence_valid_rate": evidence_valid_rate,
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
    passed = (
        recall >= RECALL_THRESHOLD
        and primary_acc >= PRIMARY_THRESHOLD
        and secondary_acc >= SECONDARY_THRESHOLD
        and hitl >= HITL_THRESHOLD
    )
    return {
        "n": len(rows),
        "recall": recall,
        "primary_acc": primary_acc,
        "secondary_acc": secondary_acc,
        "hitl": hitl,
        "passed": passed,
        "thresholds": {
            "recall": RECALL_THRESHOLD,
            "primary_acc": PRIMARY_THRESHOLD,
            "secondary_acc": SECONDARY_THRESHOLD,
            "hitl": HITL_THRESHOLD,
        },
        "rows": rows,
    }


def _evidence_valid_rate(items: list, source: str) -> float:
    """source 引用是原文子串的占比。没有 source 证据时记 1。"""
    source_items = []
    for item in items:
        kind = getattr(item.evidence_kind, "value", item.evidence_kind)
        if kind == "source":
            source_items.append(item)
    if not source_items:
        return 1.0
    ok = sum(1 for item in source_items if quote_in_source(item.evidence_quote or "", source))
    return ok / len(source_items)


def _owner_hallucination_rate(items: list, stakeholders: list[str]) -> float:
    """主责非空且不在补名单前的干系人里。没有主责时记 0。"""
    named = [item for item in items if (item.primary_owner or "").strip()]
    if not named:
        return 0.0
    allowed = set(stakeholders)
    bad = sum(1 for item in named if item.primary_owner.strip() not in allowed)
    return bad / len(named)
