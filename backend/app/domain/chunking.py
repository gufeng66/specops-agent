"""长文本切块与规则∪LLM 草案合并。

不是 RAG：规则扫全文作底稿；模型按块只抽 items，再用标题+主责+引用偏移去重。
brief 单独一次采样（头+尾），不做块摘要并集。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from app.domain.models import ActionItem, MeetingBrief, TaskDraft

_SENTENCE_ENDS = "。！？；\n.!?;"
_TITLE_NOISE = re.compile(r"[\s\-—_.,，。！？、；：:\"'“”‘’《》【】（）()\[\]{}]+")
QUOTE_WINDOW = 300


def sample_for_brief(text: str, max_chars: int = 12000, head: int = 4000, tail: int = 2000) -> str:
    """intake 用的单一窗口。超长时保留文首议题和文末结论，中间用省略标记断开。"""
    source = text or ""
    if len(source) <= max_chars:
        return source
    marker = "\n\n…\n\n"
    return f"{source[:head]}{marker}{source[-tail:]}"


def split_source(text: str, chunk_chars: int = 3500, overlap: int = 200) -> list[str]:
    """按段/句切块并保留 overlap，避免行动项落在块边界被拆丢。"""
    source = text or ""
    if not source:
        return [""]
    if len(source) <= chunk_chars:
        return [source]

    chunks: list[str] = []
    start = 0
    n = len(source)
    min_cut = max(chunk_chars // 4, 1)
    while start < n:
        end = min(start + chunk_chars, n)
        if end < n:
            window = source[start:end]
            cut = _best_cut(window, min_cut)
            if cut > 0:
                end = start + cut
        chunks.append(source[start:end])
        if end >= n:
            break
        nxt = end - overlap if overlap else end
        if nxt <= start:
            nxt = end
        start = nxt
    return chunks


def _best_cut(window: str, min_cut: int) -> int:
    """优先在空行切断，其次句末标点；找不到则返回 -1 让调用方硬切。"""
    para = window.rfind("\n\n")
    if para >= min_cut:
        return para + 2
    for i in range(len(window) - 1, min_cut - 1, -1):
        if window[i] in _SENTENCE_ENDS:
            return i + 1
    return -1


def norm_title(title: str) -> str:
    """去标点空白后小写，给规则∪LLM 去重用。"""
    return _TITLE_NOISE.sub("", (title or "").strip()).lower()


def quote_offset(source: str, quote: str) -> int:
    """引用在原文中的起点；找不到返回 -1。"""
    q = (quote or "").strip()
    if not q:
        return -1
    return source.find(q)


def items_are_duplicate(left: ActionItem, right: ActionItem, source: str) -> bool:
    """标题归一化相同、主责相同、引用起点距离 < QUOTE_WINDOW 视为同一条。"""
    if norm_title(left.title) != norm_title(right.title):
        return False
    owner_l = (left.primary_owner or "").strip()
    owner_r = (right.primary_owner or "").strip()
    if owner_l != owner_r:
        return False
    off_l = quote_offset(source, left.evidence_quote)
    off_r = quote_offset(source, right.evidence_quote)
    if off_l < 0 or off_r < 0:
        return True
    return abs(off_l - off_r) < QUOTE_WINDOW


def merge_action_items(
    base: Sequence[ActionItem],
    extra: Sequence[ActionItem],
    source: str,
) -> list[ActionItem]:
    """规则底稿优先。块间和规则∪LLM 去重；冲突时留描述更长的那条，UUID 跟底稿。"""
    kept = [item.model_copy(deep=True) for item in base]
    for incoming in extra:
        idx = next((i for i, existing in enumerate(kept) if items_are_duplicate(existing, incoming, source)), None)
        if idx is None:
            kept.append(incoming.model_copy(deep=True))
            continue
        existing = kept[idx]
        if len(incoming.description or "") > len(existing.description or ""):
            richer = incoming.model_copy(deep=True)
            richer.id = existing.id
            kept[idx] = richer
    return kept


def merge_drafts(rule_draft: TaskDraft, llm_items: Sequence[ActionItem], source: str) -> TaskDraft:
    """规则底稿在前，LLM 增量合并；不整表替换，避免模型漏检时丢掉规则召回。"""
    merged = merge_action_items(rule_draft.items, llm_items, source)
    version = rule_draft.version
    if llm_items:
        version += 1
    return TaskDraft(items=merged, version=version)


def enrich_brief_from_items(brief: MeetingBrief, items: Sequence[ActionItem]) -> MeetingBrief:
    """用已抽出的主/次责补干系人，避免后续 INVALID_PRIMARY。不把建议名写进名单。"""
    names = [n for n in brief.stakeholders if n]
    seen = set(names)
    for item in items:
        for raw in [item.primary_owner, *item.secondary_owners]:
            name = (raw or "").strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    out = brief.model_copy()
    out.stakeholders = names
    return out


def evidence_windows(source: str, draft: TaskDraft, window: int = 160, cap: int = 6000) -> str:
    """给校验工具循环 / propose_owners 的引用邻域，避免把整篇原文塞进 prompt。"""
    parts: list[str] = []
    seen: set[str] = set()
    for item in draft.items:
        quote = (item.evidence_quote or "").strip()
        if quote and quote in source:
            idx = source.find(quote)
            start = max(0, idx - window)
            end = min(len(source), idx + len(quote) + window)
            snippet = source[start:end]
        else:
            snippet = (item.title or "").strip()
        if snippet and snippet not in seen:
            seen.add(snippet)
            parts.append(snippet)
    text = "\n---\n".join(parts)
    if text:
        return text[:cap]
    return source[:2000]
