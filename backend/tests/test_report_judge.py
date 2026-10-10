"""Judge 评已配对任务。没有配对就不写一个和召回相同的语义分。"""

from app.domain.models import ActionItem
from evals.report import _judge, _JudgeVerdicts, _pairs, _render, _Verdict


def test_pairs_are_keyword_matches_only():
    case = {
        "expect": {
            "items": [
                {"title_keywords": ["认证"]},
                {"title_keywords": ["不存在的词"]},
            ]
        }
    }
    items = [ActionItem(title="认证模块", description="")]
    pairs = _pairs(case, items)
    assert len(pairs) == 1
    assert pairs[0][0]["title_keywords"] == ["认证"]


async def test_judge_counts_same_pairs_only(monkeypatch):
    async def fake_chat_json(**kwargs):
        assert "关键词命中不等于同一件" in kwargs["system"]
        return _JudgeVerdicts(
            verdicts=[_Verdict(gold=0, same=True), _Verdict(gold=1, same=False), _Verdict(gold=0, same=True)]
        )

    monkeypatch.setattr("evals.report.llm_client.chat_json", fake_chat_json)
    pairs = [
        ({"title_keywords": ["认证"]}, ActionItem(title="认证", description="", evidence_quote="")),
        ({"title_keywords": ["回归"]}, ActionItem(title="天气", description="", evidence_quote="")),
    ]
    assert await _judge(pairs) == 1


def test_render_says_judge_did_not_run():
    text = _render(
        [
            {
                "id": "compound_name",
                "mode": "rules",
                "error": "",
                "recall": 0.5,
                "precision": 1.0,
                "owner_hallucination_rate": 0.0,
                "semantic_recall": None,
                "judge_ran": False,
                "judge_error": "",
                "injection_obeyed": False,
                "token_in": 0,
                "token_out": 0,
                "latency_ms": 0,
                "cost_usd": 0.0,
            }
        ],
        {"date": "2026-10-08", "model": "m", "prompt_version": "v", "source": "test"},
    )
    assert "本数据集未触发 judge" in text
    assert "语义召回" not in text


def test_render_semantic_can_differ_from_recall():
    row = {
        "id": "compound_name",
        "mode": "llm",
        "error": "",
        "recall": 1.0,
        "precision": 1.0,
        "owner_hallucination_rate": 0.0,
        "semantic_recall": 0.5,
        "judge_ran": True,
        "judge_error": "",
        "injection_obeyed": False,
        "token_in": 1,
        "token_out": 1,
        "latency_ms": 1,
        "cost_usd": 0.0,
    }
    text = _render(
        [row, {**row, "mode": "rules"}, {**row, "mode": "llm_only"}],
        {"date": "2026-10-08", "model": "m", "prompt_version": "v", "source": "test"},
    )
    assert "本数据集未触发 judge" not in text
    assert "| compound_name | llm | 1.000 | 1.000 | 0.000 | 0.500 |" in text
