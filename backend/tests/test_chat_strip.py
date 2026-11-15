"""chat_stream 澄清文本标准化：首尾空格不能让 op / 幂等 digest / workflow 入参分叉。

对应 m01951 第 10 项：chat.py 先算 clarify_text = (body.clarify_response or '').strip() or None，
判断 start/clarify、claim_operation 的 payload、workflow.run_chat 参数必须用同一个值。
用 fake store / fake workflow 把 handler 与真实 DB、图隔离，只观察传给它们的参数。
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.domain.models import ThreadSnapshot
from app.main import app
from app.memory.store import ClaimOutcome, ClaimResult, operation_digest

SAMPLE = "会议目标：Q1 发布"


class _FakeStore:
    def __init__(self):
        self.claims: list[dict] = []

    async def claim_operation(self, **kwargs):
        self.claims.append(kwargs)
        return ClaimResult(
            outcome=ClaimOutcome.CLAIMED,
            snapshot=ThreadSnapshot(thread_id=kwargs.get("thread_id") or "t-strip"),
        )


class _FakeWorkflow:
    def __init__(self):
        self.calls: list[dict] = []

    def run_chat(self, snapshot, source_text, request_id, **kwargs):
        self.calls.append({"source_text": source_text, "request_id": request_id, **kwargs})

        async def _empty():
            if False:  # pragma: no cover - 只为了造一个不产出事件的异步生成器
                yield None

        return _empty()


@pytest.fixture
def fakes(monkeypatch):
    store = _FakeStore()
    wf = _FakeWorkflow()
    monkeypatch.setattr("app.api.chat.memory_store", store)
    monkeypatch.setattr("app.api.chat.workflow", wf)
    return store, wf


async def _post(client, *, clarify, request_id, interrupt_id="iid-1"):
    return await client.post(
        "/api/v1/chat/stream",
        json={
            "source_text": SAMPLE,
            "request_id": request_id,
            "clarify_response": clarify,
            "interrupt_id": interrupt_id,
        },
    )


@pytest.mark.asyncio
async def test_clarify_whitespace_is_stripped_everywhere(fakes):
    store, wf = fakes
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await _post(
            client, clarify="  目标：Q1 发布冲刺  ", request_id="req-strip-1"
        )
    assert resp.status_code == 200

    claim = store.claims[-1]
    assert claim["op"] == "clarify"
    # digest 用的 payload 里必须是 strip 后的值，而不是带空格的原串。
    assert claim["payload"]["clarify_response"] == "目标：Q1 发布冲刺"
    assert claim["payload"]["interrupt_id"] == "iid-1"
    assert claim["payload"]["interrupt_type"] == "clarify"
    # workflow 拿到的也是同一个标准化值。
    assert wf.calls[-1]["clarify_response"] == "目标：Q1 发布冲刺"


@pytest.mark.asyncio
async def test_clarify_whitespace_does_not_change_digest(fakes):
    """同一语义的澄清，首尾空格不同也应产生相同幂等 digest。"""
    store, _ = fakes
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await _post(client, clarify="目标：Q1 发布冲刺", request_id="req-strip-a")
        await _post(client, clarify="   目标：Q1 发布冲刺 \t", request_id="req-strip-b")

    a, b = store.claims[0], store.claims[1]
    assert a["payload"]["clarify_response"] == b["payload"]["clarify_response"]
    assert operation_digest(a["op"], a.get("thread_id"), a["payload"]) == operation_digest(
        b["op"], b.get("thread_id"), b["payload"]
    )


@pytest.mark.asyncio
async def test_no_clarify_still_starts(fakes):
    store, wf = fakes
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/chat/stream",
            json={"source_text": SAMPLE, "request_id": "req-start"},
        )
    assert resp.status_code == 200
    assert store.claims[-1]["op"] == "start"
    assert store.claims[-1]["payload"]["clarify_response"] is None
    assert wf.calls[-1]["clarify_response"] is None
