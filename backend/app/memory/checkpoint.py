"""LangGraph checkpointer。

两种模式由 CHECKPOINT_MODE 决定：
- `sqlite`（默认）：持久化。初始化失败必须让启动失败，不能静默换成内存。
- `memory`：显式选择的内存模式，只用于测试/演示；对外标记 degraded。

`MemorySaver` 永远是显式选择，不是异常兜底。
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from app.config import settings

logger = logging.getLogger(__name__)

_checkpointer: Any | None = None
_checkpointer_cm: Any | None = None
_backend: str = ""
_degraded: bool = False


class CheckpointSetupError(RuntimeError):
    """持久化 checkpointer 打不开。启动阶段必须让进程失败。"""


class CheckpointNotReadyError(RuntimeError):
    """图执行时还没有可用的 checkpointer。"""


def checkpoint_mode() -> str:
    """读取模式名：只接受 sqlite 或 memory，未知值直接抛错，不静默归一化。

    Settings 的字段校验通常在导入时就会拦住非法值；这里再兜一层，防止测试或运行时
    直接改写 settings 绕过类型校验，把拼写错误当成 sqlite 运行。
    """
    mode = (settings.checkpoint_mode or "").strip().lower()
    if mode not in ("sqlite", "memory"):
        raise CheckpointSetupError(
            f"CHECKPOINT_MODE 非法：{settings.checkpoint_mode!r}；"
            "只允许 sqlite 或 memory，不会静默回退"
        )
    return mode


def _app_env() -> str:
    return (settings.app_env or "").strip().lower()


def _assert_mode_allowed(mode: str) -> None:
    """生产环境不允许用内存 checkpointer：重启后 HITL 状态会丢，属于配置错误。"""
    if mode == "memory" and _app_env() in ("production", "prod"):
        raise CheckpointSetupError(
            "APP_ENV=production 不允许 CHECKPOINT_MODE=memory："
            "内存模式重启后无法恢复人工审核状态"
        )


async def setup_checkpointer() -> Any:
    """按模式打开 checkpointer。sqlite 模式失败直接抛 CheckpointSetupError。"""
    global _checkpointer, _checkpointer_cm, _backend, _degraded
    if _checkpointer is not None:
        return _checkpointer

    mode = checkpoint_mode()
    db_path = (settings.checkpoint_path or "").strip()
    _assert_mode_allowed(mode)

    if mode == "memory":
        _checkpointer = MemorySaver()
        _backend = "memory"
        _degraded = True
        logger.warning(
            "checkpoint mode=memory; HITL state will NOT survive a restart"
        )
        return _checkpointer

    # sqlite 是持久化模式：空路径和 :memory: 都是配置错误，绝不能隐式降级成
    # MemorySaver，否则服务看起来可用，重启后人工审核状态却全部丢失。
    if not db_path:
        raise CheckpointSetupError(
            "CHECKPOINT_MODE=sqlite 但 CHECKPOINT_PATH 为空：持久化模式必须给出数据库路径"
        )
    if db_path == ":memory:":
        raise CheckpointSetupError(
            "CHECKPOINT_MODE=sqlite 不允许 CHECKPOINT_PATH=:memory:："
            "如需内存模式请显式设置 CHECKPOINT_MODE=memory"
        )

    cm: Any | None = None
    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        cm = AsyncSqliteSaver.from_conn_string(db_path)
        saver = await cm.__aenter__()
        await saver.setup()
    except Exception as exc:
        # 已经 __aenter__ 成功但 setup 失败的连接也必须关掉，否则文件句柄泄漏。
        if cm is not None:
            try:
                await cm.__aexit__(type(exc), exc, exc.__traceback__)
            except Exception:
                logger.exception("清理失败的 checkpointer 连接时出错")
        _checkpointer = None
        _checkpointer_cm = None
        _backend = ""
        raise CheckpointSetupError(
            f"CHECKPOINT_MODE=sqlite 但无法初始化 {db_path}：{exc}"
        ) from exc

    _checkpointer_cm = cm
    _checkpointer = saver
    _backend = "sqlite"
    _degraded = False
    logger.info("AsyncSqliteSaver ready at %s", db_path)
    return _checkpointer


async def close_checkpointer() -> None:
    """关闭 lifespan 打开的 SQLite 连接。"""
    global _checkpointer, _checkpointer_cm, _backend, _degraded
    if _checkpointer_cm is not None:
        try:
            await _checkpointer_cm.__aexit__(None, None, None)
        except Exception:
            logger.exception("AsyncSqliteSaver close failed")
        _checkpointer_cm = None
    _checkpointer = None
    _backend = ""
    _degraded = False


def get_checkpointer() -> Any:
    """返回已 setup 的 checkpointer。未 setup 时抛错，不再偷偷给 MemorySaver。"""
    if _checkpointer is None:
        raise CheckpointNotReadyError(
            "checkpointer 尚未初始化；生产路径应在 lifespan 里先 setup_checkpointer()"
        )
    return _checkpointer


def checkpointer_status() -> dict[str, Any]:
    """给 /ready 用。字段统一用 ready/degraded，避免同一含义两种叫法。"""
    try:
        configured = checkpoint_mode()
    except CheckpointSetupError as exc:
        # 非法模式也是「没就绪」：状态查询本身不能抛异常让 /ready 变成 500。
        return {
            "ready": False,
            "degraded": False,
            "mode": "",
            "backend": "",
            "detail": str(exc),
        }
    if _checkpointer is None:
        return {
            "ready": False,
            "degraded": False,
            "mode": configured,
            "backend": "",
            "detail": "checkpointer 未初始化",
        }
    detail = "SQLite checkpointer 可用" if _backend == "sqlite" else "内存 checkpointer，重启丢失"
    return {
        "ready": True,
        "degraded": _degraded,
        "mode": configured,
        "backend": _backend,
        "detail": detail,
    }


async def probe_checkpointer() -> str:
    """读一次 checkpoint 表，确认连接还活着。返回空串表示正常。"""
    if _checkpointer is None:
        return "checkpointer 未初始化"
    if _backend != "sqlite":
        return ""
    try:
        conn = getattr(_checkpointer, "conn", None)
        if conn is None:
            return ""
        await conn.execute("SELECT count(*) FROM checkpoints")
        return ""
    except Exception as exc:
        logger.exception("checkpoint 探测失败")
        return f"checkpoint 探测失败：{exc}"


def reset_checkpointer() -> None:
    """丢掉缓存引用（单测）。应用退出应走 close_checkpointer。"""
    global _checkpointer, _checkpointer_cm, _backend, _degraded
    _checkpointer = None
    _checkpointer_cm = None
    _backend = ""
    _degraded = False
