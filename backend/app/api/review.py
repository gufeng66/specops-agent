"""POST /runs/{run_id}/resume：分配负责人或审核。interrupt_type 必须与当前暂停类型一致。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.agent.events import SSE_HEADERS, emit_done, emit_metadata, emit_result
from app.agent.workflow import SpecOpsWorkflow
from app.api.schemas import ResumeRequest
from app.domain.models import ThreadStatus
from app.memory.store import memory_store

router = APIRouter(prefix="/api/v1/runs", tags=["review"])
workflow = SpecOpsWorkflow(memory_store)


async def _stream_events(generator):
    """把 workflow 异步生成器原样交给 StreamingResponse。"""
    async for event in generator:
        yield event


@router.post("/{run_id}/resume")
async def resume_run(run_id: str, body: ResumeRequest):
    """分配/审核 resume。interrupt_type 必须对上当前暂停类型，避免审面板去提交分配。"""
    claimed, existing = await memory_store.claim_request(body.request_id)

    if not claimed and existing:
        data = existing.model_dump(mode="json")
        data["idempotent"] = True

        async def idempotent_stream():
            yield emit_metadata({"run_id": existing.run_id, "idempotent": True})
            yield emit_result(data)
            yield emit_done()

        return StreamingResponse(
            idempotent_stream(), media_type="text/event-stream", headers=SSE_HEADERS
        )

    snapshot = await memory_store.find_by_run_id(run_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    if snapshot.status != ThreadStatus.AWAITING_REVIEW:
        raise HTTPException(status_code=400, detail="Run is not awaiting review")

    if body.interrupt_type:
        expected = ""
        if snapshot.interrupt_payload and isinstance(snapshot.interrupt_payload, dict):
            expected = str(snapshot.interrupt_payload.get("type") or "")
        if expected and body.interrupt_type != expected:
            raise HTTPException(
                status_code=400,
                detail=f"interrupt_type mismatch: expected {expected}, got {body.interrupt_type}",
            )

    assignments = [a.model_dump() for a in body.assignments]

    return StreamingResponse(
        _stream_events(
            workflow.resume(
                snapshot,
                body.revision_scope,
                body.target_item_id,
                body.feedback,
                body.approve,
                body.request_id,
                assignments=assignments,
                drop_item_ids=body.drop_item_ids,
            )
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
