from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.agent.events import emit_done, emit_metadata, emit_result
from app.agent.workflow import SpecOpsWorkflow
from app.api.schemas import ResumeRequest
from app.domain.models import ThreadStatus
from app.memory.store import memory_store

router = APIRouter(prefix="/api/v1/runs", tags=["review"])
workflow = SpecOpsWorkflow(memory_store)


async def _stream_events(generator):
    async for event in generator:
        yield event


@router.post("/{run_id}/resume")
async def resume_run(run_id: str, body: ResumeRequest):
    claimed, existing = await memory_store.claim_request(body.request_id)

    if not claimed and existing:
        data = existing.model_dump(mode="json")
        data["idempotent"] = True

        async def idempotent_stream():
            yield emit_metadata({"run_id": existing.run_id, "idempotent": True})
            yield emit_result(data)
            yield emit_done()

        return StreamingResponse(idempotent_stream(), media_type="text/event-stream")

    snapshot = await memory_store.find_by_run_id(run_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    if snapshot.status != ThreadStatus.AWAITING_REVIEW:
        raise HTTPException(status_code=400, detail="Run is not awaiting review")

    return StreamingResponse(
        _stream_events(
            workflow.resume(
                snapshot,
                body.revision_scope,
                body.target_item_id,
                body.feedback,
                body.approve,
                body.request_id,
            )
        ),
        media_type="text/event-stream",
    )
