"""工作流与领域单测：拓扑、建议≠写入、HITL resume、规则抽取。

有真实 API Key 时部分用例会 monkeypatch LLM 关闭，避免本地 .env 污染断言。
"""

import json

import pytest
from httpx import ASGITransport, AsyncClient, Response
from langgraph.checkpoint.memory import MemorySaver

from app.agent.graph import WORKFLOW_EDGES, WORKFLOW_NODES, build_graph, get_topology
from app.agent.tools import drop_items_tool
from app.agent.workflow import SpecOpsWorkflow
from app.domain.extractors import (
    _is_generic_acceptance,
    _short_title,
    extract_owners_from_sentence,
    needs_clarification,
    patch_target_item,
    render_markdown,
    rule_decompose,
    rule_extract_brief,
)
from app.domain.models import ActionItem, EvidenceKind, OwnerCandidate, TaskDraft, ThreadStatus
from app.domain.validation import (
    apply_repairs,
    has_owner_errors,
    sanitize_owner_candidates,
    validate_task_draft,
)
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

MULTI_OWNER_TEXT = """会议目标：新产品上线
参会人：周婷、孙磊、邓凯、马超
周婷：新产品上线工作由周婷统筹，孙磊、邓凯和马超配合，截止时间为2026年8月31日。
"""


def test_graph_topology():
    topo = get_topology()
    for node in WORKFLOW_NODES:
        assert node in topo["nodes"]
    for edge in WORKFLOW_EDGES:
        assert edge in topo["edges"]
    assert "retrieve_playbooks" not in topo["nodes"]
    assert "assign_owners" in topo["nodes"]
    assert "propose_owners" in topo["nodes"]
    assert ("validate_repair", "propose_owners") in topo["edges"]
    assert ("propose_owners", "assign_owners") in topo["edges"]
    assert ("propose_owners", "review") in topo["edges"]
    assert ("validate_repair", "review") in topo["edges"]
    assert ("assign_owners", "review") in topo["edges"]
    assert ("validate_repair", "assign_owners") not in topo["edges"]


def test_rule_extract_brief():
    brief = rule_extract_brief(SAMPLE_TEXT)
    assert "Q1" in brief.goal or "发布" in brief.goal
    assert len(brief.stakeholders) >= 2


def test_rule_extract_brief_speakers():
    text = """会议目标：部署演练
张伟：今天把预发环境部署做完。
李娜：我配合写文档。
"""
    brief = rule_extract_brief(text)
    assert "张伟" in brief.stakeholders
    assert "李娜" in brief.stakeholders


def test_discussion_clause_keeps_later_commitment():
    brief = rule_extract_brief("会议目标：上线\n参会人：张三")
    kept = rule_decompose("张三先讨论方案，然后负责完成部署。", brief)
    assert len(kept.items) == 1
    assert kept.items[0].primary_owner == "张三"
    assert "部署" in kept.items[0].description
    assert "先讨论" not in kept.items[0].description
    skipped = rule_decompose("暂不推进支付改造，先讨论可行性。", brief)
    assert skipped.items == []


def test_rule_extract_brief_topic_header():
    from pathlib import Path

    sample = Path(__file__).resolve().parents[2] / "frontend/public/samples/demo_sprint_planning.txt"
    text = sample.read_text(encoding="utf-8")
    brief = rule_extract_brief(text)
    assert "新产品上线" in brief.goal
    assert "周婷" in brief.stakeholders
    assert "孙磊" in brief.stakeholders
    assert "马超" in brief.stakeholders
    assert all("负责" not in name for name in brief.stakeholders)
    draft = rule_decompose(text, brief)
    assert len(draft.items) >= 20
    safety = next(i for i in draft.items if "安全事件通知" in i.description or "双渠道通知" in i.description)
    assert safety.primary_owner == "马超"
    assert "产品负责人和管理层" in safety.description
    launch = next(i for i in draft.items if "新产品上线" in i.title or "新产品上线" in i.description)
    assert launch.primary_owner == "周婷"
    assert set(launch.secondary_owners) >= {"孙磊", "邓凯", "马超"}
    assert not has_owner_errors(validate_task_draft(draft, brief, text))
    md = render_markdown(brief, draft, validate_task_draft(draft, brief, text), text)
    zhou_block = md.split("## 周婷")[1].split("## ")[0]
    assert launch.title in zhou_block
    body = md.split("<details>")[0]
    assert "已完成并通过确认" not in body
    people_md = body[body.find("\n## ") :]
    expected: dict[str, int] = {}
    for item in draft.items:
        expected[item.title] = expected.get(item.title, 0) + 1 + len(item.secondary_owners)
    for title, cap in expected.items():
        hits = people_md.count(f"**{title}**") + people_md.count(f"配合 · {title}（")
        assert hits <= cap, (title, hits, cap)


def test_feature_review_sample_hits_unresolved_ref():
    from pathlib import Path

    sample = Path(__file__).resolve().parents[2] / "frontend/public/samples/demo_feature_review.txt"
    text = sample.read_text(encoding="utf-8")
    brief = rule_extract_brief(text)
    draft = rule_decompose(text, brief)
    assert any(i.unresolved_ref == "她" for i in draft.items)
    assert has_owner_errors(validate_task_draft(draft, brief, text))


def test_short_title_extracts_before_role_marker():
    sent = "本次会议组织任务由周婷负责，截止时间为2026年8月7日17:30。"
    title = _short_title(sent, "2026年8月7日17:30")
    assert title == "会议组织"
    assert "2026" not in title
    assert "前" not in title


def test_short_title_strips_deadline_before_marker():
    title = _short_title("张伟下周五前完成认证重构", "下周五")
    assert "下周五" not in title
    assert "前" not in title
    assert "认证" in title or "重构" in title or title == "张伟"


def test_short_title_unmarked_fallback():
    sent = "没有角色标记的一句话用于回退截断处理，内容需要足够长以便验证标题不会整句保留超过五十个汉字。"
    title = _short_title(sent, "")
    assert len(title) <= 50
    assert title == sent.strip().rstrip("。；;")[:50]


