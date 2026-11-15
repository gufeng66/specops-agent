"""usage 进快照，日志带 request_id。不打真网。"""

import logging

import pytest
import respx
from httpx import Response
from pydantic import BaseModel

from app.domain.models import ThreadSnapshot
from app.llm.client import LLMClient
from app.obs import (
    JsonFormatter,
    RequestContextFilter,
    bind_request,
    count_failure,
    count_schema_retry,
    count_transport_retry,
    flush_metrics,
    reserve_llm_call,
)


class _ValueModel(BaseModel):
    value: str  # schema 重试测试用的最小模型，含 required 字段


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


def test_flush_metrics_writes_call_counters_and_no_double_count():
    snap = ThreadSnapshot(thread_id="t-metrics")
    with bind_request("req-metrics", "run-metrics") as bucket:
        # 模拟一次执行段：2 次成功 HTTP 尝试、1 次传输重试、1 次 schema 重试、1 次失败。
        for _ in range(2):
            reserve_llm_call(128)
        count_transport_retry()
        count_schema_retry()
        count_failure()
        flush_metrics(snap)
        assert snap.llm_calls == 2
        assert snap.transport_retries == 1
        assert snap.schema_retries == 1
        assert snap.llm_failures == 1
        # 同一 bucket 里再 flush 一次：增量已清零，calls 由水位保护不重复累加。
        flush_metrics(snap)
        assert snap.llm_calls == 2  # 没有变成 4
        assert snap.transport_retries == 1
        assert snap.schema_retries == 1
        assert snap.llm_failures == 1
        assert bucket.calls == 2  # 额度累计保留
        assert bucket.flushed_calls == 2


@pytest.mark.asyncio
async def test_transport_retry_and_failure_are_observed(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr("app.llm.client.settings.llm_max_retries", 1)
    client = LLMClient()
    snap = ThreadSnapshot(thread_id="t-transport")
    with bind_request("req-transport", "run-transport"):
        with respx.mock:
            route = respx.post("https://api.deepseek.com/chat/completions")
            route.side_effect = [
                Response(500, text="no"),  # 第一次失败（可重试）
                Response(
                    200,
                    json={
                        "choices": [{"message": {"content": "{}"}}],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
                    },
                ),
            ]
            await client.post_completions({"messages": []}, "intake")
        flush_metrics(snap)
    assert snap.llm_calls == 2  # 失败 + 成功，各占一次额度
    assert snap.transport_retries == 1
    assert snap.llm_failures == 1
    assert snap.token_in == 5  # 只有成功响应产生 token；失败尝试成本为 0
    assert snap.latency_ms >= 1


@pytest.mark.asyncio
async def test_final_failure_is_observed(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr("app.llm.client.settings.llm_max_retries", 0)
    client = LLMClient()
    snap = ThreadSnapshot(thread_id="t-final-fail")
    with bind_request("req-final-fail", "run-final-fail"):
        with respx.mock:
            respx.post("https://api.deepseek.com/chat/completions").mock(
                return_value=Response(500, text="no")
            )
            with pytest.raises(Exception, match="HTTP 500"):
                await client.post_completions({"messages": []}, "intake")
        flush_metrics(snap)
    assert snap.llm_calls == 1
    assert snap.llm_failures == 1
    assert snap.transport_retries == 0  # 不可重试/最后一次不计重试次数
    assert snap.token_in == 0  # 失败请求不产生 token
    assert snap.latency_ms == 0


@pytest.mark.asyncio
async def test_schema_retry_is_observed(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr("app.llm.client.settings.llm_max_retries", 0)
    client = LLMClient()
    snap = ThreadSnapshot(thread_id="t-schema")
    with bind_request("req-schema", "run-schema"):
        with respx.mock:
            route = respx.post("https://api.deepseek.com/chat/completions")
            route.side_effect = [
                # 第一次：合法 JSON 但不是目标 schema，触发一次 schema 重试
                Response(
                    200,
                    json={
                        "choices": [{"message": {"content": '{"unexpected": 1}'}}],
                        "usage": {"prompt_tokens": 4, "completion_tokens": 2},
                    },
                ),
                Response(
                    200,
                    json={
                        "choices": [{"message": {"content": '{"value": "ok"}'}}],
                        "usage": {"prompt_tokens": 6, "completion_tokens": 3},
                    },
                ),
            ]
            parsed = await client.chat_json(
                system="s", user="u", model_cls=_ValueModel, node="intake"
            )
            assert parsed.value == "ok"
        flush_metrics(snap)
    assert snap.llm_calls == 2  # 第一次 + schema 修正请求
    assert snap.schema_retries == 1
    assert snap.llm_failures == 0  # 两次 HTTP 都成功，解析失败不是 HTTP 失败
    assert snap.token_in == 10
    assert snap.token_out == 5
