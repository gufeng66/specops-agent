"""FastAPI 入口。lifespan 里先起 SQLite 再绑定带 checkpointer 的图，并回写 API 层 workflow 单例。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.graph import bind_compiled_graph, reset_compiled_graph
from app.api.chat import router as chat_router
from app.api.review import router as review_router
from app.api.threads import router as threads_router
from app.config import settings
from app.memory.checkpoint import close_checkpointer, setup_checkpointer
from app.memory.store import memory_store


@asynccontextmanager
async def lifespan(app: FastAPI):
    """先 SQLite，再绑定带 checkpointer 的图，并回写 chat/review 里的 workflow 单例。"""
    await memory_store.init()
    checkpointer = await setup_checkpointer()
    graph = bind_compiled_graph(checkpointer)
    # Rebind API singletons so they use the async checkpointer graph
    from app.agent.workflow import SpecOpsWorkflow
    from app.api import chat as chat_mod
    from app.api import review as review_mod

    chat_mod.workflow = SpecOpsWorkflow(memory_store, graph=graph)
    review_mod.workflow = SpecOpsWorkflow(memory_store, graph=graph)
    try:
        yield
    finally:
        reset_compiled_graph()
        await close_checkpointer()


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
    """探活。llm_provider 方便确认 CI 是否 LLM_PROVIDER=none。"""
    return {"status": "ok", "llm_provider": settings.llm_provider}
