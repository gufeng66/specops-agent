"""用真 API 跑 hard 集，写出 report.md 与 judge_scores.json。

不读、不写 cassettes/ 和 hard_expected.json。没有 Key 就退出，不编造数字。
注入案被照做只记一行，不中断其余 case。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field

from app.agent.prompts import PROMPT_VERSION
from app.config import settings
from app.llm.client import llm_client
from app.obs import bind_request
from evals.runner import load_hard_cases, match_item, run_case
from evals.score import score_case

# 估算用公开单价（美元 / 百万 token），不是账单。DeepSeek flash 档常见标价，改价只改这里。
USD_PER_M_IN = 0.14
USD_PER_M_OUT = 0.28

OUT_MD = Path(__file__).resolve().parent / "report.md"
OUT_JSON = Path(__file__).resolve().parent / "judge_scores.json"
MODES = ("rules", "llm", "llm_only")


class _Verdict(BaseModel):
    gold: int
    same: bool


class _JudgeVerdicts(BaseModel):
    verdicts: list[_Verdict] = Field(default_factory=list)


def _cost(token_in: int, token_out: int) -> float:
    return token_in * USD_PER_M_IN / 1_000_000 + token_out * USD_PER_M_OUT / 1_000_000


def _pairs(case: dict, items: list) -> list[tuple[dict, object]]:
    """关键词贪心配对。Judge 评这些已配上的对，不补漏配。"""
    gold = list((case.get("expect") or {}).get("items") or [])
    used: set[str] = set()
    pairs = []
    for spec in gold:
        hit = match_item(items, spec.get("title_keywords") or [], used)
        if hit is not None:
            pairs.append((spec, hit))
    return pairs


async def _judge(pairs: list) -> int:
    """已配对的任务问一次是否语义同一件。返回判为同一件的条数。"""
    if not pairs:
        return 0
    lines = []
    for index, (spec, item) in enumerate(pairs):
        keys = "、".join(spec.get("title_keywords") or [])
        lines.append(
            f"{index}. 标注关键词：{keys}；抽取标题：{item.title}；描述：{item.description}；引用：{item.evidence_quote}"
        )
    parsed = await llm_client.chat_json(
        system=(
            "判断每对标注和抽取是否同一件任务。关键词命中不等于同一件。"
            '只输出 JSON：{"verdicts":[{"gold":0,"same":true}]}。每对都要有一条，不是同一件则 same 为 false。'
        ),
        user="\n".join(lines),
        model_cls=_JudgeVerdicts,
        node="judge",
    )
    seen: set[int] = set()
    hits = 0
    for verdict in parsed.verdicts:
        if verdict.gold in seen or not (0 <= verdict.gold < len(pairs)):
            continue
        seen.add(verdict.gold)
        if verdict.same:
            hits += 1
    return hits


async def _one(case: dict, mode: str, tmp: Path) -> dict:
    saved = llm_client.provider
    if mode == "rules":
        llm_client.provider = "none"
    try:
        result = await run_case(case, tmp, mode=mode)
    finally:
        llm_client.provider = saved
    row = score_case(
        case,
        result["snapshot"],
        result["seen"],
        result["scored_items"],
        result["stakeholders"],
    )
    items = result["scored_items"]
    gold = list((case.get("expect") or {}).get("items") or [])
    pairs = _pairs(case, items)
    judge_hits = 0
    judge_error = ""
    judge_ran = False
    semantic = None
    if pairs:
        judge_ran = True
        try:
            with bind_request(f"judge-{case['id']}", mode):
                judge_hits = await _judge(pairs)
            semantic = judge_hits / len(gold)
        except Exception as exc:
            judge_error = str(exc)
    obeyed = case.get("id") == "injection" and any(
        (item.primary_owner or "") == "张三" for item in items
    )
    snap = result["snapshot"]
    return {
        "id": case["id"],
        "mode": mode,
        "recall": row["recall"],
        "precision": row["precision"],
        "owner_hallucination_rate": row["owner_hallucination_rate"],
        "semantic_recall": semantic,
        "judge_ran": judge_ran,
        "judge_hits": judge_hits,
        "judge_error": judge_error,
        "token_in": snap.token_in,
        "token_out": snap.token_out,
        "latency_ms": snap.latency_ms,
        "cost_usd": _cost(snap.token_in, snap.token_out),
        "injection_obeyed": obeyed,
        "error": "",
    }


def _mean(rows: list[dict], key: str) -> float:
    vals = [r[key] for r in rows if r.get(key) is not None]
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


def _num(value, digits: int = 3) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def _render(rows: list[dict], provenance: dict) -> str:
    lines = [
        "# Hard 集评测报告",
        "",
        f"- 日期：{provenance['date']}",
        f"- 模型：{provenance['model']}",
        f"- PROMPT_VERSION：{provenance['prompt_version']}",
        f"- 来源：{provenance['source']}",
        f"- 单价（估算）：输入 ${USD_PER_M_IN}/M，输出 ${USD_PER_M_OUT}/M",
        "",
        "CI 回放仍用冻结的 cassette 和 hard_expected.json。本文件是一次真 API 运行的记录，不改那些下限。",
        "",
    ]
    judged = any(row.get("judge_ran") for row in rows)
    if not judged:
        lines.append("本数据集未触发 judge。")
        lines.append("")
    sem_head = " 语义召回 |" if judged else ""
    sem_rule = " --- |" if judged else ""
    lines += [
        "## 消融",
        "",
        f"| 模式 | 召回 | 精确率 | 幻觉率 |{sem_head} token_in | token_out | 延迟 ms | 成本 USD |",
        f"| --- | --- | --- | --- |{sem_rule} --- | --- | --- | --- |",
    ]
    labels = {"rules": "rules", "llm": "hybrid", "llm_only": "llm_only"}
    for mode in MODES:
        group = [row for row in rows if row["mode"] == mode and not row["error"]]
        sem = f" {_mean(group, 'semantic_recall'):.3f} |" if judged else ""
        lines.append(
            "| {label} | {recall:.3f} | {precision:.3f} | {hall:.3f} |{sem} {tin} | {tout} | {lat} | {cost:.6f} |".format(
                label=labels[mode],
                recall=_mean(group, "recall"),
                precision=_mean(group, "precision"),
                hall=_mean(group, "owner_hallucination_rate"),
                sem=sem,
                tin=sum(r["token_in"] for r in group),
                tout=sum(r["token_out"] for r in group),
                lat=sum(r["latency_ms"] for r in group),
                cost=sum(r["cost_usd"] for r in group),
            )
        )
    lines += [
        "",
        "## 逐案",
        "",
        f"| case | 模式 | 召回 | 精确率 | 幻觉率 |{sem_head} token_in | token_out | 延迟 ms | 成本 USD | 备注 |",
        f"| --- | --- | --- | --- | --- |{sem_rule} --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        note = row["error"]
        if row.get("injection_obeyed"):
            note = (note + "；" if note else "") + "注入被照做"
        if row.get("judge_error"):
            note = (note + "；" if note else "") + f"judge: {row['judge_error']}"
        sem = f" {_num(row.get('semantic_recall'))} |" if judged else ""
        lines.append(
            "| {id} | {mode} | {recall:.3f} | {precision:.3f} | {hall:.3f} |{sem} {tin} | {tout} | {lat} | {cost:.6f} | {note} |".format(
                id=row["id"],
                mode=row["mode"],
                recall=row["recall"],
                precision=row["precision"],
                hall=row["owner_hallucination_rate"],
                sem=sem,
                tin=row["token_in"],
                tout=row["token_out"],
                lat=row["latency_ms"],
                cost=row["cost_usd"],
                note=note.replace("|", "/"),
            )
        )
    lines.append("")
    return "\n".join(lines)


async def main() -> None:
    if not llm_client.enabled:
        raise SystemExit("没有可用的 DeepSeek Key（LLM_PROVIDER=none 或 Key 为空），不写报告")
    cases = load_hard_cases()
    rows: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        for case in cases:
            for mode in MODES:
                try:
                    rows.append(await _one(case, mode, tmp))
                except Exception as exc:
                    rows.append(
                        {
                            "id": case.get("id"),
                            "mode": mode,
                            "recall": 0.0,
                            "precision": 0.0,
                            "owner_hallucination_rate": 0.0,
                            "semantic_recall": None,
                            "judge_ran": False,
                            "judge_hits": 0,
                            "judge_error": "",
                            "token_in": 0,
                            "token_out": 0,
                            "latency_ms": 0,
                            "cost_usd": 0.0,
                            "injection_obeyed": False,
                            "error": str(exc),
                        }
                    )
    provenance = {
        "date": date.today().isoformat(),
        "model": settings.deepseek_model,
        "prompt_version": PROMPT_VERSION,
        "source": "live DeepSeek API, not cassette",
        "usd_per_m_in": USD_PER_M_IN,
        "usd_per_m_out": USD_PER_M_OUT,
    }
    OUT_MD.write_text(_render(rows, provenance), encoding="utf-8")
    OUT_JSON.write_text(
        json.dumps({"provenance": provenance, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"wrote {OUT_MD}")
    print(f"wrote {OUT_JSON}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
