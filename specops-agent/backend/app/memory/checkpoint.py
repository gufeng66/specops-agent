"""Thin checkpointer factory coexisting with MemoryStore (ThreadSnapshot API)."""

from __future__ import annotations

import logging
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from app.config import settings

logger = logging.getLogger(__name__)

_checkpointer: Any | None = None
_checkpointer_cm: Any | None = None


async def setup_checkpointer() -> Any:
    """Open AsyncSqliteSaver (or MemorySaver) for the process lifetime."""
    global _checkpointer, _checkpointer_cm
    if _checkpointer is not None:
        return _checkpointer

    db_path = settings.checkpoint_path
    if db_path and db_path != ":memory:":
        try:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            _checkpointer_cm = AsyncSqliteSaver.from_conn_string(db_path)
            saver = await _checkpointer_cm.__aenter__()
            await saver.setup()
            _checkpointer = saver
            logger.info("AsyncSqliteSaver ready at %s", db_path)
            return _checkpointer
        except Exception:
            logger.exception(
                "AsyncSqliteSaver setup failed; falling back to MemorySaver"
            )
            _checkpointer_cm = None

    _checkpointer = MemorySaver()
    return _checkpointer


async def close_checkpointer() -> None:
    """Close SQLite connection opened by setup_checkpointer."""
    global _checkpointer, _checkpointer_cm
    if _checkpointer_cm is not None:
        try:
            await _checkpointer_cm.__aexit__(None, None, None)
        except Exception:
            logger.exception("AsyncSqliteSaver close failed")
        _checkpointer_cm = None
    _checkpointer = None


def get_checkpointer() -> Any:
    """Return the process checkpointer; MemorySaver if setup has not run yet."""
    if _checkpointer is None:
        return MemorySaver()
    return _checkpointer


def reset_checkpointer() -> None:
    """Clear cached checkpointer reference (tests). Prefer close_checkpointer in app."""
    global _checkpointer, _checkpointer_cm
    _checkpointer = None
    _checkpointer_cm = None
