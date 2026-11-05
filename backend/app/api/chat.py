"""POST /chat/stream：开跑或澄清续跑。同一 request_id 在认领事务里决定回放、执行中或冲突。"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.agent.events import SSE_HEADERS, emit_done, emit_metadata, emit_result
from app.agent.workflow import SpecOpsWorkflow
from app.api.schemas import ChatStreamRequest
from app.errors import conflict_http
from app.memory.store import ClaimOutcome, memory_store

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])
workflow = SpecOpsWorkflow(memory_store)


async def _stream_events(generator):
    """把 workflow 异步生成器原样交给 StreamingResponse。"""
    async for event in generator:
        yield event


def _replay(existing):
    data = existing.model_dump(mode="json")
    data["idempotent"] = True

    async def idempotent_stream():
        yield emit_metadata(
            {"run_id": existing.run_id, "thread_id": existing.thread_id, "idempotent": True}
        )
        yield emit_result(data)
        yield emit_done()

    return StreamingResponse(idempotent_stream(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/stream")
async def chat_stream(body: ChatStreamRequest):
    """SSE 开跑。认领事务通过后才执行图。"""
    op = "clarify" if body.clarify_response else "start"
    payload = {
        "source_text": body.source_text,
        "clarify_response": body.clarify_response,
    }
    interrupt_id = ""
    interrupt_type = ""
    if op == "clarify":
        interrupt_id = body.interrupt_id or ""
        interrupt_type = "clarify"
        payload["interrupt_id"] = interrupt_id
        payload["interrupt_type"] = interrupt_type
    result = await memory_store.claim_operation(
        request_id=body.request_id,
        op=op,
        thread_id=body.thread_id,
        payload=payload,
        interrupt_id=interrupt_id,
        interrupt_type=interrupt_type,
        source_text=body.source_text,
    )
    if result.outcome == ClaimOutcome.REPLAY and result.snapshot:
        return _replay(result.snapshot)
    if result.outcome != ClaimOutcome.CLAIMED or result.snapshot is None:
        raise conflict_http(result.code or "REQUEST_REJECTED", result.message or "请求被拒绝")

    return StreamingResponse(
        _stream_events(
            workflow.run_chat(
                result.snapshot,
                body.source_text,
                body.request_id,
                clarify_response=body.clarify_response,
                interrupt_id=interrupt_id or None,
                interrupt_type=interrupt_type or None,
            )
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
