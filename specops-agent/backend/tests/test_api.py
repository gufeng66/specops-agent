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
    body = resp.text
    assert "event: progress" in body
    assert "event: tool_result" in body or "search_playbooks" in body
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

    async for _ in wf.resume(snap, "document", None, "更新会议目标", False, "req-doc-2"):
        pass
    final = await store.get_thread("doc-thread")
    assert "文档修订" in final.source_text
    assert final.status == ThreadStatus.AWAITING_REVIEW


@pytest.mark.asyncio
async def test_resume_approve_via_api(api_client):
    client, store = api_client
    resp = await client.post(
        "/api/v1/chat/stream",
        json={"source_text": SAMPLE, "request_id": "req-appr-1"},
    )
    assert resp.status_code == 200
    # Extract run_id from SSE
    run_id = None
    thread_id = None
    for line in resp.text.splitlines():
        if line.startswith("data:") and "run_id" in line:
            import json

            data = json.loads(line[5:].strip())
            run_id = data.get("run_id") or run_id
            thread_id = data.get("thread_id") or thread_id
            if data.get("task_draft"):
                break

    snap = None
    if thread_id:
        snap = await store.get_thread(thread_id)
        run_id = snap.run_id
    assert run_id and snap
    item_id = snap.task_draft.items[0].id if snap.task_draft.items else None

    resp2 = await client.post(
        f"/api/v1/runs/{run_id}/resume",
        json={
            "request_id": "req-appr-2",
            "approve": True,
            "revision_scope": "item",
            "target_item_id": item_id,
            "feedback": "",
        },
    )
    assert resp2.status_code == 200
    assert "event: result" in resp2.text or "event: progress" in resp2.text
    final = await store.get_thread(snap.thread_id)
    assert final.status == ThreadStatus.COMPLETED