def test_generic_acceptance_skipped_in_markdown():
    assert _is_generic_acceptance("「会议组织」已完成并通过确认")
    assert _is_generic_acceptance("完成「会议组织」并经过干系人确认")
    assert not _is_generic_acceptance("代码评审通过")


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


def test_multi_owner_extract():
    sent = "新产品上线工作由周婷统筹，孙磊、邓凯和马超配合"
    primary, secondaries = extract_owners_from_sentence(
        sent, ["周婷", "孙磊", "邓凯", "马超"]
    )
    assert primary == "周婷"
    assert set(secondaries) == {"孙磊", "邓凯", "马超"}

    brief = rule_extract_brief(MULTI_OWNER_TEXT)
    draft = rule_decompose(MULTI_OWNER_TEXT, brief)
    assert draft.items
    hit = next(i for i in draft.items if "上线" in i.title or "上线" in i.description)
    assert hit.primary_owner == "周婷"
    assert set(hit.secondary_owners) == {"孙磊", "邓凯", "马超"}


def test_validate_quote_substring():
    brief = rule_extract_brief(SAMPLE_TEXT)
    item = ActionItem(
        title="测试",
        acceptance_criteria="完成测试",
        evidence_kind=EvidenceKind.SOURCE,
        evidence_quote="不存在的引用",
        primary_owner="张伟",
    )
    draft = TaskDraft(items=[item])
    result = validate_task_draft(draft, brief, SAMPLE_TEXT)
    assert any(i.code == "INVALID_EVIDENCE_QUOTE" for i in result.error_issues)


def test_validate_invalid_primary():
    brief = rule_extract_brief(SAMPLE_TEXT)
    item = ActionItem(
        title="测试任务",
        primary_owner="不存在的人",
        acceptance_criteria="完成",
        evidence_kind=EvidenceKind.SOURCE,
        evidence_quote="张伟负责完成用户认证模块重构",
    )
    draft = TaskDraft(items=[item])
    result = validate_task_draft(draft, brief, SAMPLE_TEXT)
    assert any(i.code == "INVALID_PRIMARY" for i in result.error_issues)
    assert has_owner_errors(result)


def test_repair_does_not_fill_placeholder_owner():
    brief = rule_extract_brief(SAMPLE_TEXT)
    item = ActionItem(
        title="测试",
        primary_owner="外人",
        acceptance_criteria="",
        evidence_kind=EvidenceKind.SOURCE,
        evidence_quote="张伟负责完成用户认证模块重构，下周五前提交代码评审",
    )
    draft = TaskDraft(items=[item])
    validation = validate_task_draft(draft, brief, SAMPLE_TEXT)
    repaired = apply_repairs(draft, validation, SAMPLE_TEXT)
    assert repaired.items[0].primary_owner == "外人"
    assert repaired.items[0].primary_owner != "待指定"
    assert repaired.items[0].acceptance_criteria
    after = validate_task_draft(repaired, brief, SAMPLE_TEXT)
    assert any(i.code == "INVALID_PRIMARY" for i in after.error_issues)


