"""真实后端 override 全链路集成（m02853 第 7 项）。

与 frontend/e2e/override-approval.spec.ts 的 page.route 模拟不同，本用例：

- 用 ASGITransport 起真实 FastAPI app，指向独立的测试 SQLite 与真实图/工作流；
- 非法证据通过「测试专用入口」注入 LangGraph checkpoint 状态
  （graph.aupdate_state），**不给生产 API 增加随意修改快照的公开入口**；
- 真实调 /api/v1/chat/stream → /api/v1/runs/{run_id}/resume（普通批准被阻断）→
  再次带 override_reason 的 resume（放行完成）；
- 断言后端返回 completed，并分别通过 GET 线程与 GET Markdown 导出核对
  review.override_* 与「## 人工放行」由后端真实写入 SQLite 与真实生成。
"""

import pytest
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.memory import MemorySaver

from app.agent.graph import build_graph
from app.agent.workflow import SpecOpsWorkflow
from app.domain.models import ThreadStatus
from app.main import app
from app.memory.store import MemoryStore

SAMPLE_TEXT = """会议日期：2026-02-10
参会人：张伟、李娜
会议目标：Q1 发布准备

讨论内容：
1. 张伟负责完成认证模块重构，下周五前提交代码评审。
2. 李娜跟进 API 文档更新，本周内完成。
"""

OVERRIDE_REASON = "会议原话已改写成书面表述，我确认语义一致"
OPERATOR_CLAIM = "张三（本地审核）"


def _events(text: str, event: str) -> list[dict]:
    """按 SSE 帧提取指定 event 的 data（与 test_resilience.py 同构）。"""
    import json

    out: list[dict] = []
    current = ""
    for line in text.splitlines():
        if line.startswith("event:"):
            current = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and current == event:
            out.append(json.loads(line.split(":", 1)[1].strip()))
    return out


async def _http_client(tmp_path, monkeypatch):
    """搭一套指向独立 store 的 ASGI client，并返回同一个图实例。

    返回 graph 是为了让测试用 aupdate_state 注入非法证据（测试专用入口）；
    production API 没有任何「任意改快照」的入口。
    """
    store = MemoryStore(str(tmp_path / "override_int.db"))
    await store.init()
    graph = build_graph(MemorySaver())
    wf = SpecOpsWorkflow(store, graph=graph)
    monkeypatch.setattr("app.memory.store.memory_store", store)
    monkeypatch.setattr("app.api.chat.memory_store", store)
    monkeypatch.setattr("app.api.chat.workflow", wf)
    monkeypatch.setattr("app.api.review.memory_store", store)
    monkeypatch.setattr("app.api.review.workflow", wf)
    monkeypatch.setattr("app.api.threads.memory_store", store)
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    return client, store, graph


async def _inject_invalid_evidence(graph, snap) -> None:
    """测试专用注入：把第一条任务的证据改成原文里不存在的句子。

    review_node 的 approve 分支会 validate_and_annotate 重算校验，
    所以改 checkpoint 里的 task_draft 就足以触发 INVALID_EVIDENCE_QUOTE。
    """
    draft = snap.task_draft.model_copy(deep=True)
    draft.items[0].evidence_quote = "这句话并不出现在会议原文里"
    config = {"configurable": {"thread_id": snap.graph_ns}}
    await graph.aupdate_state(config, {"task_draft": draft})


@pytest.mark.asyncio
async def test_real_backend_override_flow(tmp_path, monkeypatch):
    client, store, graph = await _http_client(tmp_path, monkeypatch)

    # 1) 真实起会话：规则底稿直接落到人工审核中断。
    start = await client.post(
        "/api/v1/chat/stream",
        json={"source_text": SAMPLE_TEXT, "request_id": "req-int-start"},
    )
    assert start.status_code == 200
    thread_id = _events(start.text, "metadata")[0]["thread_id"]
    first_interrupt = _events(start.text, "interrupt")[-1]
    assert first_interrupt["type"] == "review"

    snap = await store.get_thread(thread_id)
    assert snap.status == ThreadStatus.AWAITING_REVIEW

    # 2) 测试专用入口注入非法证据（不改生产 API）。
    await _inject_invalid_evidence(graph, snap)

    # 3) 普通批准：后端 review_node 重新校验后必须阻断，不能导出。
    blocked = await client.post(
        f"/api/v1/runs/{snap.run_id}/resume",
        json={
            "request_id": "req-int-blocked",
            "approve": True,
            "revision_scope": "item",
            "feedback": "",
            "interrupt_id": first_interrupt["interrupt_id"],
            "interrupt_type": "review",
        },
    )
    assert blocked.status_code == 200
    blocked_result = _events(blocked.text, "result")[-1]
    assert blocked_result["status"] == ThreadStatus.AWAITING_REVIEW.value
    assert blocked_result["markdown_output"] == ""
    assert blocked_result["review"]["override_reason"] == ""
    codes = {
        issue["code"]
        for issue in (blocked_result.get("interrupt_payload") or {}).get("blocking_errors") or []
    }
    assert "INVALID_EVIDENCE_QUOTE" in codes

    # 4) 从最新快照取新 interrupt_id / run_id，换新 request_id，显式放行。
    #    review_node 每次执行都会换新 interrupt_id，旧的一次性 id 不能复用。
    snap2 = await store.get_thread(thread_id)
    assert snap2.status == ThreadStatus.AWAITING_REVIEW
    assert snap2.interrupt_id

    approved = await client.post(
        f"/api/v1/runs/{snap2.run_id}/resume",
        json={
            "request_id": "req-int-override",
            "approve": True,
            "revision_scope": "item",
            "feedback": "",
            "interrupt_id": snap2.interrupt_id,
            "interrupt_type": "review",
            "override_reason": OVERRIDE_REASON,
            "operator_claim": OPERATOR_CLAIM,
        },
    )
    assert approved.status_code == 200
    approved_result = _events(approved.text, "result")[-1]
    assert approved_result["status"] == ThreadStatus.COMPLETED.value

    # 5) GET 线程：override 信息由后端真实写入了 SQLite 业务快照。
    thread = await client.get(f"/api/v1/threads/{thread_id}")
    assert thread.status_code == 200
    body = thread.json()
    assert body["status"] == ThreadStatus.COMPLETED.value
    assert body["review"]["approved"] is True
    assert body["review"]["override_reason"] == OVERRIDE_REASON
    assert body["review"]["override_codes"] == ["INVALID_EVIDENCE_QUOTE"]
    assert body["review"]["operator_claim"] == OPERATOR_CLAIM
    assert body["review"]["override_at"]

    # 6) GET Markdown 导出：人工放行节由后端真实生成（不是前端伪造文本）。
    exported = await client.get(
        f"/api/v1/threads/{thread_id}/export", params={"format": "md"}
    )
    assert exported.status_code == 200
    md = exported.text
    assert "## 人工放行" in md
    assert f"- 放行理由：{OVERRIDE_REASON}" in md
    assert f"放行人（自述，未认证）：{OPERATOR_CLAIM}" in md
    assert "放行问题码：" in md
    # _md_inline 会转义下划线，导出里是 INVALID\_EVIDENCE\_QUOTE，不断言裸码。
    assert "INVALID" in md and "EVIDENCE" in md and "QUOTE" in md
    assert "## 未解决问题" in md
    assert "## 原文（原样，未转义）" in md
    # 原文区是动态代码围栏，会议原文逐字保留（证据连续子串约定的前提）。
    assert "1. 张伟负责完成认证模块重构，下周五前提交代码评审。" in md
