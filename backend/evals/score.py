"""黄金集打分：任务召回 / 主责 exact / 次责 set / HITL 触发。不代表 LLM 抽取质量。"""

from __future__ import annotations

from typing import Any

from evals.runner import match_item

RECALL_THRESHOLD = 0.9
PRIMARY_THRESHOLD = 0.8
SECONDARY_THRESHOLD = 0.8
HITL_THRESHOLD = 1.0


def score_case(case: dict[str, Any], snapshot, seen: list[str]) -> dict[str, Any]:
    """单 case：召回看关键词配对，主责 exact，次责集合相等，HITL 类型必须对上 expect。"""
    expect = case.get("expect") or {}
    gold_items = list(expect.get("items") or [])
    extracted = list(snapshot.task_draft.items)
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
    primary_acc = (primary_ok / paired) if paired else 1.0
    secondary_acc = (secondary_ok / paired) if paired else 1.0

    saw_clarify = "clarify" in seen
    saw_assign = "assign_owners" in seen
    hitl_ok = (bool(expect.get("needs_clarify")) == saw_clarify) and (
        bool(expect.get("needs_assignment")) == saw_assign
    )

    return {
        "id": case.get("id"),
        "recall": recall,
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
