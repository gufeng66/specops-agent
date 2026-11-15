"""HTTP schema 层：空白澄清、override 理由的 422 约束。

m00001 要求「API schema 拒绝空白 clarify_response」「节点层再次校验」；
节点层由 test_clarify_guard.py / test_approval_override.py 覆盖，本文件覆盖
schemas.py 的 model_validator 与 FastAPI 是否把它翻成 422。
"""

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api.schemas import ChatStreamRequest, ResumeRequest
from app.main import app

# ---- 纯模型：精确断言校验器拒绝的理由 ----

@pytest.mark.parametrize("blank", ["", " ", "   ", "\n", "\t", " \n\t "])
def test_blank_clarify_response_rejected(blank):
    with pytest.raises(ValidationError) as exc:
        ChatStreamRequest(
            source_text="会议目标：Q1 发布",
            request_id="req-blank",
            clarify_response=blank,
            interrupt_id="iid-1",  # 带上 interrupt_id，单独隔离「空白」这条规则
        )
    assert "clarify_response must not be blank" in str(exc.value)


def test_clarify_without_interrupt_id_rejected():
    with pytest.raises(ValidationError) as exc:
        ChatStreamRequest(
            source_text="会议目标：Q1 发布",
            request_id="req-no-iid",
            clarify_response="目标是 Q1 发布冲刺",
        )
    assert "interrupt_id is required when clarifying" in str(exc.value)


def test_valid_clarify_passes():
    req = ChatStreamRequest(
        source_text="会议目标：Q1 发布",
        request_id="req-ok",
        clarify_response="目标是 Q1 发布冲刺",
        interrupt_id="iid-1",
    )
    assert req.clarify_response == "目标是 Q1 发布冲刺"


@pytest.mark.parametrize("blank", ["", " ", "\n", " \t "])
def test_blank_override_reason_rejected(blank):
    with pytest.raises(ValidationError) as exc:
        ResumeRequest(
            request_id="req-ovr-blank",
            interrupt_id="iid-1",
            approve=True,
            override_reason=blank,
        )
    assert "override_reason must not be blank" in str(exc.value)


def test_override_reason_requires_approve():
    with pytest.raises(ValidationError) as exc:
        ResumeRequest(
            request_id="req-ovr-noapprove",
            interrupt_id="iid-1",
            approve=False,
            override_reason="确实需要放行",
        )
    assert "override_reason requires approve=true" in str(exc.value)


def test_valid_override_passes():
    req = ResumeRequest(
        request_id="req-ovr-ok",
        interrupt_id="iid-1",
        approve=True,
        override_reason="原文已改写，我确认语义一致",
        operator_claim="张三（本地审核）",
    )
    assert req.override_reason == "原文已改写，我确认语义一致"


@pytest.mark.parametrize("length", [499, 500])
def test_override_reason_limit_accepts_trimmed_boundary(length):
    req = ResumeRequest(
        request_id=f"req-ovr-{length}",
        interrupt_id="iid-1",
        approve=True,
        override_reason=" " + "a" * length + " ",
        operator_claim=" 审核人 ",
    )
    assert req.override_reason == "a" * length
    assert req.operator_claim == "审核人"


def test_override_reason_limit_rejects_501_and_operator_claim_rejects_201():
    with pytest.raises(ValidationError, match="override_reason must be at most 500"):
        ResumeRequest(
            request_id="req-ovr-501",
            interrupt_id="iid-1",
            approve=True,
            override_reason="a" * 501,
        )
    with pytest.raises(ValidationError, match="operator_claim must be at most 200"):
        ResumeRequest(
            request_id="req-claim-201",
            interrupt_id="iid-1",
            operator_claim="a" * 201,
        )


def test_resume_requires_interrupt_id():
    with pytest.raises(ValidationError) as exc:
        ResumeRequest(request_id="req-no-iid", approve=True)
    assert "interrupt_id is required" in str(exc.value)


# ---- HTTP：确认 FastAPI 把这些约束翻成 422，而非 500 ----
# 请求体校验发生在进入 handler 之前，因此不需要起 workflow / store。

@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_http_blank_clarify_returns_422(client):
    resp = await client.post(
        "/api/v1/chat/stream",
        json={
            "source_text": "会议目标：Q1 发布",
            "request_id": "req-http-blank",
            "clarify_response": "   ",
            "interrupt_id": "iid-1",
        },
    )
    assert resp.status_code == 422
    assert "clarify_response must not be blank" in resp.text


@pytest.mark.asyncio
async def test_http_clarify_without_interrupt_id_returns_422(client):
    resp = await client.post(
        "/api/v1/chat/stream",
        json={
            "source_text": "会议目标：Q1 发布",
            "request_id": "req-http-noiid",
            "clarify_response": "目标是 Q1 发布冲刺",
        },
    )
    assert resp.status_code == 422
    assert "interrupt_id is required when clarifying" in resp.text


@pytest.mark.asyncio
async def test_http_blank_override_returns_422(client):
    resp = await client.post(
        "/api/v1/runs/any-run/resume",
        json={
            "request_id": "req-http-ovr-blank",
            "interrupt_id": "iid-1",
            "approve": True,
            "override_reason": "  ",
        },
    )
    assert resp.status_code == 422
    assert "override_reason must not be blank" in resp.text


@pytest.mark.asyncio
async def test_http_override_without_approve_returns_422(client):
    resp = await client.post(
        "/api/v1/runs/any-run/resume",
        json={
            "request_id": "req-http-ovr-noapprove",
            "interrupt_id": "iid-1",
            "approve": False,
            "override_reason": "确实需要放行",
        },
    )
    assert resp.status_code == 422
    assert "override_reason requires approve=true" in resp.text
