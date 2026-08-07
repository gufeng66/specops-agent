import json

import pytest
from httpx import ASGITransport, AsyncClient, Response
from langgraph.checkpoint.memory import MemorySaver

from app.agent.graph import WORKFLOW_EDGES, WORKFLOW_NODES, build_graph, get_topology
from app.agent.tools import search_playbooks
from app.agent.workflow import SpecOpsWorkflow
from app.domain.extractors import (
    needs_clarification,
    patch_target_item,
    rule_decompose,
    rule_extract_brief,
)
from app.domain.models import ActionItem, EvidenceKind, TaskDraft, ThreadStatus
from app.domain.validation import apply_repairs, validate_task_draft
from app.main import app
from app.memory.store import MemoryStore

SAMPLE_TEXT = """会议日期：2026-02-10
参会人：张伟、李娜、王强
会议目标：Q1 产品发布冲刺计划

讨论内容：
1. 张伟负责完成用户认证模块重构，下周五前提交代码评审。
2. 李娜跟进 API 文档更新，本周内完成并发布到内部 Wiki。
3. 王强安排预发环境部署测试，确认 2 月 20 日前上线。
"""


def test_graph_topology():
    topo = get_topology()
    for node in WORKFLOW_NODES:
        assert node in topo["nodes"]
    for edge in WORKFLOW_EDGES:
        assert edge in topo["edges"]
    # Real compiled graph must include playbook retrieve + HITL routes
    assert "retrieve_playbooks" in topo["nodes"]
    assert ("decompose", "retrieve_playbooks") in topo["edges"]
    assert ("review", "finalize") in topo["edges"]


def test_rule_extract_brief():
    brief = rule_extract_brief(SAMPLE_TEXT)
    assert "Q1" in brief.goal or "发布" in brief.goal
    assert len(brief.stakeholders) >= 2


def test_needs_clarification():
    from app.domain.models import MeetingBrief

    assert needs_clarification(MeetingBrief(goal=""))
    assert not needs_clarification(MeetingBrief(goal="有目标"))


def test_rule_decompose_evidence_backed():
    brief = rule_extract_brief(SAMPLE_TEXT)
    draft = rule_decompose(SAMPLE_TEXT, brief)
    assert len(draft.items) >= 2
    for item in draft.items:
        assert item.evidence_kind == EvidenceKind.SOURCE
        assert item.evidence_quote in SAMPLE_TEXT


def test_validate_quote_substring():
    brief = rule_extract_brief(SAMPLE_TEXT)
    item = ActionItem(
        title="测试",
        acceptance_criteria="完成测试",
        evidence_kind=EvidenceKind.SOURCE,
        evidence_quote="不存在的引用",
    )
    draft = TaskDraft(items=[item])
    result = validate_task_draft(draft, brief, SAMPLE_TEXT)
    assert any(i.code == "INVALID_EVIDENCE_QUOTE" for i in result.error_issues)


def test_validate_invalid_owner():
    brief = rule_extract_brief(SAMPLE_TEXT)
    item = ActionItem(
        title="测试任务",
        owner="不存在的人",
        acceptance_criteria="完成",
        evidence_kind=EvidenceKind.SOURCE,
        evidence_quote="张伟负责完成用户认证模块重构",
    )
    draft = TaskDraft(items=[item])
    result = validate_task_draft(draft, brief, SAMPLE_TEXT)
    assert any(i.code == "INVALID_OWNER" for i in result.error_issues)


def test_repair_invalid_owner():
    brief = rule_extract_brief(SAMPLE_TEXT)
    item = ActionItem(
        title="测试",
        owner="外人",
        acceptance_criteria="",
        evidence_kind=EvidenceKind.SOURCE,
        evidence_quote="张伟负责完成用户认证模块重构，下周五前提交代码评审",
    )
    draft = TaskDraft(items=[item])
    validation = validate_task_draft(draft, brief, SAMPLE_TEXT)
    repaired = apply_repairs(draft, validation, SAMPLE_TEXT)
    assert repaired.items[0].owner == "待指定"
    assert repaired.items[0].acceptance_criteria


def test_patch_target_item_preserves_ids():
    items = [
        ActionItem(id="a1", title="任务1", acceptance_criteria="完成1", evidence_quote="x"),
        ActionItem(id="a2", title="任务2", acceptance_criteria="完成2", evidence_quote="y"),
    ]
    draft = TaskDraft(items=items)
    patched = patch_target_item(draft, "a1", "请补充细节")
    assert patched.items[0].id == "a1"
    assert patched.items[1].id == "a2"
    assert "修订反馈" in patched.items[0].description


