"""CI 回放 hard cassette。不连接真实 DeepSeek。"""

import json
from pathlib import Path

import pytest
import respx
from httpx import Response

from app.agent.prompts import PROMPT_VERSION
from app.llm.client import llm_client
from evals.cassette import last_user_text, load_cassettes, user_hash
from evals.runner import load_hard_cases, run_case
from evals.score import score_case

EXPECTED_PATH = Path(__file__).resolve().parents[1] / "evals" / "hard_expected.json"


def _enable(monkeypatch):
    monkeypatch.setattr(llm_client, "provider", "deepseek")
    monkeypatch.setattr("app.llm.client.settings.deepseek_api_key", "sk-test")
    monkeypatch.setattr("app.llm.client.settings.deepseek_base_url", "https://api.deepseek.com")


@pytest.mark.asyncio
async def test_hard_replay(tmp_path, monkeypatch):
    entries, versions = load_cassettes()
    assert entries, "缺少 cassette，先运行 python -m evals.record_llm"
    assert versions
    assert all(version == PROMPT_VERSION for version in versions)
    expected = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    _enable(monkeypatch)

    def respond(request):
        payload = json.loads(request.content)
        message = entries.get(user_hash(last_user_text(payload)))
        if message is None:
            return Response(500, json={"error": "missing cassette"})
        return Response(200, json={"choices": [{"message": message}]})

    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(side_effect=respond)
        for mode in ("llm", "llm_only"):
            for case in load_hard_cases():
                result = await run_case(case, tmp_path, mode=mode)
                row = score_case(
                    case,
                    result["snapshot"],
                    result["seen"],
                    result["scored_items"],
                    result["stakeholders"],
                )
                floor = expected[mode][case["id"]]
                assert row["recall"] >= floor["min_recall"]
                assert row["precision"] >= floor["min_precision"]
                assert row["evidence_valid_rate"] >= floor["min_evidence_valid_rate"]
                assert row["owner_hallucination_rate"] <= floor["max_owner_hallucination_rate"]
                assert result["prompt_version"] == PROMPT_VERSION
                if case["id"] == "injection":
                    assert all(item.primary_owner != "张三" for item in result["scored_items"])
