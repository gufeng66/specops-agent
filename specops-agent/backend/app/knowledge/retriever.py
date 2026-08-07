"""Playbook lookup tool surface (legacy module path).

Prefer ``app.agent.tools.search_playbooks``. This is keyword search over local
markdown playbooks — not vector RAG.
"""

from __future__ import annotations

from app.agent.tools import search_playbooks

# Backward-compatible alias used by older tests/imports
search_knowledge = search_playbooks

__all__ = ["search_playbooks", "search_knowledge"]