def test_search_playbooks():
    results = search_playbooks("部署")
    assert len(results) > 0
    assert all("note" in r for r in results)
    assert all("Playbook" in r["note"] or "playbook" in r["note"].lower() for r in results)


@pytest.mark.asyncio
async def test_health():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/health")
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_full_workflow_none_provider(tmp_path):
    store = MemoryStore(str(tmp_path / "wf.db"))
    await store.init()
    graph = build_graph(MemorySaver())
    wf = SpecOpsWorkflow(store, graph=graph)
    snap = await store.ensure_thread("thread-1")

    events = []
    async for ev in wf.run_chat(snap, SAMPLE_TEXT, "req-1"):
        events.append(ev)

    assert any("event: progress" in e for e in events)
    assert any("event: tool_result" in e and "search_playbooks" in e for e in events)
    assert any("event: interrupt" in e and "review" in e for e in events)
    final = await store.get_thread("thread-1")
    assert final.status == ThreadStatus.AWAITING_REVIEW
    assert len(final.task_draft.items) >= 1
    assert final.graph_ns


@pytest.mark.asyncio
async def test_repair_exhausted_still_reviews(tmp_path):
    store = MemoryStore(str(tmp_path / "repair.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("thread-r")

    bad_text = "会议目标：测试\n参会人：甲\n某人负责做某事。"
    async for _ in wf.run_chat(snap, bad_text, "req-r"):
        pass

    final = await store.get_thread("thread-r")
    assert final.status == ThreadStatus.AWAITING_REVIEW


@pytest.mark.asyncio
async def test_item_revise_and_approve(tmp_path):
    store = MemoryStore(str(tmp_path / "rev.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("thread-rev")

    async for _ in wf.run_chat(snap, SAMPLE_TEXT, "req-rev"):
        pass
    snap = await store.get_thread("thread-rev")
    item_id = snap.task_draft.items[0].id

    async for _ in wf.resume(snap, "item", item_id, "补充验收标准", False, "req-rev2"):
        pass
    snap = await store.get_thread("thread-rev")
    assert snap.status == ThreadStatus.AWAITING_REVIEW

    async for _ in wf.resume(snap, "item", item_id, "", True, "req-rev3"):
        pass
    snap = await store.get_thread("thread-rev")
    assert snap.status == ThreadStatus.COMPLETED
    assert snap.markdown_output
    assert snap.sync_status == "not_synced"


@pytest.mark.asyncio
async def test_idempotency(tmp_path):
    store = MemoryStore(str(tmp_path / "idem.db"))
    await store.init()
    snap = await store.ensure_thread("t-idem")
    snap.run_id = "run-1"
    await store.complete_request("req-idem", "t-idem", "run-1", snap)

    claimed, existing = await store.claim_request("req-idem", "t-idem")
    assert claimed is False
    assert existing is not None


@pytest.mark.asyncio
async def test_thread_busy(tmp_path):
    from app.errors import ThreadBusyError

    store = MemoryStore(str(tmp_path / "busy.db"))
    await store.init()
    snap = await store.ensure_thread("t-busy")
    snap.status = ThreadStatus.PLANNING
    await store.save_thread(snap)

    with pytest.raises(ThreadBusyError):
        await store.claim_request("new-req", "t-busy")


@pytest.mark.asyncio
async def test_export_endpoint(tmp_path):
    store = MemoryStore(str(tmp_path / "exp.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("t-exp")

    async for _ in wf.run_chat(snap, SAMPLE_TEXT, "req-exp"):
        pass
    snap = await store.get_thread("t-exp")
    item_id = snap.task_draft.items[0].id
    async for _ in wf.resume(snap, "item", item_id, "", True, "req-exp2"):
        pass

    snap = await store.get_thread("t-exp")
    from app.domain.extractors import render_markdown

    md = render_markdown(snap.meeting_brief, snap.task_draft, snap.validation, snap.source_text)
    assert "SpecOps" in md
    assert snap.sync_status == "not_synced"


@pytest.mark.asyncio
async def test_clarify_hitl(tmp_path):
    store = MemoryStore(str(tmp_path / "clarify.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("t-clarify")

    text = "参会人：张三\n张三负责完成部署测试。"
    events = []
    async for ev in wf.run_chat(snap, text, "req-c1"):
        events.append(ev)

    assert any("clarify" in e for e in events)
    snap = await store.get_thread("t-clarify")
    assert snap.status == ThreadStatus.AWAITING_REVIEW

    events2 = []
    async for ev in wf.run_chat(snap, text, "req-c2", clarify_response="明确部署目标"):
        events2.append(ev)

    snap = await store.get_thread("t-clarify")
    assert snap.meeting_brief.goal
    assert snap.status == ThreadStatus.AWAITING_REVIEW  # review after clarify path


@pytest.mark.asyncio
async def test_llm_fallback_with_httpx_mock(tmp_path, monkeypatch):
    """Mock DeepSeek HTTP; on parse failure workflow falls back to rules."""
    import respx

    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

    from app.llm import client as client_mod

    monkeypatch.setattr(client_mod.settings, "llm_provider", "deepseek")
    monkeypatch.setattr(client_mod.settings, "deepseek_api_key", "sk-test")
    monkeypatch.setattr(client_mod.settings, "deepseek_base_url", "https://api.deepseek.com")
    monkeypatch.setattr(client_mod.settings, "deepseek_model", "deepseek-v4-flash")
    client_mod.llm_client = client_mod.LLMClient()
    monkeypatch.setattr("app.agent.nodes.llm_client", client_mod.llm_client)

    store = MemoryStore(str(tmp_path / "llm.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("t-llm")

    with respx.mock(assert_all_called=False) as router:
        router.post("https://api.deepseek.com/chat/completions").mock(
            return_value=Response(500, json={"error": "boom"})
        )
        events = []
        async for ev in wf.run_chat(snap, SAMPLE_TEXT, "req-llm"):
            events.append(ev)

    snap = await store.get_thread("t-llm")
    assert snap.status == ThreadStatus.AWAITING_REVIEW
    assert len(snap.task_draft.items) >= 1
    assert any("event: interrupt" in e for e in events)


@pytest.mark.asyncio
async def test_llm_structured_success(tmp_path, monkeypatch):
    import respx

    monkeypatch.setattr("app.llm.client.settings.llm_provider", "deepseek")
    monkeypatch.setattr("app.llm.client.settings.deepseek_api_key", "sk-test")
    monkeypatch.setattr("app.llm.client.settings.deepseek_base_url", "https://api.deepseek.com")
    monkeypatch.setattr("app.llm.client.settings.deepseek_model", "deepseek-v4-flash")

    from app.llm.client import LLMClient

    llm = LLMClient()
    assert llm.enabled

    brief_payload = {
        "brief": {
            "goal": "Q1 产品发布冲刺计划",
            "stakeholders": ["张伟", "李娜", "王强"],
            "meeting_date": "2026-02-10",
            "context": "冲刺讨论",
        }
    }
    quote = "张伟负责完成用户认证模块重构，下周五前提交代码评审"
    items_payload = {
        "items": [
            {
                "title": "认证模块重构",
                "description": quote,
                "owner": "张伟",
                "deadline": "下周五",
                "acceptance_criteria": "代码评审通过",
                "evidence_kind": "source",
                "evidence_quote": quote,
                "priority": "high",
            }
        ]
    }

    call_count = {"n": 0}

    def _reply(request):
        call_count["n"] += 1
        body = json.loads(request.content)
        sys_content = body["messages"][0]["content"]
        if "brief" in sys_content or "解析" in sys_content:
            content = json.dumps(brief_payload, ensure_ascii=False)
        else:
            content = json.dumps(items_payload, ensure_ascii=False)
        return Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    store = MemoryStore(str(tmp_path / "llm-ok.db"))
    await store.init()

    # Nodes use module-level llm_client — patch it
    monkeypatch.setattr("app.agent.nodes.llm_client", llm)

    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("t-llm-ok")

    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(side_effect=_reply)
        async for _ in wf.run_chat(snap, SAMPLE_TEXT, "req-llm-ok"):
            pass

    snap = await store.get_thread("t-llm-ok")
    assert snap.status == ThreadStatus.AWAITING_REVIEW
    assert any(i.evidence_quote in SAMPLE_TEXT for i in snap.task_draft.items)
    assert call_count["n"] >= 1
