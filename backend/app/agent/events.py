"""SSE 帧格式。progress.status 仅 running|done|skipped|waiting，与前端步进条一一对应。"""

import json
from typing import Any, Literal

ProgressStatus = Literal["running", "done", "skipped", "waiting"]

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _sse(event: str, data: dict[str, Any]) -> str:
    """一帧：event 名 + 一行 JSON + 空行。ensure_ascii=False 保留中文。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def emit_metadata(data: dict[str, Any]) -> str:
    return _sse("metadata", data)


def emit_progress(node: str, message: str, status: ProgressStatus | None = None) -> str:
    """步进条只认 status；缺省时前端只记时间线、不改圆点。"""
    payload: dict[str, Any] = {"node": node, "message": message}
    if status is not None:
        payload["status"] = status
    return _sse("progress", payload)


def emit_tool_result(data: dict[str, Any]) -> str:
    return _sse("tool_result", data)


def emit_interrupt(data: dict[str, Any]) -> str:
    return _sse("interrupt", data)


def emit_token(text: str) -> str:
    return _sse("token", {"text": text})


def emit_result(data: dict[str, Any]) -> str:
    return _sse("result", data)


def emit_error(message: str) -> str:
    return _sse("error", {"message": message})


def emit_done() -> str:
    return _sse("done", {})
