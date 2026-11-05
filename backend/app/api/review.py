"""POST /runs/{run_id}/resume：分配负责人或审核。认领事务里核对中断标识。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.agent.events import SSE_HEADERS, emit_done, emit_metadata, emit_result
from app.agent.workflow import SpecOpsWorkflow
from app.api.schemas import ResumeRequest
from app.errors import conflict_http
from app.memory.store import ClaimOutcome, memory_store

router = APIRouter(prefix="/api/v1/runs", tags=["review"])
workflow = SpecOpsWorkflow(memory_store)


async def _stream_events(generator):
    """把 workflow 异步生成器原样交给 StreamingResponse。"""
    async for event in generator:
        yield event


@router.post("/{run_id}/resume")
async def resume_run(run_id: str, body: ResumeRequest):
    """分配/审核 resume。状态、中断类型和中断标识都在认领事务里校验。"""
    assignments = [a.model_dump() for a in body.assignments]
    patch = body.item_patch.model_dump(exclude_unset=True) if body.item_patch else None
    payload = {
        "revision_scope": body.revision_scope,
        "target_item_id": body.target_item_id,
        "feedback": body.feedback,
        "approve": body.approve,
        "assignments": assignments,
        "drop_item_ids": body.drop_item_ids,
        "interrupt_id": body.interrupt_id,
        "interrupt_type": body.interrupt_type or "",
        "item_patch": patch,
    }
    known = await memory_store.get_processed(body.request_id)
    if known:
        if known["op"] != "resume" or known["target_run_id"] != run_id:
            raise conflict_http("REQUEST_CONFLICT", "相同 request_id 对应了不同的运行或操作")
        thread_id = known["thread_id"]
    else:
        snapshot = await memory_store.find_by_run_id(run_id)
        if not snapshot:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
        thread_id = snapshot.thread_id
    result = await memory_store.claim_operation(
        request_id=body.request_id,
        op="resume",
        thread_id=thread_id,
        payload=payload,
        interrupt_id=body.interrupt_id,
        interrupt_type=body.interrupt_type or "",
        target_run_id=run_id,
    )
    if result.outcome == ClaimOutcome.REPLAY and result.snapshot:
        data = result.snapshot.model_dump(mode="json")
        data["idempotent"] = True

        async def idempotent_stream():
            yield emit_metadata({"run_id": result.snapshot.run_id, "idempotent": True})
            yield emit_result(data)
            yield emit_done()

        return StreamingResponse(
            idempotent_stream(), media_type="text/event-stream", headers=SSE_HEADERS
        )
    if result.outcome != ClaimOutcome.CLAIMED or result.snapshot is None:
        raise conflict_http(result.code or "REQUEST_REJECTED", result.message or "请求被拒绝")

    return StreamingResponse(
        _stream_events(
            workflow.resume(
                result.snapshot,
                body.revision_scope,
                body.target_item_id,
                body.feedback,
                body.approve,
                body.request_id,
                assignments=assignments,
                drop_item_ids=body.drop_item_ids,
                interrupt_id=body.interrupt_id,
                interrupt_type=body.interrupt_type,
                item_patch=patch,
            )
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
