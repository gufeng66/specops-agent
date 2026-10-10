"""SQLite checkpointer 关掉再打开，同一 graph_ns 仍能从分配续到导出。"""

import pytest

from app.agent.graph import build_graph
from app.agent.workflow import SpecOpsWorkflow
from app.llm.client import llm_client
from app.memory.store import MemoryStore

TEXT = """会议目标：发布准备
参会人：张伟、李娜
认证模块这周要完成代码评审。
"""


@pytest.mark.asyncio
async def test_sqlite_checkpoint_resume_after_reopen(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_client, "provider", "none")
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    db = str(tmp_path / "cp.db")
    store = MemoryStore(str(tmp_path / "snap.db"))
    await store.init()

    cm = AsyncSqliteSaver.from_conn_string(db)
    saver = await cm.__aenter__()
    try:
        wf = SpecOpsWorkflow(store, graph=build_graph(saver))
        snap = await store.ensure_thread("t-cp")
        async for _ in wf.run_chat(snap, TEXT, "req-cp-1"):
            pass
    finally:
        await cm.__aexit__(None, None, None)

    snap = await store.get_thread("t-cp")
    assert snap is not None
    assert snap.interrupt_payload
    assert snap.interrupt_payload.get("type") == "assign_owners"
    ns = snap.graph_ns
    item_id = snap.task_draft.items[0].id

    cm2 = AsyncSqliteSaver.from_conn_string(db)
    saver2 = await cm2.__aenter__()
    try:
        wf2 = SpecOpsWorkflow(store, graph=build_graph(saver2))
        async for _ in wf2.resume(
            snap,
            "item",
            None,
            "",
            False,
            "req-cp-2",
            assignments=[{"item_id": item_id, "primary_owner": "张伟", "secondary_owners": []}],
        ):
            pass
        snap = await store.get_thread("t-cp")
        assert snap is not None
        assert snap.graph_ns == ns
        assert snap.interrupt_payload
        assert snap.interrupt_payload.get("type") == "review"
        async for _ in wf2.resume(snap, "item", item_id, "", True, "req-cp-3"):
            pass
    finally:
        await cm2.__aexit__(None, None, None)

    snap = await store.get_thread("t-cp")
    assert snap is not None
    assert snap.markdown_output
    assert snap.graph_ns == ns
