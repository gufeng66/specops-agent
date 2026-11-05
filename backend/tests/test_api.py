"""HTTP 层：SSE、409 忙碌、resume 批准/删除、原文硬上限。"""

import json

import pytest
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.memory import MemorySaver

from app.agent.graph import build_graph
from app.agent.workflow import SpecOpsWorkflow
from app.domain.models import ThreadStatus
from app.main import app
from app.memory.store import MemoryStore

SAMPLE = """会议目标：测试发布
参会人：张三
张三负责完成部署，本周内上线。"""


def sse_payloads(text: str, event: str) -> list[dict]:
    current = ""
    found = []
    for line in text.splitlines():
        if line.startswith("event:"):
            current = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and current == event:
            found.append(json.loads(line.split(":", 1)[1].strip()))
    return found


@pytest.fixture
async def api_client(tmp_path, monkeypatch):
    db = tmp_path / "api.db"
    store = MemoryStore(str(db))
    await store.init()
    graph = build_graph(MemorySaver())
    wf = SpecOpsWorkflow(store, graph=graph)
    monkeypatch.setattr("app.memory.store.memory_store", store)
    monkeypatch.setattr("app.api.chat.memory_store", store)
    monkeypatch.setattr("app.api.chat.workflow", wf)
    monkeypatch.setattr("app.api.review.memory_store", store)
    monkeypatch.setattr("app.api.review.workflow", wf)
    monkeypatch.setattr("app.api.threads.memory_store", store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, store


@pytest.mark.asyncio
async def test_chat_stream_returns_sse(api_client):
    client, _ = api_client
    resp = await client.post(
        "/api/v1/chat/stream",
        json={"source_text": SAMPLE, "request_id": "req-api-1"},
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")
    assert resp.headers.get("x-accel-buffering") == "no"
    assert "no-cache" in (resp.headers.get("cache-control") or "")
    body = resp.text
    assert "event: progress" in body
    assert "event: tool_result" in body
    assert "validate_task_draft" in body or "repair_task_draft" in body
    assert "event: interrupt" in body or "event: result" in body


@pytest.mark.asyncio
async def test_thread_busy_409(api_client):
    client, store = api_client
    snap = await store.ensure_thread("busy-thread")
    snap.status = ThreadStatus.AWAITING_REVIEW
    await store.save_thread(snap)

    resp = await client.post(
        "/api/v1/chat/stream",
        json={"source_text": SAMPLE, "thread_id": "busy-thread", "request_id": "req-busy"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "THREAD_BUSY"


@pytest.mark.asyncio
async def test_document_revise_scope(tmp_path):
    store = MemoryStore(str(tmp_path / "doc.db"))
    await store.init()
    wf = SpecOpsWorkflow(store, graph=build_graph(MemorySaver()))
    snap = await store.ensure_thread("doc-thread")

    async for _ in wf.run_chat(snap, SAMPLE, "req-doc-1"):
        pass
    snap = await store.get_thread("doc-thread")

    async for _ in wf.resume(snap, "document", None, "删除部署", False, "req-doc-2"):
        pass
    final = await store.get_thread("doc-thread")
    assert "文档修订" not in final.source_text
    assert final.revision_notes == ["删除部署"]
    assert all("部署" not in item.title and "部署" not in item.description for item in final.task_draft.items)
    assert final.status == ThreadStatus.AWAITING_REVIEW


@pytest.mark.asyncio
async def test_resume_approve_via_api(api_client):
    client, store = api_client
    resp = await client.post(
        "/api/v1/chat/stream",
        json={"source_text": SAMPLE, "request_id": "req-appr-1"},
    )
    assert resp.status_code == 200
    interrupts = sse_payloads(resp.text, "interrupt")
    assert interrupts
    thread_id = sse_payloads(resp.text, "metadata")[0]["thread_id"]
    snap = await store.get_thread(thread_id)
    assert snap
    item_id = snap.task_draft.items[0].id if snap.task_draft.items else None

    resp2 = await client.post(
        f"/api/v1/runs/{snap.run_id}/resume",
        json={
            "request_id": "req-appr-2",
            "approve": True,
            "revision_scope": "item",
            "target_item_id": item_id,
            "feedback": "",
            "interrupt_id": interrupts[-1]["interrupt_id"],
            "interrupt_type": interrupts[-1]["type"],
        },
    )
    assert resp2.status_code == 200
    assert "event: result" in resp2.text or "event: progress" in resp2.text
    final = await store.get_thread(snap.thread_id)
    assert final.status == ThreadStatus.COMPLETED


@pytest.mark.asyncio
async def test_resume_nl_drop_without_target_id(api_client):
    client, store = api_client
    resp = await client.post(
        "/api/v1/chat/stream",
        json={"source_text": SAMPLE, "request_id": "req-nldrop-1"},
    )
    assert resp.status_code == 200
    interrupts = sse_payloads(resp.text, "interrupt")
    thread_id = sse_payloads(resp.text, "metadata")[0]["thread_id"]
    snap = await store.get_thread(thread_id)
    assert snap and snap.task_draft.items
    before_ids = [i.id for i in snap.task_draft.items]
    resp2 = await client.post(
        f"/api/v1/runs/{snap.run_id}/resume",
        json={
            "request_id": "req-nldrop-2",
            "revision_scope": "item",
            "feedback": "第 1 条不是任务，删掉",
            "interrupt_id": interrupts[-1]["interrupt_id"],
            "interrupt_type": interrupts[-1]["type"],
        },
    )
    assert resp2.status_code == 200
    after = await store.get_thread(snap.thread_id)
    assert after.status == ThreadStatus.AWAITING_REVIEW
    assert before_ids[0] not in [i.id for i in after.task_draft.items]


async def _open_interrupt(client, store, text: str, request_id: str):
    resp = await client.post(
        "/api/v1/chat/stream",
        json={"source_text": text, "request_id": request_id},
    )
    assert resp.status_code == 200
    interrupts = sse_payloads(resp.text, "interrupt")
    assert interrupts
    thread_id = sse_payloads(resp.text, "metadata")[0]["thread_id"]
    snap = await store.get_thread(thread_id)
    assert snap
    return snap, interrupts[-1]


@pytest.mark.asyncio
async def test_resume_http_replay_returns_the_first_result(api_client):
    client, store = api_client
    approved, interrupt = await _open_interrupt(client, store, SAMPLE, "req-replay-approve-1")
    body = {
        "request_id": "req-replay-approve-2",
        "approve": True,
        "revision_scope": "item",
        "target_item_id": approved.task_draft.items[0].id,
        "feedback": "",
        "interrupt_id": interrupt["interrupt_id"],
        "interrupt_type": interrupt["type"],
    }
    first = await client.post(f"/api/v1/runs/{approved.run_id}/resume", json=body)
    second = await client.post(f"/api/v1/runs/{approved.run_id}/resume", json=body)
    assert first.status_code == 200
    assert second.status_code == 200
    assert sse_payloads(second.text, "metadata")[0]["idempotent"] is True
    assert "idempotent" in second.text
    final = await store.get_thread(approved.thread_id)
    assert final.status == ThreadStatus.COMPLETED
    assert final.run_id == sse_payloads(second.text, "result")[0]["run_id"]

    patched, review = await _open_interrupt(client, store, SAMPLE, "req-replay-patch-1")
    item = patched.task_draft.items[0]
    patch_body = {
        "request_id": "req-replay-patch-2",
        "approve": False,
        "revision_scope": "item",
        "target_item_id": item.id,
        "feedback": "",
        "interrupt_id": review["interrupt_id"],
        "interrupt_type": review["type"],
        "item_patch": {"title": "改过的标题", "secondary_owners": ["李四"]},
    }
    patched_first = await client.post(f"/api/v1/runs/{patched.run_id}/resume", json=patch_body)
    patched_again = await client.post(f"/api/v1/runs/{patched.run_id}/resume", json=patch_body)
    assert patched_first.status_code == 200
    assert patched_again.status_code == 200
    assert sse_payloads(patched_again.text, "metadata")[0]["idempotent"] is True
    after_patch = await store.get_thread(patched.thread_id)
    titles = [row.title for row in after_patch.task_draft.items]
    assert titles.count("改过的标题") == 1

    assign_text = "会议目标：发布\n参会人：张三、李四\n本周完成认证模块检查。"
    assigned, assign_interrupt = await _open_interrupt(client, store, assign_text, "req-replay-assign-1")
    assert assign_interrupt["type"] == "assign_owners"
    item_id = assigned.task_draft.items[0].id
    assign_body = {
        "request_id": "req-replay-assign-2",
        "approve": False,
        "revision_scope": "item",
        "feedback": "",
        "interrupt_id": assign_interrupt["interrupt_id"],
        "interrupt_type": "assign_owners",
        "assignments": [
            {
                "item_id": item_id,
                "primary_owner": "张三",
                "secondary_owners": ["李四"],
                "confirm_owner": True,
            }
        ],
    }
    assign_first = await client.post(f"/api/v1/runs/{assigned.run_id}/resume", json=assign_body)
    assign_again = await client.post(f"/api/v1/runs/{assigned.run_id}/resume", json=assign_body)
    assert assign_first.status_code == 200
    assert assign_again.status_code == 200
    assert sse_payloads(assign_again.text, "metadata")[0]["idempotent"] is True
    after_assign = await store.get_thread(assigned.thread_id)
    assert after_assign.task_draft.items[0].primary_owner == "张三"


@pytest.mark.asyncio
async def test_source_hard_limit_422(api_client, monkeypatch):
    monkeypatch.setattr("app.api.schemas.settings.source_hard_limit_chars", 40)
    client, _ = api_client
    resp = await client.post(
        "/api/v1/chat/stream",
        json={"source_text": "会议目标：超长输入。" + "哈" * 80, "request_id": "req-huge"},
    )
    assert resp.status_code == 422
