"""规则路径评测 runner。检测到 LLM Key 直接失败，避免污染黄金集门槛。

seen 记录的是 interrupt 类型（clarify/assign_owners/review），不是 SSE 节点名；
propose_owners 不停图，因此不影响 HITL 一致性统计。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.memory import MemorySaver

from app.agent.graph import build_graph
from app.agent.workflow import SpecOpsWorkflow
from app.domain.models import ThreadSnapshot
from app.llm.client import llm_client
from app.memory.store import MemoryStore

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
MAX_STEPS = 12
KNOWN_INTERRUPTS = {"clarify", "assign_owners", "review"}


class EvalError(RuntimeError):
    """评测失败：有 LLM Key、未知 interrupt、或步数超上限。"""
    pass


def load_cases() -> list[dict[str, Any]]:
    """读取 golden/*.json。文件名顺序即评测顺序。"""
    cases = []
    for path in sorted(GOLDEN_DIR.glob("*.json")):
        cases.append(json.loads(path.read_text(encoding="utf-8")))
    return cases


def _keyword_score(text: str, keywords: list[str]) -> int:
    """标题+描述里命中几个 gold 关键词。"""
    return sum(1 for kw in keywords if kw and kw in text)


def match_item(items: list, keywords: list[str], used: set[str]):
    """用标题关键词贪心配对，used 防止同一条任务被两个 gold 抢走。"""
    best = None
    best_score = 0
    for item in items:
        if item.id in used:
            continue
        blob = f"{item.title} {item.description}"
        score = _keyword_score(blob, keywords)
        if score > best_score:
            best_score = score
            best = item
    if best is None or best_score <= 0:
        return None
    used.add(best.id)
    return best


def _assignments_for(snap: ThreadSnapshot, case: dict[str, Any]) -> list[dict[str, Any]]:
    """把 gold.assignments 的关键词映射成当前草案 UUID，自动 resume 分配。"""
    gold = list((case.get("gold") or {}).get("assignments") or [])
    used: set[str] = set()
    out: list[dict[str, Any]] = []
    for spec in gold:
        item = match_item(snap.task_draft.items, spec.get("title_keywords") or [], used)
        if item is None:
            continue
        out.append(
            {
                "item_id": item.id,
                "primary_owner": spec.get("primary_owner") or "",
                "secondary_owners": list(spec.get("secondary_owners") or []),
            }
        )
    return out


async def run_case(case: dict[str, Any], tmp_dir: Path) -> dict[str, Any]:
    """独立 MemorySaver + SQLite，自动 resume 黄金答复。有 Key 直接失败。"""
    if llm_client.enabled:
        raise EvalError("llm_client.enabled is True; refuse to pollute frozen eval")

    case_id = str(case.get("id") or uuid4())
    store = MemoryStore(str(tmp_dir / f"{case_id}.db"))
    await store.init()
    graph = build_graph(MemorySaver())
    wf = SpecOpsWorkflow(store, graph=graph)
    snap = await store.ensure_thread(case_id)
    graph_ns = f"{case_id}:{uuid4()}"
    snap.graph_ns = graph_ns

    seen: list[str] = []
    async for _ in wf.run_chat(snap, case["source_text"], f"{case_id}-0"):
        pass

    for step in range(MAX_STEPS):
        snap = await store.get_thread(case_id)
        assert snap is not None
        if snap.markdown_output and not (
            snap.interrupt_payload and snap.status.value == "awaiting_review"
        ):
            return {"snapshot": snap, "seen": seen, "steps": step}

        payload = snap.interrupt_payload or {}
        itype = str(payload.get("type") or "")
        if not itype:
            if snap.markdown_output:
                return {"snapshot": snap, "seen": seen, "steps": step}
            raise EvalError(f"{case_id}: stopped without interrupt or markdown")
        if itype not in KNOWN_INTERRUPTS:
            raise EvalError(f"{case_id}: unknown interrupt type {itype}")
        seen.append(itype)

        if itype == "clarify":
            text = str((case.get("gold") or {}).get("clarify") or "补充会议目标")
            async for _ in wf.run_chat(snap, snap.source_text, f"{case_id}-c{step}", clarify_response=text):
                pass
            continue
        if itype == "assign_owners":
            assignments = _assignments_for(snap, case)
            async for _ in wf.resume(
                snap, "item", None, "", False, f"{case_id}-a{step}", assignments=assignments
            ):
                pass
            continue
        if itype == "review":
            item_id = snap.task_draft.items[0].id if snap.task_draft.items else None
            async for _ in wf.resume(snap, "item", item_id, "", True, f"{case_id}-r{step}"):
                pass
            continue

    raise EvalError(f"{case_id}: exceeded interrupt cap {MAX_STEPS}")
