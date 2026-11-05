"""usage 进快照，日志带 request_id。不打真网。"""

import logging

import pytest
import respx
from httpx import Response

from app.domain.models import ThreadSnapshot
from app.llm.client import LLMClient
from app.obs import JsonFormatter, RequestContextFilter, bind_request, flush_metrics


def _enable(monkeypatch):
    monkeypatch.setattr("app.llm.client.settings.llm_provider", "deepseek")
    monkeypatch.setattr("app.llm.client.settings.deepseek_api_key", "sk-test")
    monkeypatch.setattr("app.llm.client.settings.deepseek_base_url", "https://api.deepseek.com")
    monkeypatch.setattr("app.llm.client.settings.llm_retry_base_seconds", 0)


@pytest.mark.asyncio
async def test_usage_flushes_onto_snapshot(monkeypatch):
    _enable(monkeypatch)
    client = LLMClient()
    snap = ThreadSnapshot(thread_id="t-usage")
    with bind_request("req-usage", "run-usage"):
        with respx.mock:
            respx.post("https://api.deepseek.com/chat/completions").mock(
                return_value=Response(
                    200,
                    json={
                        "choices": [{"message": {"content": "{}"}}],
                        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                    },
                )
            )
            await client.post_completions({"messages": [{"role": "user", "content": "hi"}]}, "intake")
        flush_metrics(snap)
    assert snap.token_in == 11
    assert snap.token_out == 7
    assert snap.latency_ms >= 1


def test_json_log_carries_request_id():
    record = logging.LogRecord("app.agent.workflow", logging.INFO, __file__, 1, "chat start", (), None)
    with bind_request("req-9", "run-9"):
        assert RequestContextFilter().filter(record)
        line = JsonFormatter().format(record)
    assert '"request_id": "req-9"' in line
    assert '"run_id": "run-9"' in line


@pytest.mark.asyncio
async def test_decompose_limits_concurrency(monkeypatch):
    import asyncio

    from app.llm.runtime import http_slot, stop_llm_runtime

    monkeypatch.setattr("app.config.settings.llm_max_concurrency", 2)
    await stop_llm_runtime()
    current = 0
    peak = 0

    async def worker():
        nonlocal current, peak
        async with http_slot():
            current += 1
            peak = max(peak, current)
            await asyncio.sleep(0.05)
            current -= 1

    await asyncio.gather(*[worker() for _ in range(4)])
    assert peak == 2


@pytest.mark.asyncio
async def test_call_budget_counts_retries(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr("app.llm.client.settings.llm_max_calls_per_run", 1)
    monkeypatch.setattr("app.llm.client.settings.llm_max_retries", 1)
    client = LLMClient()
    with bind_request("req-budget", "run-budget"):
        with respx.mock:
            respx.post("https://api.deepseek.com/chat/completions").mock(
                return_value=Response(500, text="no")
            )
            with pytest.raises(Exception, match="budget|HTTP 500"):
                await client.post_completions({"messages": []}, "intake")
