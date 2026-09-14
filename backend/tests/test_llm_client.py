"""LLM 客户端：thinking=disabled、墙钟超时。不打真实 Key。"""

import json

import httpx
import pytest
import respx
from httpx import Response

from app.llm.client import LLMClient
from app.llm.errors import LLMTimeoutError


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
async def test_post_completions_wall_clock_timeout(monkeypatch):
    monkeypatch.setattr("app.llm.client.settings.llm_provider", "deepseek")
    monkeypatch.setattr("app.llm.client.settings.deepseek_api_key", "sk-test")
    monkeypatch.setattr("app.llm.client.settings.deepseek_base_url", "https://api.deepseek.com")
    monkeypatch.setattr("app.llm.client.settings.llm_timeout_seconds", 0.01)

    client = LLMClient()

    async def _hang(_request):
        raise httpx.ReadTimeout("slow thinking tokens")

    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(side_effect=_hang)
        with pytest.raises(LLMTimeoutError):
            await client.post_completions({"messages": []}, "decompose")
