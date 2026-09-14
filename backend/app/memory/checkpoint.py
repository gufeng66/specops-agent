"""LangGraph checkpointer。SQLite 打不开时回退 MemorySaver（进程内，重启丢失）。"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from app.config import settings

logger = logging.getLogger(__name__)

_checkpointer: Any | None = None
_checkpointer_cm: Any | None = None


async def setup_checkpointer() -> Any:
    """打开进程级 AsyncSqliteSaver；打不开则 MemorySaver（重启丢失）。"""
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
    """关闭 lifespan 打开的 SQLite 连接。"""
    global _checkpointer, _checkpointer_cm
    if _checkpointer_cm is not None:
        try:
            await _checkpointer_cm.__aexit__(None, None, None)
        except Exception:
            logger.exception("AsyncSqliteSaver close failed")
        _checkpointer_cm = None
    _checkpointer = None


def get_checkpointer() -> Any:
    """返回当前 checkpointer；尚未 setup 时给 MemorySaver，避免 import 期崩溃。"""
    if _checkpointer is None:
        return MemorySaver()
    return _checkpointer


def reset_checkpointer() -> None:
    """丢掉缓存引用（单测）。应用退出应走 close_checkpointer。"""
    global _checkpointer, _checkpointer_cm
    _checkpointer = None
    _checkpointer_cm = None
