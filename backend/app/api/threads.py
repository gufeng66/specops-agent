"""线程查询与导出。GET 快照保留建议供 UI；export?format=json 会剥离 owner_suggestions。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse

from app.domain.models import ThreadSnapshot
from app.memory.store import memory_store

router = APIRouter(prefix="/api/v1/threads", tags=["threads"])


@router.get("/{thread_id}")
async def get_thread(thread_id: str) -> ThreadSnapshot:
    """给 UI 恢复用的完整快照，保留 owner_suggestions。"""
    snapshot = await memory_store.get_thread(thread_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Thread not found")
    return snapshot


@router.get("/{thread_id}/export")
async def export_thread(thread_id: str, format: str = Query("md", pattern="^(md|json)$")):
    """json 走 export_dict() 剥离建议；md 用已生成正文，缺则现渲染。"""
    snapshot = await memory_store.get_thread(thread_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Thread not found")

    if format == "json":
        return JSONResponse(snapshot.export_dict())

    md = snapshot.markdown_output
    if not md:
        from app.domain.extractors import render_markdown

        md = render_markdown(
            snapshot.meeting_brief,
            snapshot.task_draft,
            snapshot.validation,
            snapshot.source_text,
        )
    return PlainTextResponse(md, media_type="text/markdown")
