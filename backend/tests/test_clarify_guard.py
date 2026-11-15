"""澄清守门：空白澄清不进 decompose；自循环换新 interrupt；旧中断不能推进新中断。"""

import json

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.agent.graph import build_graph
from app.agent.workflow import SpecOpsWorkflow
from app.domain.models import ThreadStatus
from app.memory.store import MemoryStore

NO_GOAL_TEXT = """参会人：张伟、李娜
讨论内容：
1. 张伟负责完成认证模块重构，下周五前提交代码评审。
"""


def _events(text: str, name: str) -> list[dict]:
    current = ""
    found = []
    for line in text.splitlines():
        if line.startswith("event:"):
            current = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and current == name:
            found.append(json.loads(line.split(":", 1)[1].strip()))
    return found


@pytest.mark.asyncio
async def test_blank_clarify_self_loops_with_new_interrupt(tmp_path):
    store = MemoryStore(str(tmp_path / "clarify.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("t-clarify")

    events = []
    async for ev in wf.run_chat(snap, NO_GOAL_TEXT, "req-c-1"):
        events.append(ev)
    snap = await store.get_thread("t-clarify")
    assert snap.status == ThreadStatus.AWAITING_REVIEW
    assert (snap.interrupt_payload or {}).get("type") == "clarify"
    first_id = snap.interrupt_id
    assert first_id

    events = []
    async for ev in wf.run_chat(
        snap,
        NO_GOAL_TEXT,
        "req-c-2",
        clarify_response="   ",
        interrupt_id=first_id,
        interrupt_type="clarify",
    ):
        events.append(ev)

    snap = await store.get_thread("t-clarify")
    # 仍是澄清中断，没有进 decompose
    assert snap.status == ThreadStatus.AWAITING_REVIEW
    assert (snap.interrupt_payload or {}).get("type") == "clarify"
    assert snap.task_draft.items == []
    assert snap.interrupt_id != first_id
    body = "".join(events)
    assert "decompose" not in body
    assert "澄清为空" in body or "不能为空" in body


@pytest.mark.asyncio
async def test_old_clarify_interrupt_cannot_advance_new_one(tmp_path):
    store = MemoryStore(str(tmp_path / "stale.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("t-stale")

    async for _ in wf.run_chat(snap, NO_GOAL_TEXT, "req-s-1"):
        pass
    snap = await store.get_thread("t-stale")
    stale_id = snap.interrupt_id

    async for _ in wf.run_chat(
        snap,
        NO_GOAL_TEXT,
        "req-s-2",
        clarify_response="  ",
        interrupt_id=stale_id,
        interrupt_type="clarify",
    ):
        pass
    snap = await store.get_thread("t-stale")
    fresh_id = snap.interrupt_id
    assert fresh_id and fresh_id != stale_id

    # 用旧 ID 提交合法澄清：必须被 INTERRUPT_MISMATCH 拒绝，图不推进
    events = []
    async for ev in wf.run_chat(
        snap,
        NO_GOAL_TEXT,
        "req-s-3",
        clarify_response="补一个明确的会议目标",
        interrupt_id=stale_id,
        interrupt_type="clarify",
    ):
        events.append(ev)
    body = "".join(events)
    assert "INTERRUPT_MISMATCH" in body or "中断已变化" in body

    snap = await store.get_thread("t-stale")
    assert snap.status == ThreadStatus.AWAITING_REVIEW
    assert snap.interrupt_id == fresh_id
    assert snap.task_draft.items == []


@pytest.mark.asyncio
async def test_valid_clarify_reaches_decompose(tmp_path):
    store = MemoryStore(str(tmp_path / "ok.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("t-ok")
    async for _ in wf.run_chat(snap, NO_GOAL_TEXT, "req-ok-1"):
        pass
    snap = await store.get_thread("t-ok")
    async for _ in wf.run_chat(
        snap,
        NO_GOAL_TEXT,
        "req-ok-2",
        clarify_response="确定认证模块在本周内完成重构",
        interrupt_id=snap.interrupt_id,
        interrupt_type="clarify",
    ):
        pass
    snap = await store.get_thread("t-ok")
    assert snap.meeting_brief.goal == "确定认证模块在本周内完成重构"
    assert snap.task_draft.items
