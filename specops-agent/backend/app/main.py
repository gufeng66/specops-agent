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


app = FastAPI(title="SpecOps Agent", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_router)
app.include_router(review_router)
app.include_router(threads_router)


@app.get("/health")
async def health():
    return {"status": "ok", "llm_provider": settings.llm_provider}
