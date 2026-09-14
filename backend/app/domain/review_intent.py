"""审核反馈里的删除意图解析：全局序号 / UUID，不用模型。

「张三的第 2 条」会被忽略（的第），避免和按人看板上的局部序号打架。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.domain.models import TaskDraft

DROP_TRIGGERS = ("不是任务", "删掉", "删除", "去掉")
_ORDINAL_RE = re.compile(r"(?<!的)第\s*([0-9一二两三四五六七八九十百零〇两、，,\s]+)\s*条")
_CN_DIGIT = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


@dataclass
class DropParseResult:
    matched: bool = False
    item_ids: list[str] = field(default_factory=list)
    error: str | None = None


def looks_like_drop_feedback(feedback: str) -> bool:
    """反馈是否含删除触发词，供 schema 在缺 target_id 时放行。"""
    text = (feedback or "").strip()
    if not text:
        return False
    return any(token in text for token in DROP_TRIGGERS)


def parse_cn_int(raw: str) -> int | None:
    """「十二」「2」等转正整数。不处理大于九十九的复杂中文。"""
    s = (raw or "").strip()
    if not s:
        return None
    if s.isdigit():
        value = int(s)
        return value if value > 0 else None
    if s == "十":
        return 10
    if s.startswith("十"):
        ones = parse_cn_int(s[1:])
        return 10 + ones if ones is not None else None
    if "十" in s:
        left, right = s.split("十", 1)
        tens = _CN_DIGIT.get(left) if left else 1
        ones = _CN_DIGIT.get(right, 0) if right else 0
        if tens is None:
            return None
        return tens * 10 + ones
    return _CN_DIGIT.get(s)


def _split_ordinal_token(blob: str) -> list[int]:
    """「1、2」或「一二」拆成多个全局序号。"""
    parts = re.split(r"[、，,\s]+", blob.strip())
    out: list[int] = []
    for part in parts:
        n = parse_cn_int(part)
        if n is not None:
            out.append(n)
    return out


def parse_drop_intent(
    *,
    feedback: str,
    draft: TaskDraft,
    revision_scope: str = "item",
    target_item_id: str | None = None,
    drop_item_ids: list[str] | None = None,
) -> DropParseResult:
    """把结构化 drop / 自然语言反馈映射到当前草案 UUID。

    序号是 1-based 全局顺序。「张三的第 2 条」因含「的第」被忽略，避免和看板局部序号打架。
    """
    items = list(draft.items)
    explicit_ids = [str(i) for i in (drop_item_ids or []) if str(i).strip()]
    scope_drop = revision_scope == "drop"
    text = (feedback or "").strip()
    has_trigger = looks_like_drop_feedback(text)

    if explicit_ids:
        valid = [i for i in explicit_ids if any(item.id == i for item in items)]
        if not valid:
            return DropParseResult(matched=True, error="要删除的条目不存在")
        return DropParseResult(matched=True, item_ids=valid)

    if not scope_drop and not has_trigger:
        return DropParseResult(matched=False)

    if not items:
        return DropParseResult(matched=True, error="当前没有可删除的任务")

    indices: list[int] = []
    skip_ordinals = "的第" in text
    if not skip_ordinals:
        for match in _ORDINAL_RE.finditer(text):
            indices.extend(_split_ordinal_token(match.group(1)))

    if re.search(r"最后一[条项]", text):
        indices.append(len(items))

    relative = bool(re.search(r"上面那条|这条|该项", text))
    selected = str(target_item_id).strip() if target_item_id else ""

    if relative and selected:
        return DropParseResult(matched=True, item_ids=[selected])

    if indices:
        out_of_range = [n for n in indices if n < 1 or n > len(items)]
        if out_of_range:
            return DropParseResult(matched=True, error=f"当前只有 {len(items)} 条")
        ids: list[str] = []
        seen: set[str] = set()
        for n in indices:
            item_id = items[n - 1].id
            if item_id not in seen:
                seen.add(item_id)
                ids.append(item_id)
        return DropParseResult(matched=True, item_ids=ids)

    if selected and has_trigger and not skip_ordinals:
        return DropParseResult(matched=True, item_ids=[selected])

    if skip_ordinals:
        return DropParseResult(
            matched=True,
            error="请使用全局序号，例如「第 2 条不是任务，删掉」",
        )

    if scope_drop and selected:
        return DropParseResult(matched=True, item_ids=[selected])

    if scope_drop or has_trigger:
        return DropParseResult(matched=True, error="无法解析要删除的条目")

    return DropParseResult(matched=False)
