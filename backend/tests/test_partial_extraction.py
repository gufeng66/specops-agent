"""部分抽取失败必须在后续重新校验后仍然可见（修订、批准都不丢告警）。

这里用假 LLM 客户端 + 替换 `_llm_decompose`，而不是靠 HTTP 桩制造失败：
分块数与失败块数需要完全确定，否则断言会随分块策略漂移。
"""

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.agent.graph import build_graph
from app.agent.workflow import SpecOpsWorkflow
from app.domain.models import ActionItem, TaskDraft, ThreadStatus
from app.llm.errors import LLMError
from app.memory.store import MemoryStore

TEXT = """会议目标：Q1 发布准备
参会人：张伟、李娜
张伟负责完成认证模块重构，下周五前提交代码评审。
李娜跟进 API 文档更新，本周内完成。
"""


def _item() -> ActionItem:
    return ActionItem(
        title="认证模块重构",
        description="张伟负责完成认证模块重构",
        primary_owner="张伟",
        secondary_owners=[],
        deadline="下周五",
        acceptance_criteria="提交代码评审",
        evidence_kind="source",
        evidence_quote="张伟负责完成认证模块重构，下周五前提交代码评审。",
    )


async def _partial_decompose(chunks, brief, source, base=None, revision_notes=None):
    """模拟：分块抽取部分失败（有成功块也有失败块）。"""
    draft = base if base is not None else TaskDraft()
    if not draft.items:
        draft = TaskDraft(items=[_item()])
    return draft, True, {"ok": 2, "empty": 0, "failed": 1}


class _FakeLLM:
    """enabled=True 但不发任何 HTTP：所有调用都走既有降级路径。"""

    enabled = True

    async def structured_extract_brief(self, *args, **kwargs):
        return None

    async def structured_decompose(self, *args, **kwargs):
        raise LLMError("fake decompose")

    async def structured_propose_owners(self, *args, **kwargs):
        return None

    async def chat_tools(self, *args, **kwargs):
        raise LLMError("fake tools")


async def _run(tmp_path, monkeypatch, name: str):
    monkeypatch.setattr("app.agent.nodes.llm_client", _FakeLLM())
    monkeypatch.setattr("app.agent.nodes._llm_decompose", _partial_decompose)

    store = MemoryStore(str(tmp_path / name))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread(f"t-{name}")
    async for _ in wf.run_chat(snap, TEXT, f"req-{name}-1"):
        pass
    return store, wf, await store.get_thread(f"t-{name}")


def _codes(snapshot) -> set[str]:
    return {issue.code for issue in snapshot.validation.issues}


@pytest.mark.asyncio
async def test_partial_extraction_is_recorded(tmp_path, monkeypatch):
    _, _, snap = await _run(tmp_path, monkeypatch, "partial.db")
    assert snap.extraction.partial is True
    assert snap.extraction.chunks_failed >= 1
    assert snap.extraction.chunks_ok >= 1
    assert "PARTIAL_EXTRACTION" in _codes(snap)
    assert snap.interrupt_payload is not None
    assert snap.interrupt_payload["extraction"]["partial"] is True


@pytest.mark.asyncio
async def test_partial_warning_survives_item_patch(tmp_path, monkeypatch):
    store, wf, snap = await _run(tmp_path, monkeypatch, "survive.db")
    item = snap.task_draft.items[0]
    # item_patch 走 validate_repair 重新校验，用来验证告警不是一次性注入
    async for _ in wf.resume(
        snap,
        "item",
        item.id,
        "",
        False,
        "req-survive-2",
        item_patch={"acceptance_criteria": "提交代码评审并通过"},
    ):
        pass
    snap = await store.get_thread("t-survive.db")
    assert snap.extraction.partial is True
    assert "PARTIAL_EXTRACTION" in _codes(snap)


@pytest.mark.asyncio
async def test_partial_warning_survives_approval(tmp_path, monkeypatch):
    store, wf, snap = await _run(tmp_path, monkeypatch, "approve.db")
    async for _ in wf.resume(snap, "item", None, "", True, "req-approve-2"):
        pass
    snap = await store.get_thread("t-approve.db")
    assert snap.status == ThreadStatus.COMPLETED
    assert "PARTIAL_EXTRACTION" in _codes(snap)
    assert "部分分块抽取失败" in snap.markdown_output
    assert "抽取状态**: 成功 2 / 零任务 0 / 失败 1" in snap.markdown_output
    assert "结果不完整，请人工确认" in snap.markdown_output
