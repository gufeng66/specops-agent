"""LLM 客户端：thinking=disabled、墙钟超时。不打真实 Key。"""

import json

import httpx
import pytest
import respx
from httpx import Response

from app.domain.models import MeetingBriefExtract
from app.llm.client import LLMClient
from app.llm.errors import LLMAPIError, LLMTimeoutError


@pytest.mark.asyncio
async def test_post_completions_disables_thinking(monkeypatch):
    monkeypatch.setattr("app.llm.client.settings.llm_provider", "deepseek")
    monkeypatch.setattr("app.llm.client.settings.deepseek_api_key", "sk-test")
    monkeypatch.setattr("app.llm.client.settings.deepseek_base_url", "https://api.deepseek.com")
    monkeypatch.setattr("app.llm.client.settings.deepseek_model", "deepseek-v4-flash")

    client = LLMClient()
    captured: dict = {}

    def _capture(request):
        captured.update(json.loads(request.content))
        return Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(side_effect=_capture)
        await client.post_completions(
            {
                "model": "deepseek-v4-flash",
                "messages": [{"role": "user", "content": "hi"}],
            },
            "intake",
        )

    assert captured["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_shared_client_post_uses_configured_timeout(monkeypatch):
    monkeypatch.setattr("app.llm.client.settings.llm_provider", "deepseek")
    monkeypatch.setattr("app.llm.client.settings.deepseek_api_key", "sk-test")
    monkeypatch.setattr("app.llm.client.settings.deepseek_base_url", "https://api.deepseek.com")
    monkeypatch.setattr("app.llm.client.settings.llm_timeout_seconds", 30)
    captured: dict = {}

    class Shared:
        async def post(self, url, headers=None, json=None, timeout=None):
            captured["timeout"] = timeout
            return Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    monkeypatch.setattr("app.llm.client.shared_client", lambda: Shared())
    client = LLMClient()
    await client.post_completions({"messages": [{"role": "user", "content": "hi"}]}, "intake")
    assert captured["timeout"] == 30


@pytest.mark.asyncio
async def test_post_completions_wall_clock_timeout(monkeypatch):
    monkeypatch.setattr("app.llm.client.settings.llm_provider", "deepseek")
    monkeypatch.setattr("app.llm.client.settings.deepseek_api_key", "sk-test")
    monkeypatch.setattr("app.llm.client.settings.deepseek_base_url", "https://api.deepseek.com")
    monkeypatch.setattr("app.llm.client.settings.llm_timeout_seconds", 0.01)

    client = LLMClient()

    async def _hang(_request):
        raise httpx.ReadTimeout("slow thinking tokens")

    calls = {"n": 0}

    async def _hang(_request):
        calls["n"] += 1
        raise httpx.ReadTimeout("slow thinking tokens")

    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(side_effect=_hang)
        with pytest.raises(LLMTimeoutError):
            await client.post_completions({"messages": []}, "decompose")
    assert calls["n"] == 1


def _enable(monkeypatch):
    monkeypatch.setattr("app.llm.client.settings.llm_provider", "deepseek")
    monkeypatch.setattr("app.llm.client.settings.deepseek_api_key", "sk-test")
    monkeypatch.setattr("app.llm.client.settings.deepseek_base_url", "https://api.deepseek.com")
    monkeypatch.setattr("app.llm.client.settings.deepseek_model", "deepseek-v4-flash")
    monkeypatch.setattr("app.llm.client.settings.llm_max_retries", 2)
    monkeypatch.setattr("app.llm.client.settings.llm_retry_base_seconds", 0)
    monkeypatch.setattr("app.llm.client.settings.llm_retry_budget_seconds", 8)


@pytest.mark.asyncio
async def test_chat_json_retries_invalid_json_once(monkeypatch):
    _enable(monkeypatch)
    client = LLMClient()
    calls = {"n": 0}
    ok = json.dumps(
        {"brief": {"goal": "发布", "stakeholders": [], "meeting_date": "", "context": ""}},
        ensure_ascii=False,
    )

    def respond(_request):
        calls["n"] += 1
        content = "not-json" if calls["n"] == 1 else ok
        return Response(200, json={"choices": [{"message": {"content": content}}]})

    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(side_effect=respond)
        parsed = await client.chat_json(
            system="sys",
            user="user",
            model_cls=MeetingBriefExtract,
            node="intake",
        )
    assert calls["n"] == 2
    assert parsed.brief.goal == "发布"


@pytest.mark.asyncio
async def test_chat_json_does_not_retry_http_error(monkeypatch):
    _enable(monkeypatch)
    client = LLMClient()
    calls = {"n": 0}

    def respond(_request):
        calls["n"] += 1
        return Response(400, json={"error": "bad"})

    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(side_effect=respond)
        with pytest.raises(LLMAPIError):
            await client.chat_json(
                system="sys",
                user="user",
                model_cls=MeetingBriefExtract,
                node="intake",
            )
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_post_retries_429_then_ok(monkeypatch):
    _enable(monkeypatch)
    client = LLMClient()
    calls = {"n": 0}

    def respond(_request):
        calls["n"] += 1
        if calls["n"] == 1:
            return Response(429, json={"error": "slow down"})
        return Response(
            200,
            json={
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        )

    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(side_effect=respond)
        data = await client.post_completions({"messages": []}, "intake")
    assert calls["n"] == 2
    assert data["usage"]["prompt_tokens"] == 3


@pytest.mark.asyncio
async def test_post_stops_after_max_retries(monkeypatch):
    _enable(monkeypatch)
    client = LLMClient()
    calls = {"n": 0}

    def respond(_request):
        calls["n"] += 1
        return Response(503, json={"error": "down"})

    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(side_effect=respond)
        with pytest.raises(LLMAPIError):
            await client.post_completions({"messages": []}, "intake")
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_chat_json_http_cap_is_two_schema_rounds(monkeypatch):
    """schema 两轮 × (1 + llm_max_retries) 次 HTTP。这里 max_retries=1 → 4。"""
    _enable(monkeypatch)
    monkeypatch.setattr("app.llm.client.settings.llm_max_retries", 1)
    client = LLMClient()
    calls = {"n": 0}

    def respond(_request):
        calls["n"] += 1
        if calls["n"] in (1, 3):
            return Response(500, json={"error": "blip"})
        return Response(200, json={"choices": [{"message": {"content": "not-json"}}]})

    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(side_effect=respond)
        with pytest.raises(Exception):
            await client.chat_json(
                system="sys",
                user="user",
                model_cls=MeetingBriefExtract,
                node="intake",
            )
    assert calls["n"] == 2 * (1 + 1)
