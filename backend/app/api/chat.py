"""POST /chat/stream：开跑或澄清续跑。同一 request_id 幂等返回第一次结果。"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.agent.events import SSE_HEADERS, emit_done, emit_metadata, emit_result
from app.agent.workflow import SpecOpsWorkflow
from app.api.schemas import ChatStreamRequest
from app.domain.models import ThreadStatus
from app.errors import ThreadBusyError, thread_busy_http
from app.memory.store import memory_store

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])
workflow = SpecOpsWorkflow(memory_store)


async def _stream_events(generator):
    """把 workflow 异步生成器原样交给 StreamingResponse。"""
    async for event in generator:
        yield event


@router.post("/stream")
async def chat_stream(body: ChatStreamRequest):
    """SSE 开跑。同一 request_id 幂等回放；澄清允许在 awaiting_review 续跑。"""
    # Allow clarify resume on AWAITING_REVIEW; still block concurrent planning
    busy_thread = None if body.clarify_response else body.thread_id
    try:
        claimed, existing = await memory_store.claim_request(body.request_id, busy_thread)
    except ThreadBusyError as exc:
        raise thread_busy_http(exc.thread_id) from exc

    if not claimed and existing:
        data = existing.model_dump(mode="json")
        data["idempotent"] = True

        async def idempotent_stream():
            yield emit_metadata(
                {"run_id": existing.run_id, "thread_id": existing.thread_id, "idempotent": True}
            )
            yield emit_result(data)
            yield emit_done()

        return StreamingResponse(
            idempotent_stream(), media_type="text/event-stream", headers=SSE_HEADERS
        )

    thread_id = body.thread_id
    if thread_id:
        thread = await memory_store.get_thread(thread_id)
        if thread and thread.status == ThreadStatus.PLANNING:
            raise thread_busy_http(thread_id)
        if (
            thread
            and thread.status == ThreadStatus.AWAITING_REVIEW
            and not body.clarify_response
        ):
            raise thread_busy_http(thread_id)
    else:
        snap = await memory_store.ensure_thread(None)
        thread_id = snap.thread_id

    snapshot = await memory_store.ensure_thread(thread_id)

    return StreamingResponse(
        _stream_events(
            workflow.run_chat(
                snapshot,
                body.source_text,
                body.request_id,
                clarify_response=body.clarify_response,
            )
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
