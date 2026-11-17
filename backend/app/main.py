"""FastAPI 入口。lifespan 里先起 SQLite 再绑定带 checkpointer 的图，并回写 API 层 workflow 单例。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.agent.graph import bind_compiled_graph, reset_compiled_graph
from app.api.chat import router as chat_router
from app.api.review import router as review_router
from app.api.threads import router as threads_router
from app.config import settings
from app.llm.client import llm_client
from app.llm.runtime import shared_client, start_llm_runtime, stop_llm_runtime
from app.memory.checkpoint import (
    checkpointer_status,
    close_checkpointer,
    probe_checkpointer,
    setup_checkpointer,
)
from app.memory.store import memory_store
from app.obs import configure_logging

logger = logging.getLogger(__name__)
_REQUEST_PRUNE_INTERVAL_SECONDS = 60 * 60


async def _processed_request_maintenance() -> None:
    """低流量进程也定期清理过期快照正文。"""
    while True:
        try:
            await asyncio.sleep(_REQUEST_PRUNE_INTERVAL_SECONDS)
            await memory_store.prune_processed_requests(settings.request_retention_days)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("后台清理过期请求快照失败，将在下一轮重试")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """先 SQLite，再绑定带 checkpointer 的图，并回写 chat/review 里的 workflow 单例。"""
    configure_logging()
    # 记录启动进度：初始化在任意一步失败时都能据此判断已启动了哪些资源。
    # 三个清理函数本身幂等、且各自内部判断是否真的持有资源，所以 finally 无条件调用，
    # 并把整个初始化（不只是 yield）都包进 try：中途失败也一定关闭共享 HTTP client /
    # SQLite 连接，不会把已启动的资源泄漏在半启动的进程里。
    runtime_started = False
    checkpointer_ready = False
    prune_task: asyncio.Task[None] | None = None
    try:
        start_llm_runtime()
        runtime_started = True
        # 启动即校验模型配置：provider 非 none 且配置非法（缺 Key / 坏 base URL / 坏模型名）
        # 时直接阻止启动，而不是等第一个请求或只靠 /ready 返回 503。
        # LLM_PROVIDER=none（离线规则路径）validate_llm_config() 返回空串，允许启动。
        llm_error = llm_client.validate_llm_config()
        if llm_error:
            raise RuntimeError(f"LLM 配置非法，拒绝启动：{llm_error}")
        await memory_store.init()
        # sqlite 模式打不开直接抛 CheckpointSetupError，让进程起不来；
        # 不能降级成内存模式假装可用（重启后 HITL 状态会全部丢失）。
        checkpointer = await setup_checkpointer()
        checkpointer_ready = True
        graph = bind_compiled_graph(checkpointer)
        # Rebind API singletons so they use the async checkpointer graph
        from app.agent.workflow import SpecOpsWorkflow
        from app.api import chat as chat_mod
        from app.api import review as review_mod

        chat_mod.workflow = SpecOpsWorkflow(memory_store, graph=graph)
        review_mod.workflow = SpecOpsWorkflow(memory_store, graph=graph)
        await chat_mod.workflow.reconcile_open_requests()
        # 回放窗口清理：启动时清理积压，后台定期继续维护。逻辑过期仍在 claim 事务中保证。
        # REQUEST_RETENTION_DAYS=0 表示关闭。失败只记录日志，不能让服务起不来。
        if settings.request_retention_days > 0:
            try:
                pruned = await memory_store.prune_processed_requests(
                    settings.request_retention_days
                )
                if pruned:
                    logger.info("pruned %s expired request snapshots", pruned)
            except Exception:
                logger.exception("清理过期请求快照失败，服务继续启动")
            prune_task = asyncio.create_task(
                _processed_request_maintenance(), name="processed-request-prune"
            )
            app.state.processed_request_prune_task = prune_task
        yield
    finally:
        if prune_task is not None:
            prune_task.cancel()
            try:
                await prune_task
            except asyncio.CancelledError:
                pass
            app.state.processed_request_prune_task = None
        logger.info(
            "lifespan teardown (runtime_started=%s, checkpointer_ready=%s)",
            runtime_started,
            checkpointer_ready,
        )
        reset_compiled_graph()
        await close_checkpointer()
        await stop_llm_runtime()


# 将上面定义好的 lifespan 传入 FastAPI，使得整个启动流程挂钩。
app = FastAPI(title="SpecOps Agent", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_router)# 处理主流程：上传会议记录 -> 生成行动项
app.include_router(review_router)# 处理 HITL 审核反馈（批准/修改/打回）
app.include_router(threads_router)# 管理会话快照（查询历史、删除等）


@app.get("/health")
async def health():
    """liveness：进程活着就 200。依赖状态看 /ready。"""
    return {"status": "ok", "llm_provider": settings.llm_provider}


async def _readiness_checks() -> list[dict[str, Any]]:
    """逐项探测：业务库、checkpoint、共享 HTTP 客户端、模型配置。"""
    checks: list[dict[str, Any]] = []

    ok = await memory_store.ping()
    checks.append(
        {
            "name": "business_db",
            "ok": ok,
            "detail": "" if ok else "业务 SQLite 探测失败（ping 返回 False）",
        }
    )

    schema = await memory_store.schema_status()
    checks.append(
        {
            "name": "store_schema",
            "ok": bool(schema["ok"]),
            "degraded": bool(schema["degraded"]),
            "detail": str(schema["detail"]),
            "supported_version": schema["supported_version"],
            "max_version": schema["max_version"],
            "future_count": schema["future_count"],
            "corrupt_count": schema["corrupt_count"],
            "corrupt_thread_ids": schema["corrupt_thread_ids"],
        }
    )

    cp = checkpointer_status()
    probe = await probe_checkpointer() if cp["ready"] else cp["detail"]
    checks.append(
        {
            "name": "checkpoint",
            "ok": bool(cp["ready"]) and not probe,
            "degraded": bool(cp["degraded"]),
            "detail": probe or cp["detail"],
        }
    )

    client = shared_client()
    checks.append(
        {
            "name": "http_client",
            "ok": client is not None and not client.is_closed,
            "detail": "" if client is not None else "共享 HTTP 客户端未启动",
        }
    )

    llm_error = llm_client.validate_llm_config()
    checks.append({"name": "llm_config", "ok": not llm_error, "detail": llm_error})

    return checks


@app.get("/ready")
async def ready():
    """readiness：任一关键依赖不可用返回 503，并带上逐项结果。"""
    checks = await _readiness_checks()
    failed = [c for c in checks if not c["ok"]]
    degraded = any(c.get("degraded") for c in checks)
    payload = {
        "status": "ready" if not failed else "not_ready",
        "degraded": degraded,
        "app_env": settings.app_env,
        "checkpoint_mode": settings.checkpoint_mode,
        "checks": checks,
    }
    if failed:
        payload["detail"] = "；".join(f"{c['name']}: {c['detail']}" for c in failed)
        return JSONResponse(status_code=503, content=payload)
    return payload
