from __future__ import annotations

import json
from typing import Any


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def emit_metadata(data: dict[str, Any]) -> str:
    return _sse("metadata", data)


def emit_progress(node: str, message: str) -> str:
    return _sse("progress", {"node": node, "message": message})


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
