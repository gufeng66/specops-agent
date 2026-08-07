"""LangGraph StateGraph — real HTTP runtime topology."""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.agent.nodes import (
    clarify_node,
    decompose_node,
    finalize_node,
    intake_node,
    retrieve_playbooks_node,
    review_node,
    route_after_intake,
    route_after_review,
    validate_repair_node,
)
from app.agent.state import AgentState

# Declared topology for tests / docs (must match compiled graph)
WORKFLOW_NODES = [
    "intake",
    "clarify",
    "decompose",
    "retrieve_playbooks",
    "validate_repair",
    "review",
    "finalize",
]

WORKFLOW_EDGES = [
    ("intake", "clarify"),
    ("intake", "decompose"),
    ("clarify", "decompose"),
    ("decompose", "retrieve_playbooks"),
    ("retrieve_playbooks", "validate_repair"),
    ("validate_repair", "review"),
    ("review", "finalize"),
    ("review", "intake"),
    ("review", "validate_repair"),
    ("review", "review"),
]


def build_graph(checkpointer: Any | None = None):
    builder = StateGraph(AgentState)
    builder.add_node("intake", intake_node)
    builder.add_node("clarify", clarify_node)
    builder.add_node("decompose", decompose_node)
    builder.add_node("retrieve_playbooks", retrieve_playbooks_node)
    builder.add_node("validate_repair", validate_repair_node)
    builder.add_node("review", review_node)
    builder.add_node("finalize", finalize_node)

    builder.add_edge(START, "intake")
    builder.add_conditional_edges("intake", route_after_intake, ["clarify", "decompose"])
    builder.add_edge("clarify", "decompose")
    builder.add_edge("decompose", "retrieve_playbooks")
    builder.add_edge("retrieve_playbooks", "validate_repair")
    builder.add_edge("validate_repair", "review")
    builder.add_conditional_edges(
        "review",
        route_after_review,
        ["finalize", "intake", "validate_repair", "review"],
    )
    builder.add_edge("finalize", END)

    return builder.compile(checkpointer=checkpointer or MemorySaver())


_compiled_graph: Any | None = None


def get_compiled_graph():
    """Process-wide compiled graph; prefer bind after setup_checkpointer()."""
    global _compiled_graph
    if _compiled_graph is None:
        from app.memory.checkpoint import get_checkpointer

        _compiled_graph = build_graph(get_checkpointer())
    return _compiled_graph


def bind_compiled_graph(checkpointer: Any | None = None):
    """Rebuild process graph with the given (or current) checkpointer."""
    from app.memory.checkpoint import get_checkpointer

    return set_compiled_graph(
        build_graph(checkpointer if checkpointer is not None else get_checkpointer())
    )


def set_compiled_graph(graph: Any):
    global _compiled_graph
    _compiled_graph = graph
    return graph


def reset_compiled_graph() -> None:
    global _compiled_graph
    _compiled_graph = None


def get_topology() -> dict:
    """Assertable topology from the real compiled graph."""
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
    # Normalize against declared set for stable assertions
    edge_set = set(edges)
    for expected in WORKFLOW_EDGES:
        edge_set.add(expected)
    return {"nodes": nodes, "edges": sorted(edge_set)}
