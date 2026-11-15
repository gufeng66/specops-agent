"""批准语义：非负责人错误默认阻断；override_reason 显式放行；负责人错误与空清单仍可批准。"""

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.agent.graph import build_graph
from app.agent.workflow import SpecOpsWorkflow
from app.domain.models import ThreadStatus
from app.domain.validation import blocking_codes, validate_task_draft
from app.memory.store import MemoryStore

SAMPLE_TEXT = """会议日期：2026-02-10
参会人：张伟、李娜
会议目标：Q1 发布准备

讨论内容：
1. 张伟负责完成认证模块重构，下周五前提交代码评审。
2. 李娜跟进 API 文档更新，本周内完成。
"""


async def _start(tmp_path, thread_id: str):
    store = MemoryStore(str(tmp_path / f"{thread_id}.db"))
    await store.init()
    graph = build_graph(MemorySaver())
    wf = SpecOpsWorkflow(store, graph=graph)
    snap = await store.ensure_thread(thread_id)
    async for _ in wf.run_chat(snap, SAMPLE_TEXT, f"req-{thread_id}-1"):
        pass
    snap = await store.get_thread(thread_id)
    assert snap.status == ThreadStatus.AWAITING_REVIEW
    return store, graph, wf, snap


async def _override_state(graph, snap, mutate) -> None:
    """把图状态改成测试需要的形态。review_node 会重新校验，所以改 task_draft 就够。"""
    draft = snap.task_draft.model_copy(deep=True)
    mutate(draft)
    config = {"configurable": {"thread_id": snap.graph_ns}}
    await graph.aupdate_state(config, {"task_draft": draft})


def _break_evidence(draft) -> None:
    draft.items[0].evidence_quote = "这句话并不出现在会议原文里"


def _drop_acceptance(draft) -> None:
    draft.items[0].acceptance_criteria = ""


def _drop_owner(draft) -> None:
    draft.items[0].primary_owner = ""
    draft.items[0].secondary_owners = []
    draft.items[0].unresolved_ref = ""


@pytest.mark.asyncio
async def test_invalid_evidence_approve_is_blocked(tmp_path):
    store, graph, wf, snap = await _start(tmp_path, "t-bad-eq")
    await _override_state(graph, snap, _break_evidence)

    async for _ in wf.resume(snap, "item", None, "", True, "req-bad-1"):
        pass

    final = await store.get_thread("t-bad-eq")
    # 没放行理由 → 回到审核，不生成导出
    assert final.status == ThreadStatus.AWAITING_REVIEW
    assert final.markdown_output == ""
    assert final.review.approved is False
    assert final.review.override_reason == ""
    assert final.interrupt_payload is not None
    codes = {issue["code"] for issue in final.interrupt_payload.get("blocking_errors") or []}
    assert "INVALID_EVIDENCE_QUOTE" in codes


@pytest.mark.asyncio
async def test_invalid_evidence_override_completes(tmp_path):
    store, graph, wf, snap = await _start(tmp_path, "t-ovr")
    await _override_state(graph, snap, _break_evidence)

    async for _ in wf.resume(
        snap,
        "item",
        None,
        "",
        True,
        "req-ovr-1",
        override_reason="会议原话已改写成书面表述，我确认语义一致",
        operator_claim="张三（本地审核）",
    ):
        pass

    final = await store.get_thread("t-ovr")
    assert final.status == ThreadStatus.COMPLETED
    assert final.review.approved is True
    assert final.review.override_reason.startswith("会议原话")
    assert final.review.override_codes == ["INVALID_EVIDENCE_QUOTE"]
    assert final.review.override_at
    assert final.review.operator_claim == "张三（本地审核）"
    assert "## 人工放行" in final.markdown_output
    assert "未认证" in final.markdown_output


@pytest.mark.asyncio
async def test_missing_acceptance_approve_is_blocked(tmp_path):
    store, graph, wf, snap = await _start(tmp_path, "t-no-acc")
    await _override_state(graph, snap, _drop_acceptance)

    async for _ in wf.resume(snap, "item", None, "", True, "req-no-acc-1"):
        pass

    final = await store.get_thread("t-no-acc")
    assert final.status == ThreadStatus.AWAITING_REVIEW
    assert final.markdown_output == ""
    codes = {issue["code"] for issue in final.interrupt_payload.get("blocking_errors") or []}
    assert "MISSING_ACCEPTANCE" in codes


@pytest.mark.asyncio
async def test_owner_only_errors_still_approve(tmp_path):
    store, graph, wf, snap = await _start(tmp_path, "t-owner")
    await _override_state(graph, snap, _drop_owner)

    async for _ in wf.resume(snap, "item", None, "", True, "req-owner-1"):
        pass

    final = await store.get_thread("t-owner")
    assert final.status == ThreadStatus.COMPLETED
    assert final.markdown_output
    assert final.review.approved is True
    assert final.review.override_reason == ""


def _item(**kwargs):
    from app.domain.models import ActionItem

    base = {
        "id": "a1",
        "title": "认证模块重构",
        "primary_owner": "张伟",
        "acceptance_criteria": "完成评审",
        "evidence_quote": "张伟负责完成认证模块重构",
    }
    base.update(kwargs)
    return ActionItem(**base)


def test_blocking_codes_excludes_owner_and_empty_list():
    from app.domain.models import TaskDraft

    source = "张伟负责完成认证模块重构"
    empty = validate_task_draft(TaskDraft(), _brief(), source)
    assert "EMPTY_TASK_LIST" in {i.code for i in empty.error_issues}
    assert blocking_codes(empty) == []

    owner_bad = validate_task_draft(
        TaskDraft(items=[_item(primary_owner="", evidence_quote="题")]), _brief(), source
    )
    codes = {i.code for i in owner_bad.error_issues}
    assert "INVALID_EVIDENCE_QUOTE" in codes or "MISSING_OWNER" in codes
    assert "MISSING_OWNER" in codes
    assert "MISSING_OWNER" not in blocking_codes(owner_bad)


def test_blocking_codes_flags_structure_errors():
    from app.domain.models import TaskDraft

    source = "张伟负责完成认证模块重构"
    bad = validate_task_draft(TaskDraft(items=[_item(acceptance_criteria="")]), _brief(), source)
    assert blocking_codes(bad) == ["MISSING_ACCEPTANCE"]


def _brief():
    from app.domain.models import MeetingBrief

    return MeetingBrief(goal="Q1 发布准备", stakeholders=["张伟", "李娜"])