def test_patch_target_item_preserves_ids():
    items = [
        ActionItem(id="a1", title="任务1", acceptance_criteria="完成1", evidence_quote="x"),
        ActionItem(id="a2", title="任务2", acceptance_criteria="完成2", evidence_quote="y"),
    ]
    draft = TaskDraft(items=items)
    patched, changed = patch_target_item(draft, "a1", {"deadline": "下周五"})
    assert changed
    assert patched.items[0].id == "a1"
    assert patched.items[1].id == "a2"
    assert patched.items[0].deadline == "下周五"
    assert patched.items[1].deadline == ""
    same, unchanged = patch_target_item(patched, "a1", {"deadline": "下周五"})
    assert not unchanged
    missing, _ = patch_target_item(patched, "missing", {"title": "x"})
    assert missing is patched


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
    assert any("status" in e and "running" in e for e in events)
    assert any("event: tool_result" in e and "validate_task_draft" in e for e in events)
    assert any("event: interrupt" in e and "review" in e for e in events)
    final = await store.get_thread("thread-1")
    assert final.status == ThreadStatus.AWAITING_REVIEW
    assert len(final.task_draft.items) >= 1
    assert final.graph_ns
    assert all(i.primary_owner for i in final.task_draft.items)
    from app.llm.client import llm_client

    if not llm_client.enabled:
        assert final.token_in == 0
        assert final.token_out == 0
        assert final.latency_ms == 0


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
async def test_unassigned_owner_hits_assign_interrupt(tmp_path):
    store = MemoryStore(str(tmp_path / "assign.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("thread-a")
    text = """会议目标：发布准备
参会人：张伟、李娜
认证模块这周要完成代码评审并提交。
"""
    events = []
    async for ev in wf.run_chat(snap, text, "req-a"):
        events.append(ev)

    snap = await store.get_thread("thread-a")
    assert snap.status == ThreadStatus.AWAITING_REVIEW
    assert snap.interrupt_payload
    assert snap.interrupt_payload.get("type") == "assign_owners"
    assert any("assign_owners" in e for e in events)


@pytest.mark.asyncio
async def test_partial_assignment_reinterrupts(tmp_path):
    store = MemoryStore(str(tmp_path / "partial.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("thread-p")
    text = """会议目标：发布准备
参会人：张伟、李娜
认证模块这周要完成代码评审。
接口文档本周内需要完成更新并发布。
"""
    async for _ in wf.run_chat(snap, text, "req-p1"):
        pass
    snap = await store.get_thread("thread-p")
    assert snap.interrupt_payload and snap.interrupt_payload.get("type") == "assign_owners"
    items = snap.task_draft.items
    assert len(items) >= 2
    async for _ in wf.resume(
        snap,
        "item",
        None,
        "",
        False,
        "req-p2",
        assignments=[
            {
                "item_id": items[0].id,
                "primary_owner": "张伟",
                "secondary_owners": [],
            }
        ],
    ):
        pass
    snap = await store.get_thread("thread-p")
    assert snap.status == ThreadStatus.AWAITING_REVIEW
    assert snap.interrupt_payload.get("type") == "assign_owners"


@pytest.mark.asyncio
async def test_assign_then_review_approve(tmp_path):
    store = MemoryStore(str(tmp_path / "asg.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("thread-asg")
    text = """会议目标：发布准备
参会人：张伟、李娜
认证模块这周要完成代码评审。
"""
    async for _ in wf.run_chat(snap, text, "req-asg1"):
        pass
    snap = await store.get_thread("thread-asg")
    item_id = snap.task_draft.items[0].id
    async for _ in wf.resume(
        snap,
        "item",
        None,
        "",
        False,
        "req-asg2",
        assignments=[{"item_id": item_id, "primary_owner": "张伟", "secondary_owners": ["李娜"]}],
    ):
        pass
    snap = await store.get_thread("thread-asg")
    assert snap.interrupt_payload.get("type") == "review"
    assert snap.task_draft.items[0].primary_owner == "张伟"
    async for _ in wf.resume(snap, "item", item_id, "", True, "req-asg3"):
        pass
    snap = await store.get_thread("thread-asg")
    assert snap.status == ThreadStatus.COMPLETED
    assert "## 张伟" in snap.markdown_output or "## 李娜" in snap.markdown_output


@pytest.mark.asyncio
async def test_review_approve_applies_secondary_assignment(tmp_path):
    store = MemoryStore(str(tmp_path / "rev-sec.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("thread-rev-sec")
    async for _ in wf.run_chat(snap, SAMPLE_TEXT, "req-rev-sec-1"):
        pass
    snap = await store.get_thread("thread-rev-sec")
    assert (snap.interrupt_payload or {}).get("type") == "review"
    item = snap.task_draft.items[0]
    assert item.primary_owner
    async for _ in wf.resume(
        snap,
        "item",
        None,
        "",
        True,
        "req-rev-sec-2",
        assignments=[
            {
                "item_id": item.id,
                "primary_owner": item.primary_owner,
                "secondary_owners": ["李娜"],
            }
        ],
    ):
        pass
    snap = await store.get_thread("thread-rev-sec")
    assert snap.status == ThreadStatus.COMPLETED
    updated = next(i for i in snap.task_draft.items if i.id == item.id)
    assert updated.primary_owner == item.primary_owner
    assert "李娜" in updated.secondary_owners


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
    assert any(
        f"## {name}" in snap.markdown_output for name in ("张伟", "李娜", "王强")
    )


@pytest.mark.asyncio
async def test_idempotency(tmp_path):
    from app.memory.store import ClaimOutcome

    store = MemoryStore(str(tmp_path / "idem.db"))
    await store.init()
    await store.ensure_thread("t-idem")
    payload = {"source_text": "会议目标：幂等"}
    first = await store.claim_operation(
        request_id="req-idem", op="start", thread_id="t-idem", payload=payload, source_text=payload["source_text"]
    )
    assert first.outcome == ClaimOutcome.CLAIMED
    assert first.snapshot is not None
    saved = first.snapshot.model_copy(deep=True)
    saved.status = ThreadStatus.AWAITING_REVIEW
    assert await store.finish_request("req-idem", saved, request_status="succeeded")

    again = await store.claim_operation(
        request_id="req-idem", op="start", thread_id="t-idem", payload=payload, source_text=payload["source_text"]
    )
    assert again.outcome == ClaimOutcome.REPLAY
    assert again.snapshot is not None
    assert again.snapshot.status == ThreadStatus.AWAITING_REVIEW


@pytest.mark.asyncio
async def test_thread_busy(tmp_path):
    store = MemoryStore(str(tmp_path / "busy.db"))
    await store.init()
    snap = await store.ensure_thread("t-busy")
    from app.memory.store import ClaimOutcome

    snap.status = ThreadStatus.PLANNING
    await store.save_thread(snap)

    result = await store.claim_operation(
        request_id="new-req",
        op="start",
        thread_id="t-busy",
        payload={"source_text": "x"},
        source_text="x",
    )
    assert result.outcome == ClaimOutcome.REJECTED
    assert result.code == "THREAD_BUSY"


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
    assert "会议纪要" in md
    assert "主责" in md
    assert "同步状态" not in md


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
    assert snap.interrupt_payload.get("type") == "clarify"

    events2 = []
    async for ev in wf.run_chat(snap, text, "req-c2", clarify_response="明确部署目标"):
        events2.append(ev)

    snap = await store.get_thread("t-clarify")
    assert snap.meeting_brief.goal
    assert snap.status == ThreadStatus.AWAITING_REVIEW


@pytest.mark.asyncio
async def test_llm_fallback_with_httpx_mock(tmp_path, monkeypatch):
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
                "primary_owner": "张伟",
                "secondary_owners": [],
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


def test_non_task_sentence_not_in_draft():
    text = """会议目标：对齐节奏
周婷：先同步一下背景。
周婷：支付问题周婷负责修复，今晚完成。
"""
    brief = rule_extract_brief(text)
    draft = rule_decompose(text, brief)
    blobs = " ".join(f"{i.title} {i.description}" for i in draft.items)
    assert "背景" not in blobs
    assert any("支付" in i.title or "支付" in i.description for i in draft.items)


def test_pronoun_unresolved_reference_suppresses_missing_owner():
    text = "会议目标：跟进安排\n周婷：先把背景说一下。\n孙磊：这个跟进交给她负责，本周内完成。"
    brief = rule_extract_brief(text)
    draft = rule_decompose(text, brief)
    hit = next(i for i in draft.items if "跟进" in i.title or "跟进" in i.description)
    assert hit.unresolved_ref == "她"
    assert hit.primary_owner == ""
    result = validate_task_draft(draft, brief, text)
    codes = [i.code for i in result.error_issues if i.item_id == hit.id]
    assert "UNRESOLVED_REFERENCE" in codes
    assert "MISSING_OWNER" not in codes
    assert has_owner_errors(result)


def test_drop_items_empty_is_legal():
    brief = rule_extract_brief(SAMPLE_TEXT)
    draft = rule_decompose(SAMPLE_TEXT, brief)
    assert draft.items
    emptied = drop_items_tool(draft, [i.id for i in draft.items])
    assert emptied.items == []
    result = validate_task_draft(emptied, brief, SAMPLE_TEXT)
    assert any(i.code == "EMPTY_TASK_LIST" for i in result.error_issues)
    repaired = apply_repairs(emptied, result, SAMPLE_TEXT)
    assert repaired.items == []


@pytest.mark.asyncio
async def test_pronoun_hits_assign_interrupt(tmp_path):
    store = MemoryStore(str(tmp_path / "pronoun.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("t-pronoun")
    text = "会议目标：跟进安排\n周婷：先把背景说一下。\n孙磊：这个跟进交给她负责，本周内完成。"
    events = []
    async for ev in wf.run_chat(snap, text, "req-pronoun"):
        events.append(ev)
    snap = await store.get_thread("t-pronoun")
    assert snap.status == ThreadStatus.AWAITING_REVIEW
    assert (snap.interrupt_payload or {}).get("type") == "assign_owners"
    items = (snap.interrupt_payload or {}).get("items") or []
    assert items
    assert any(i.get("unresolved_ref") == "她" for i in items)
    assert any(i.get("evidence_quote") for i in items)


def test_sanitize_drops_quote_not_in_source():
    source = "孙磊：这个跟进交给她负责，本周内完成。"
    kept = sanitize_owner_candidates(
        [
            OwnerCandidate(
                name="周婷",
                evidence_kind=EvidenceKind.SOURCE,
                evidence_quote="这段话不在原文",
                reason="猜的",
                confidence=0.9,
            ),
            OwnerCandidate(
                name="周婷",
                evidence_kind=EvidenceKind.SOURCE,
                evidence_quote="这个跟进交给她负责",
                reason="上一句发言人是周婷",
                confidence=0.8,
            ),
        ],
        source,
        ["周婷", "孙磊"],
    )
    assert len(kept) == 1
    assert kept[0].evidence_quote == "这个跟进交给她负责"
    assert kept[0].name == "周婷"


def test_sanitize_allows_name_absent_from_quote():
    source = "孙磊：这个跟进交给她负责，本周内完成。"
    kept = sanitize_owner_candidates(
        [
            OwnerCandidate(
                name="周婷",
                evidence_kind=EvidenceKind.SOURCE,
                evidence_quote="这个跟进交给她负责",
                reason="前文周婷在发言",
                confidence=0.7,
            )
        ],
        source,
        ["周婷", "孙磊"],
    )
    assert len(kept) == 1
    assert "周婷" not in kept[0].evidence_quote


def test_sanitize_drops_secondary_matching_primary_or_existing():
    source = "周婷负责上线，孙磊配合验收，本周五完成。"
    kept = sanitize_owner_candidates(
        [
            OwnerCandidate(
                name="周婷",
                role="secondary",
                evidence_kind=EvidenceKind.SOURCE,
                evidence_quote="孙磊配合验收",
                reason="误把主责当次责",
            ),
            OwnerCandidate(
                name="马超",
                role="secondary",
                evidence_kind=EvidenceKind.SOURCE,
                evidence_quote="孙磊配合验收",
                reason="已是次责",
            ),
            OwnerCandidate(
                name="孙磊",
                role="secondary",
                evidence_kind=EvidenceKind.SOURCE,
                evidence_quote="孙磊配合验收",
                reason="配合验收",
                confidence=0.8,
            ),
        ],
        source,
        ["周婷", "孙磊", "马超"],
        primary_owner="周婷",
        existing_secondary=["马超"],
    )
    assert [c.name for c in kept] == ["孙磊"]
    assert kept[0].role == "secondary"


def test_snapshot_export_strips_owner_suggestions():
    from app.domain.models import MeetingBrief, TaskDraft, ThreadSnapshot

    item = ActionItem(
        title="跟进",
        acceptance_criteria="完成",
        evidence_quote="这个跟进交给她负责",
        owner_suggestions=[
            OwnerCandidate(name="周婷", evidence_quote="这个跟进交给她负责", reason="上下文")
        ],
    )
    snap = ThreadSnapshot(
        thread_id="t-export",
        meeting_brief=MeetingBrief(goal="跟进"),
        task_draft=TaskDraft(items=[item]),
    )
    dumped = snap.export_dict()
    assert "owner_suggestions" not in dumped["task_draft"]["items"][0]
    assert snap.task_draft.items[0].owner_suggestions


@pytest.mark.asyncio
async def test_propose_empty_on_llm_off_still_assigns(tmp_path, monkeypatch):
    monkeypatch.setattr("app.llm.client.settings.llm_provider", "none")
    monkeypatch.setattr("app.llm.client.settings.deepseek_api_key", "")
    from app.llm.client import LLMClient

    monkeypatch.setattr("app.agent.nodes.llm_client", LLMClient())
    store = MemoryStore(str(tmp_path / "propose-off.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("t-propose-off")
    text = "会议目标：跟进安排\n周婷：先把背景说一下。\n孙磊：这个跟进交给她负责，本周内完成。"
    events = []
    async for ev in wf.run_chat(snap, text, "req-propose-off"):
        events.append(ev)
    snap = await store.get_thread("t-propose-off")
    assert (snap.interrupt_payload or {}).get("type") == "assign_owners"
    assert any("propose_owners" in e for e in events)
    items = (snap.interrupt_payload or {}).get("items") or []
    assert items
    assert all(not (row.get("owner_suggestions") or []) for row in items)
    assert all(not i.primary_owner for i in snap.task_draft.items if i.unresolved_ref)


@pytest.mark.asyncio
async def test_propose_empty_on_llm_error_still_assigns(tmp_path, monkeypatch):
    import respx

    _enable_llm(monkeypatch)
    store = MemoryStore(str(tmp_path / "propose-err.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("t-propose-err")
    text = "会议目标：跟进安排\n周婷：先把背景说一下。\n孙磊：这个跟进交给她负责，本周内完成。"
    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=Response(500, json={"error": "boom"})
        )
        events = []
        async for ev in wf.run_chat(snap, text, "req-propose-err"):
            events.append(ev)
    snap = await store.get_thread("t-propose-err")
    assert snap.status == ThreadStatus.AWAITING_REVIEW
    assert (snap.interrupt_payload or {}).get("type") == "assign_owners"
    items = (snap.interrupt_payload or {}).get("items") or []
    assert items
    assert all(not (row.get("owner_suggestions") or []) for row in items)


@pytest.mark.asyncio
async def test_llm_propose_suggestions_not_written_until_resume(tmp_path, monkeypatch):
    import respx

    _enable_llm(monkeypatch)
    text = "会议目标：跟进安排\n周婷：先把背景说一下。\n孙磊：这个跟进交给她负责，本周内完成。"
    quote = "这个跟进交给她负责"
    brief_payload = {
        "brief": {
            "goal": "跟进安排",
            "stakeholders": ["周婷", "孙磊"],
            "meeting_date": "",
            "context": "",
        }
    }
    items_payload = {
        "items": [
            {
                "title": "跟进事项",
                "description": quote,
                "primary_owner": "",
                "secondary_owners": [],
                "deadline": "本周内",
                "acceptance_criteria": "跟进完成并同步结果",
                "evidence_kind": "source",
                "evidence_quote": quote,
                "unresolved_ref": "她",
                "priority": "medium",
            }
        ]
    }

    def _reply(request):
        body = json.loads(request.content)
        if body.get("tools"):
            return Response(200, json={"choices": [{"message": {"content": "done"}}]})
        sys_content = body["messages"][0]["content"]
        user_content = body["messages"][1]["content"] if len(body["messages"]) > 1 else ""
        if "解析专员" in sys_content:
            content = json.dumps(brief_payload, ensure_ascii=False)
        elif "负责人建议" in sys_content:
            item_id = ""
            try:
                marker = "任务列表:\n"
                idx = user_content.find(marker)
                rest = user_content[idx + len(marker) :]
                cut = rest.find("\n\n证据窗口:")
                pending = json.loads(rest[:cut] if cut >= 0 else rest)
                item_id = str((pending[0] or {}).get("id") or "")
            except (json.JSONDecodeError, IndexError, TypeError, ValueError):
                item_id = ""
            content = json.dumps(
                {
                    "items": [
                        {
                            "item_id": item_id,
                            "suggestions": [
                                {
                                    "name": "周婷",
                                    "role": "primary",
                                    "confidence": 0.82,
                                    "reason": "上一句发言人是周婷",
                                    "evidence_kind": "source",
                                    "evidence_quote": quote,
                                    "inference_reason": "",
                                }
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            )
        else:
            content = json.dumps(items_payload, ensure_ascii=False)
        return Response(200, json={"choices": [{"message": {"content": content}}]})

    store = MemoryStore(str(tmp_path / "propose-ok.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("t-propose-ok")
    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(side_effect=_reply)
        async for _ in wf.run_chat(snap, text, "req-propose-ok"):
            pass
    snap = await store.get_thread("t-propose-ok")
    assert (snap.interrupt_payload or {}).get("type") == "assign_owners"
    payload_items = (snap.interrupt_payload or {}).get("items") or []
    assert payload_items
    suggested = [s for row in payload_items for s in (row.get("owner_suggestions") or [])]
    assert any(s.get("name") == "周婷" for s in suggested)
    for item in snap.task_draft.items:
        if item.unresolved_ref == "她":
            assert item.primary_owner == ""
    hit = next(i for i in snap.task_draft.items if i.unresolved_ref == "她")
    async for _ in wf.resume(
        snap,
        "item",
        None,
        "",
        False,
        "req-propose-ok-2",
        assignments=[{"item_id": hit.id, "primary_owner": "周婷", "secondary_owners": []}],
    ):
        pass
    snap = await store.get_thread("t-propose-ok")
    updated = next(i for i in snap.task_draft.items if i.id == hit.id)
    assert updated.primary_owner == "周婷"


def _enable_llm(monkeypatch):
    monkeypatch.setattr("app.llm.client.settings.llm_provider", "deepseek")
    monkeypatch.setattr("app.llm.client.settings.deepseek_api_key", "sk-test")
    monkeypatch.setattr("app.llm.client.settings.deepseek_base_url", "https://api.deepseek.com")
    monkeypatch.setattr("app.llm.client.settings.deepseek_model", "deepseek-v4-flash")
    from app.llm.client import LLMClient

    llm = LLMClient()
    monkeypatch.setattr("app.agent.nodes.llm_client", llm)
    return llm


def _task_ids_from_propose_user(user_content: str) -> list[str]:
    marker = "任务列表:\n"
    idx = user_content.find(marker)
    rest = user_content[idx + len(marker) :] if idx >= 0 else user_content
    cut = rest.find("\n\n证据窗口:")
    pending = json.loads(rest[:cut] if cut >= 0 else rest)
    if not isinstance(pending, list):
        return []
    return [str((row or {}).get("id") or "") for row in pending]


@pytest.mark.asyncio
async def test_propose_secondary_on_review_when_primary_set(tmp_path, monkeypatch):
    import respx

    _enable_llm(monkeypatch)
    quote = "周婷负责完成上线验收，本周五完成。"
    text = f"会议目标：上线准备\n参会人：周婷、孙磊\n{quote}"
    brief_payload = {
        "brief": {
            "goal": "上线准备",
            "stakeholders": ["周婷", "孙磊"],
            "meeting_date": "",
            "context": "",
        }
    }
    items_payload = {
        "items": [
            {
                "title": "上线验收",
                "description": quote,
                "primary_owner": "周婷",
                "secondary_owners": [],
                "deadline": "本周五",
                "acceptance_criteria": "验收完成",
                "evidence_kind": "source",
                "evidence_quote": quote,
                "priority": "medium",
            }
        ]
    }

    hits = {"propose": 0, "ids": []}

    def _reply(request):
        body = json.loads(request.content)
        if body.get("tools"):
            return Response(200, json={"choices": [{"message": {"content": "done"}}]})
        sys_content = body["messages"][0]["content"]
        user_content = body["messages"][1]["content"] if len(body["messages"]) > 1 else ""
        if "解析专员" in sys_content:
            content = json.dumps(brief_payload, ensure_ascii=False)
        elif "负责人建议" in sys_content:
            hits["propose"] += 1
            ids = []
            try:
                ids = _task_ids_from_propose_user(user_content)
            except (json.JSONDecodeError, IndexError, TypeError, ValueError):
                ids = []
            hits["ids"] = ids
            content = json.dumps(
                {
                    "items": [
                        {
                            "item_id": item_id,
                            "suggestions": [
                                {
                                    "name": "孙磊",
                                    "role": "secondary",
                                    "confidence": 0.8,
                                    "reason": "干系人孙磊适合一起验收",
                                    "evidence_kind": "source",
                                    "evidence_quote": quote,
                                    "inference_reason": "",
                                }
                            ],
                        }
                        for item_id in ids
                    ]
                },
                ensure_ascii=False,
            )
        else:
            content = json.dumps(items_payload, ensure_ascii=False)
        return Response(200, json={"choices": [{"message": {"content": content}}]})

    store = MemoryStore(str(tmp_path / "propose-sec.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("t-propose-sec")
    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(side_effect=_reply)
        async for _ in wf.run_chat(snap, text, "req-propose-sec"):
            pass
    snap = await store.get_thread("t-propose-sec")
    assert hits["propose"] >= 1, hits
    assert (snap.interrupt_payload or {}).get("type") == "review"
    payload_items = (snap.interrupt_payload or {}).get("items") or []
    suggested = [s for row in payload_items for s in (row.get("owner_suggestions") or [])]
    assert any(s.get("name") == "孙磊" and s.get("role") == "secondary" for s in suggested)
    hit = snap.task_draft.items[0]
    assert hit.primary_owner == "周婷"
    assert hit.secondary_owners == []
    async for _ in wf.resume(
        snap,
        "item",
        None,
        "",
        True,
        "req-propose-sec-2",
        assignments=[{"item_id": hit.id, "primary_owner": "周婷", "secondary_owners": ["孙磊"]}],
    ):
        pass
    snap = await store.get_thread("t-propose-sec")
    updated = next(i for i in snap.task_draft.items if i.id == hit.id)
    assert updated.primary_owner == "周婷"
    assert updated.secondary_owners == ["孙磊"]


@pytest.mark.asyncio
async def test_propose_no_secondary_without_collab_signal(tmp_path, monkeypatch):
    import respx

    _enable_llm(monkeypatch)
    quote = "张伟负责完成用户认证模块重构，下周五前提交代码评审"
    text = f"会议目标：发布冲刺\n参会人：张伟、李娜\n{quote}"
    brief_payload = {
        "brief": {
            "goal": "发布冲刺",
            "stakeholders": ["张伟", "李娜"],
            "meeting_date": "",
            "context": "",
        }
    }
    items_payload = {
        "items": [
            {
                "title": "认证模块重构",
                "description": quote,
                "primary_owner": "张伟",
                "secondary_owners": [],
                "deadline": "下周五",
                "acceptance_criteria": "代码评审通过",
                "evidence_kind": "source",
                "evidence_quote": quote,
                "priority": "high",
            }
        ]
    }

    def _reply(request):
        body = json.loads(request.content)
        if body.get("tools"):
            return Response(200, json={"choices": [{"message": {"content": "done"}}]})
        sys_content = body["messages"][0]["content"]
        user_content = body["messages"][1]["content"] if len(body["messages"]) > 1 else ""
        if "解析专员" in sys_content:
            content = json.dumps(brief_payload, ensure_ascii=False)
        elif "负责人建议" in sys_content:
            item_id = ""
            try:
                ids = _task_ids_from_propose_user(user_content)
                item_id = ids[0] if ids else ""
            except (json.JSONDecodeError, IndexError, TypeError, ValueError):
                item_id = ""
            content = json.dumps({"items": [{"item_id": item_id, "suggestions": []}]}, ensure_ascii=False)
        else:
            content = json.dumps(items_payload, ensure_ascii=False)
        return Response(200, json={"choices": [{"message": {"content": content}}]})

    store = MemoryStore(str(tmp_path / "propose-nosec.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("t-propose-nosec")
    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(side_effect=_reply)
        async for _ in wf.run_chat(snap, text, "req-propose-nosec"):
            pass
    snap = await store.get_thread("t-propose-nosec")
    assert (snap.interrupt_payload or {}).get("type") == "review"
    payload_items = (snap.interrupt_payload or {}).get("items") or []
    suggested = [s for row in payload_items for s in (row.get("owner_suggestions") or [])]
    assert not any(s.get("role") == "secondary" for s in suggested)
    assert snap.task_draft.items[0].primary_owner == "张伟"
    assert snap.task_draft.items[0].secondary_owners == []


@pytest.mark.asyncio
async def test_llm_tool_loop_validate_then_drop(tmp_path, monkeypatch):
    import respx

    _enable_llm(monkeypatch)
    quote = "张伟负责完成用户认证模块重构，下周五前提交代码评审"
    brief_payload = {
        "brief": {
            "goal": "Q1 产品发布冲刺计划",
            "stakeholders": ["张伟", "李娜"],
            "meeting_date": "2026-02-10",
            "context": "",
        }
    }
    items_payload = {
        "items": [
            {
                "id": "keep-1",
                "title": "认证模块重构",
                "description": quote,
                "primary_owner": "张伟",
                "secondary_owners": [],
                "deadline": "下周五",
                "acceptance_criteria": "代码评审通过",
                "evidence_kind": "source",
                "evidence_quote": quote,
                "priority": "high",
            },
            {
                "id": "junk-1",
                "title": "今天主要聊进度",
                "description": "今天主要聊进度",
                "primary_owner": "张伟",
                "secondary_owners": [],
                "deadline": "本周",
                "acceptance_criteria": "同步完成",
                "evidence_kind": "source",
                "evidence_quote": "会议日期：2026-02-10",
                "priority": "low",
            },
        ]
    }
    tool_names: list[str] = []

    def _reply(request):
        body = json.loads(request.content)
        if body.get("tools"):
            if "validate_task_draft" not in tool_names:
                tool_names.append("validate_task_draft")
                return Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "content": "",
                                    "tool_calls": [
                                        {
                                            "id": "c1",
                                            "type": "function",
                                            "function": {
                                                "name": "validate_task_draft",
                                                "arguments": "{}",
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            if "drop_items" not in tool_names:
                tool_names.append("drop_items")
                return Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "content": "",
                                    "tool_calls": [
                                        {
                                            "id": "c2",
                                            "type": "function",
                                            "function": {
                                                "name": "drop_items",
                                                "arguments": '{"item_ids": ["junk-1"]}',
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            return Response(200, json={"choices": [{"message": {"content": "done"}}]})
        sys_content = body["messages"][0]["content"]
        if "解析专员" in sys_content:
            content = json.dumps(brief_payload, ensure_ascii=False)
        else:
            content = json.dumps(items_payload, ensure_ascii=False)
        return Response(200, json={"choices": [{"message": {"content": content}}]})

    store = MemoryStore(str(tmp_path / "tool-drop.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("t-drop")
    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(side_effect=_reply)
        async for _ in wf.run_chat(snap, SAMPLE_TEXT, "req-drop"):
            pass
    snap = await store.get_thread("t-drop")
    ids = [i.id for i in snap.task_draft.items]
    assert "junk-1" not in ids
    assert any(i.title == "认证模块重构" for i in snap.task_draft.items)
    assert tool_names == ["validate_task_draft", "drop_items"]


@pytest.mark.asyncio
async def test_llm_tool_loop_stall_stops(tmp_path, monkeypatch):
    import respx

    monkeypatch.setattr("app.config.settings.max_tool_rounds", 4)
    monkeypatch.setattr("app.config.settings.max_tool_stall", 2)
    monkeypatch.setattr("app.agent.nodes.settings.max_tool_rounds", 4)
    monkeypatch.setattr("app.agent.nodes.settings.max_tool_stall", 2)
    _enable_llm(monkeypatch)
    quote = "张伟负责完成用户认证模块重构，下周五前提交代码评审"
    brief_payload = {
        "brief": {
            "goal": "Q1",
            "stakeholders": ["张伟"],
            "meeting_date": "",
            "context": "",
        }
    }
    items_payload = {
        "items": [
            {
                "id": "bad-1",
                "title": "认证模块重构",
                "description": quote,
                "primary_owner": "外人",
                "secondary_owners": [],
                "deadline": "本周",
                "acceptance_criteria": "完成",
                "evidence_kind": "source",
                "evidence_quote": quote,
                "priority": "medium",
            }
        ]
    }
    tool_calls = {"n": 0}

    def _reply(request):
        body = json.loads(request.content)
        if body.get("tools"):
            tool_calls["n"] += 1
            return Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": f"s{tool_calls['n']}",
                                        "type": "function",
                                        "function": {
                                            "name": "repair_task_draft",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        sys_content = body["messages"][0]["content"]
        if "解析专员" in sys_content:
            content = json.dumps(brief_payload, ensure_ascii=False)
        else:
            content = json.dumps(items_payload, ensure_ascii=False)
        return Response(200, json={"choices": [{"message": {"content": content}}]})

    store = MemoryStore(str(tmp_path / "stall.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("t-stall")
    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(side_effect=_reply)
        async for _ in wf.run_chat(snap, SAMPLE_TEXT, "req-stall"):
            pass
    assert tool_calls["n"] <= 4
    assert tool_calls["n"] >= 2
    snap = await store.get_thread("t-stall")
    assert snap.status == ThreadStatus.AWAITING_REVIEW


@pytest.mark.asyncio
async def test_review_drop_by_id_rewrites_draft(tmp_path):
    store = MemoryStore(str(tmp_path / "drop-id.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("t-drop-id")
    async for _ in wf.run_chat(snap, SAMPLE_TEXT, "req-drop-id-1"):
        pass
    snap = await store.get_thread("t-drop-id")
    assert snap.interrupt_payload and snap.interrupt_payload.get("type") == "review"
    items = snap.task_draft.items
    assert len(items) >= 3
    first_ids = [i.id for i in items]
    payload_indexes = [row.get("index") for row in (snap.interrupt_payload.get("items") or [])]
    assert payload_indexes == list(range(1, len(items) + 1))
    drop_id = items[1].id
    version = snap.task_draft.version
    interrupt_before = snap.interrupt_id

    async for _ in wf.resume(
        snap,
        "drop",
        None,
        "",
        False,
        "req-drop-id-2",
        drop_item_ids=[drop_id],
    ):
        pass
    snap = await store.get_thread("t-drop-id")
    remaining = [i.id for i in snap.task_draft.items]
    assert drop_id not in remaining
    assert remaining == [i for i in first_ids if i != drop_id]
    assert snap.task_draft.version == version + 1
    assert snap.status == ThreadStatus.AWAITING_REVIEW
    assert snap.interrupt_payload.get("type") == "review"
    assert snap.interrupt_id != interrupt_before
    new_indexes = [row.get("index") for row in (snap.interrupt_payload.get("items") or [])]
    assert new_indexes == list(range(1, len(remaining) + 1))
    assert snap.review.dropped_item_ids == [drop_id]


@pytest.mark.asyncio
async def test_review_nl_drop_second_item(tmp_path):
    store = MemoryStore(str(tmp_path / "drop-nl.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("t-drop-nl")
    async for _ in wf.run_chat(snap, SAMPLE_TEXT, "req-drop-nl-1"):
        pass
    snap = await store.get_thread("t-drop-nl")
    second = snap.task_draft.items[1].id
    async for _ in wf.resume(snap, "item", None, "第 2 条不是任务，删掉", False, "req-drop-nl-2"):
        pass
    snap = await store.get_thread("t-drop-nl")
    assert second not in [i.id for i in snap.task_draft.items]
    assert snap.review.revision_scope == "drop"


@pytest.mark.asyncio
async def test_review_drop_idempotent_replay(tmp_path):
    store = MemoryStore(str(tmp_path / "drop-idem.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("t-drop-idem")
    async for _ in wf.run_chat(snap, SAMPLE_TEXT, "req-drop-idem-1"):
        pass
    snap = await store.get_thread("t-drop-idem")
    drop_id = snap.task_draft.items[1].id
    async for _ in wf.resume(
        snap, "drop", None, "", False, "req-drop-idem-2", drop_item_ids=[drop_id]
    ):
        pass
    after = await store.get_thread("t-drop-idem")
    count = len(after.task_draft.items)
    from app.memory.store import ClaimOutcome

    again = await store.claim_operation(
        request_id="req-drop-idem-2",
        op="resume",
        thread_id=after.thread_id,
        payload={
            "revision_scope": "drop",
            "target_item_id": None,
            "feedback": "",
            "approve": False,
            "assignments": [],
            "drop_item_ids": [drop_id],
            "interrupt_id": snap.interrupt_id,
            "interrupt_type": str((snap.interrupt_payload or {}).get("type") or ""),
            "item_patch": None,
        },
        interrupt_id=snap.interrupt_id,
        interrupt_type=str((snap.interrupt_payload or {}).get("type") or ""),
    )
    assert again.outcome == ClaimOutcome.REPLAY
    assert again.snapshot is not None
    assert len(again.snapshot.task_draft.items) == count
    assert drop_id not in [i.id for i in again.snapshot.task_draft.items]


@pytest.mark.asyncio
async def test_approve_empty_list_exports(tmp_path):
    store = MemoryStore(str(tmp_path / "empty-ok.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("t-empty")
    async for _ in wf.run_chat(snap, SAMPLE_TEXT, "req-empty-1"):
        pass
    snap = await store.get_thread("t-empty")
    ids = [i.id for i in snap.task_draft.items]
    async for _ in wf.resume(snap, "drop", None, "", False, "req-empty-2", drop_item_ids=ids):
        pass
    snap = await store.get_thread("t-empty")
    assert snap.task_draft.items == []
    assert snap.status == ThreadStatus.AWAITING_REVIEW
    async for _ in wf.resume(snap, "item", None, "", True, "req-empty-3"):
        pass
    snap = await store.get_thread("t-empty")
    assert snap.status == ThreadStatus.COMPLETED
    assert "本次无任务" in snap.markdown_output


@pytest.mark.asyncio
async def test_long_text_keeps_tail_and_rules(tmp_path, monkeypatch):
    import respx
    from httpx import Response

    monkeypatch.setattr("app.llm.client.settings.llm_provider", "deepseek")
    monkeypatch.setattr("app.llm.client.settings.deepseek_api_key", "sk-test")
    monkeypatch.setattr("app.llm.client.settings.deepseek_base_url", "https://api.deepseek.com")
    monkeypatch.setattr("app.llm.client.settings.deepseek_model", "deepseek-v4-flash")
    monkeypatch.setattr("app.config.settings.llm_chunk_chars", 400)
    monkeypatch.setattr("app.config.settings.llm_chunk_overlap", 40)
    monkeypatch.setattr("app.agent.nodes.settings.llm_chunk_chars", 400)
    monkeypatch.setattr("app.agent.nodes.settings.llm_chunk_overlap", 40)

    from app.llm.client import LLMClient

    llm = LLMClient()
    monkeypatch.setattr("app.agent.nodes.llm_client", llm)

    head = "张伟负责完成开头模块，本周内提交。"
    tail_weak = "李娜：认证模块这周也得有人看一下。"
    padding = "这是背景说明，不含动作。" * 80
    text = f"会议目标：长会同步\n参会人：张伟、李娜\n{head}\n{padding}\n{tail_weak}"

    brief_payload = {
        "brief": {
            "goal": "长会同步",
            "stakeholders": ["张伟", "李娜"],
            "meeting_date": "",
            "context": "",
        }
    }

    def _reply(request):
        body = json.loads(request.content)
        sys_content = body["messages"][0]["content"]
        user = body["messages"][-1]["content"]
        if "解析专员" in sys_content:
            return Response(200, json={"choices": [{"message": {"content": json.dumps(brief_payload, ensure_ascii=False)}}]})
        items = []
        if "开头模块" in user:
            items.append(
                {
                    "title": "开头模块",
                    "description": head,
                    "primary_owner": "张伟",
                    "secondary_owners": [],
                    "deadline": "本周",
                    "acceptance_criteria": "提交完成",
                    "evidence_kind": "source",
                    "evidence_quote": head,
                }
            )
        if "看一下" in user:
            items.append(
                {
                    "title": "认证模块跟进",
                    "description": tail_weak,
                    "primary_owner": "李娜",
                    "secondary_owners": [],
                    "deadline": "本周",
                    "acceptance_criteria": "有人看过认证模块",
                    "evidence_kind": "source",
                    "evidence_quote": tail_weak,
                }
            )
        return Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"items": items}, ensure_ascii=False)}}]},
        )

    store = MemoryStore(str(tmp_path / "long.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("t-long")
    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(side_effect=_reply)
        async for _ in wf.run_chat(snap, text, "req-long"):
            pass
    snap = await store.get_thread("t-long")
    titles = [i.title for i in snap.task_draft.items]
    blobs = " ".join(f"{i.title} {i.description}" for i in snap.task_draft.items)
    assert "开头" in blobs
    assert "认证模块" in blobs or "看一下" in blobs
    assert titles.count("开头模块") <= 1
    for item in snap.task_draft.items:
        if item.evidence_quote:
            assert item.evidence_quote in text

