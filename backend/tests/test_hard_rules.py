"""规则路径跑 hard case：上报召回和精确率。漏检不写成失败。"""

import pytest

from evals.runner import load_hard_cases, run_case
from evals.score import score_case


@pytest.mark.asyncio
async def test_hard_rules_report(tmp_path, capsys):
    for case in load_hard_cases():
        result = await run_case(case, tmp_path, mode="rules")
        row = score_case(
            case,
            result["snapshot"],
            result["seen"],
            result["scored_items"],
            result["stakeholders"],
        )
        print(f"{case['id']} recall={row['recall']:.3f} precision={row['precision']:.3f}")
        if case["id"] == "pronoun":
            item = next(i for i in result["scored_items"] if "跟进" in f"{i.title} {i.description}")
            assert item.primary_owner == ""
            assert item.unresolved_ref
    captured = capsys.readouterr()
    assert "compound_name" in captured.out
    assert "weak_verb" in captured.out
