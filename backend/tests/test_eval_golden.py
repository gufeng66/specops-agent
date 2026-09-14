"""黄金集门槛测试。必须 LLM_PROVIDER=none；有 Key 时主动 fail 而不是用模型分数冒充规则回归。"""

from __future__ import annotations

import pytest

from app.llm.client import llm_client
from evals.generate_cases import write_golden
from evals.runner import EvalError, load_cases, run_case
from evals.score import aggregate, score_case


@pytest.mark.asyncio
async def test_eval_golden(tmp_path):
    write_golden()
    if llm_client.enabled:
        pytest.fail("llm_client.enabled is True; frozen eval must run with LLM_PROVIDER=none")
    cases = load_cases()
    assert len(cases) >= 12
    multi = [c for c in cases if c["id"].startswith("multi_")]
    assert len(multi) >= 2

    rows = []
    for case in cases:
        try:
            result = await run_case(case, tmp_path)
        except EvalError as exc:
            pytest.fail(str(exc))
        rows.append(score_case(case, result["snapshot"], result["seen"]))

    summary = aggregate(rows)
    assert summary["passed"], (
        f"eval thresholds failed: recall={summary['recall']:.3f} "
        f"primary={summary['primary_acc']:.3f} secondary={summary['secondary_acc']:.3f} "
        f"hitl={summary['hitl']:.3f} rows={summary['rows']}"
    )
