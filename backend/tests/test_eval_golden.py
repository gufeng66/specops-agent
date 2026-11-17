"""黄金集门槛测试。必须 LLM_PROVIDER=none；有 Key 时主动 fail 而不是用模型分数冒充规则回归。

冻结基准只读：普通测试绝不调用 write_golden() 写仓库。生成/重录必须通过显式命令
`python -m evals.generate_cases --write`，或把 out_dir 注入 tmp_path 在临时目录里验证生成器。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.llm.client import llm_client
from evals.generate_cases import GOLDEN_DIR, write_golden
from evals.runner import EvalError, load_cases, run_case
from evals.score import aggregate, score_case


def _golden_fingerprint() -> dict[str, str]:
    """按文件相对名 + 字节哈希记录冻结基准当前状态，用于断言测试没有写仓库。"""
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(GOLDEN_DIR.glob("*.json"))
    }


@pytest.mark.asyncio
async def test_eval_golden(tmp_path):
    before = _golden_fingerprint()
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
        rows.append(
            score_case(
                case,
                result["snapshot"],
                result["seen"],
                result["snapshot"].task_draft.items,
            )
        )

    summary = aggregate(rows)
    assert summary["passed"], (
        f"eval thresholds failed: recall={summary['recall']:.3f} "
        f"primary={summary['primary_acc']:.3f} secondary={summary['secondary_acc']:.3f} "
        f"hitl={summary['hitl']:.3f} rows={summary['rows']}"
    )
    # 整个冻结评测只读 golden，不得改写仓库里的基准文件（内容或换行）。
    assert _golden_fingerprint() == before, "frozen golden files were modified by the test run"


def test_write_golden_to_tmp_path_does_not_touch_repo(tmp_path):
    """生成器的 out_dir 注入：在临时目录产出与仓库基准一致的文件，且不碰仓库。"""
    before = _golden_fingerprint()
    out_dir: Path = tmp_path / "golden"
    written = write_golden(out_dir=out_dir)
    assert written, "generator produced no cases"
    repo_names = {p.name for p in GOLDEN_DIR.glob("*.json")}
    generated_names = {p.name for p in written}
    # assign_04 是手写代词 case，不走模板生成器；生成器只覆盖其余模板 case。
    assert generated_names <= repo_names, "generator produced files absent from repo golden"
    assert generated_names, "generator set is empty"
    for src in written:
        dst = GOLDEN_DIR / src.name
        # 规范化后比较 JSON 对象；行尾（LF/CRLF）差异不影响基准语义。
        assert json.loads(src.read_text(encoding="utf-8-sig")) == json.loads(
            dst.read_text(encoding="utf-8-sig")
        ), f"{src.name} differs from repo golden"
    # 手写 case 不能被模板重录抹掉。
    assert "assign_04.json" in repo_names
    assert "assign_04.json" not in generated_names
    assert _golden_fingerprint() == before, "write_golden(out_dir=...) touched the repo golden dir"
