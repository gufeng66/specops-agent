"""精确率：没配对的抽出条数留在分母里。"""

from types import SimpleNamespace

from app.domain.models import ActionItem
from evals.score import score_case


def _row(items, gold):
    case = {
        "id": "precision",
        "expect": {"items": gold, "needs_clarify": False, "needs_assignment": False},
    }
    snapshot = SimpleNamespace(source_text="认证模块发布说明")
    return score_case(case, snapshot, [], items, [])


def test_precision_two_of_three():
    items = [
        ActionItem(title="认证模块", description="认证"),
        ActionItem(title="发布说明", description="发布"),
        ActionItem(title="无关寒暄", description="天气"),
    ]
    gold = [
        {"title_keywords": ["认证"], "primary_owner": "", "secondary_owners": []},
        {"title_keywords": ["发布"], "primary_owner": "", "secondary_owners": []},
    ]
    assert _row(items, gold)["precision"] == 2 / 3


def test_precision_counts_unmatched_item():
    items = [
        ActionItem(title="认证模块", description="认证"),
        ActionItem(title="发布说明", description="发布"),
        ActionItem(title="完全没有关键词", description="zzzz"),
    ]
    gold = [
        {"title_keywords": ["认证"], "primary_owner": "", "secondary_owners": []},
        {"title_keywords": ["发布"], "primary_owner": "", "secondary_owners": []},
    ]
    row = _row(items, gold)
    assert len(items) == 3
    assert row["precision"] == 2 / 3
