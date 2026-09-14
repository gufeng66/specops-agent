"""LangGraph 拓扑。WORKFLOW_* 给测试断言，防止文档和代码分叉。

propose_owners：有 owner ERROR → assign_owners；主责已合法 → review（可带次责建议）。
无 Key 且无 owner ERROR 时 validate_repair 直达 review，建议/分配靠 SSE skipped 展示。
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.agent.nodes import (
    assign_owners_node,
    clarify_node,
    decompose_node,
    finalize_node,
    intake_node,
    propose_owners_node,
    review_node,
    route_after_assign,
    route_after_intake,
    route_after_propose,
    route_after_review,
    route_after_validate_repair,
    validate_repair_node,
)
from app.agent.state import AgentState

WORKFLOW_NODES = [
    "intake",
    "clarify",
    "decompose",
    "validate_repair",
    "propose_owners",
    "assign_owners",
    "review",
    "finalize",
]

WORKFLOW_EDGES = [
    ("intake", "clarify"),
    ("intake", "decompose"),
    ("clarify", "decompose"),
    ("decompose", "validate_repair"),
    ("validate_repair", "propose_owners"),
    ("validate_repair", "review"),
    ("propose_owners", "assign_owners"),
    ("propose_owners", "review"),
    ("assign_owners", "assign_owners"),
    ("assign_owners", "review"),
    ("review", "finalize"),
    ("review", "intake"),
    ("review", "validate_repair"),
    ("review", "review"),
]


def build_graph(checkpointer: Any | None = None):
    """编译固定拓扑。未传入 checkpointer 时用 MemorySaver，仅适合单测。"""
    builder = StateGraph(AgentState)
    builder.add_node("intake", intake_node)
    builder.add_node("clarify", clarify_node)
    builder.add_node("decompose", decompose_node)
    builder.add_node("validate_repair", validate_repair_node)
    builder.add_node("propose_owners", propose_owners_node)
    builder.add_node("assign_owners", assign_owners_node)
    builder.add_node("review", review_node)
    builder.add_node("finalize", finalize_node)

    builder.add_edge(START, "intake")
    builder.add_conditional_edges("intake", route_after_intake, ["clarify", "decompose"])
    builder.add_edge("clarify", "decompose")
    builder.add_edge("decompose", "validate_repair")
    builder.add_conditional_edges(
        "validate_repair",
        route_after_validate_repair,
        ["propose_owners", "review"],
    )
    builder.add_conditional_edges(
        "propose_owners",
        route_after_propose,
        ["assign_owners", "review"],
    )
    builder.add_conditional_edges(
        "assign_owners",
        route_after_assign,
        ["assign_owners", "review"],
    )
    builder.add_conditional_edges(
        "review",
        route_after_review,
        ["finalize", "intake", "validate_repair", "review"],
    )
    builder.add_edge("finalize", END)

    return builder.compile(checkpointer=checkpointer or MemorySaver())


_compiled_graph: Any | None = None


def get_compiled_graph():
    """进程级编译图。生产路径应在 lifespan 里 bind_compiled_graph。"""
    global _compiled_graph
    if _compiled_graph is None:
        from app.memory.checkpoint import get_checkpointer

        _compiled_graph = build_graph(get_checkpointer())
    return _compiled_graph


def bind_compiled_graph(checkpointer: Any | None = None):
    """用给定（或当前）checkpointer 重建进程图，供 API 单例共用。"""
    from app.memory.checkpoint import get_checkpointer

    return set_compiled_graph(
        build_graph(checkpointer if checkpointer is not None else get_checkpointer())
    )


def set_compiled_graph(graph: Any):
    """测试可注入内存图，避免打到真实 SQLite checkpointer。"""
    global _compiled_graph
    _compiled_graph = graph
    return graph


def reset_compiled_graph() -> None:
    """进程退出或测试 teardown 时丢掉编译图引用。"""
    global _compiled_graph
    _compiled_graph = None


def get_topology() -> dict:
    """从真实编译图抽出节点/边，给测试断言，防止文档和代码分叉。"""
    compiled = build_graph(MemorySaver())
    graph = compiled.get_graph()
    nodes = sorted(n for n in graph.nodes if n not in ("__start__", "__end__"))
    edges: list[tuple[str, str]] = []
    for edge in graph.edges:
        src, dst = edge[0], edge[1]
        if src in ("__start__",):
            continue
        if dst in ("__end__",):
            continue
        edges.append((src, dst))
    return {"nodes": nodes, "edges": sorted(set(edges))}
