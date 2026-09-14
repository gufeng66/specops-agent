"""切块与去重单测：句边界、overlap、规则∪LLM merge。"""

from app.domain.chunking import (
    merge_action_items,
    sample_for_brief,
    split_source,
)
from app.domain.models import ActionItem


def test_split_short_is_single_chunk():
    assert split_source("短文本", chunk_chars=100, overlap=10) == ["短文本"]


def test_split_prefers_sentence_boundary():
    left = "甲。" * 40
    right = "乙负责完成文末任务。"
    text = left + right
    chunks = split_source(text, chunk_chars=80, overlap=10)
    assert len(chunks) >= 2
    assert any("乙负责完成文末任务。" in c for c in chunks)
    assert "乙负" in "".join(chunks)


def test_split_no_paragraphs_keeps_overlap():
    text = "这是一句。那是一句。" * 30
    chunks = split_source(text, chunk_chars=60, overlap=12)
    assert len(chunks) >= 2
    assert chunks[0][-8:] in chunks[1]


def test_sample_for_brief_head_tail():
    text = "H" * 4000 + "MID" * 1000 + "T" * 2000
    window = sample_for_brief(text, max_chars=5000, head=4000, tail=2000)
    assert window.startswith("H" * 20)
    assert window.endswith("T" * 20)
    assert "…" in window
    assert "MID" not in window or window.count("M") < 50


def test_merge_dedup_rules_and_llm_and_overlap():
    source = "张伟负责完成认证模块。" + "x" * 50 + "张伟负责完成认证模块。"
    rule = ActionItem(
        id="rule-1",
        title="认证模块",
        description="短",
        primary_owner="张伟",
        evidence_quote="张伟负责完成认证模块。",
    )
    overlap_copy = ActionItem(
        id="llm-1",
        title="认证模块",
        description="更长的描述补充验收细节",
        primary_owner="张伟",
        evidence_quote="张伟负责完成认证模块。",
    )
    other = ActionItem(
        id="llm-2",
        title="文末验收",
        description="李娜负责完成文末验收。",
        primary_owner="李娜",
        evidence_quote="李娜负责完成文末验收。",
    )
    source = source + "李娜负责完成文末验收。"
    merged = merge_action_items([rule], [overlap_copy, other], source)
    titles = [i.title for i in merged]
    assert titles.count("认证模块") == 1
    assert "文末验收" in titles
    hit = next(i for i in merged if i.title == "认证模块")
    assert hit.id == "rule-1"
    assert "更长" in hit.description
