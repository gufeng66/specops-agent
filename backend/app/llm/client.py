"""DeepSeek Chat Completions 客户端。

enabled=False 时结构化方法返回 None，节点走规则。thinking 强制关闭，避免 Flash 超时。
propose_owners 失败返回 None（空建议），不抛到图外。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.agent.prompts import (
    DECOMPOSE_SYSTEM,
    DECOMPOSE_USER,
    INTAKE_SYSTEM,
    INTAKE_USER,
    PROPOSE_OWNERS_SYSTEM,
    PROPOSE_OWNERS_USER,
    TOOL_SCHEMAS,
)
from app.config import settings
from app.domain.chunking import sample_for_brief
from app.domain.models import ActionItemList, MeetingBriefExtract, OwnerSuggestionBatch
from app.llm.errors import LLMAPIError, LLMConfigError, LLMError, LLMParseError, LLMTimeoutError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMClient:
    def __init__(self):
        self.provider = settings.llm_provider.lower()

    @property
    def enabled(self) -> bool:
        return self.provider not in ("none", "") and bool(settings.deepseek_api_key)

    def _completions_url(self) -> str:
        return f"{settings.deepseek_base_url.rstrip('/')}/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {settings.deepseek_api_key}"}

    def _with_thinking_disabled(self, payload: dict[str, Any]) -> dict[str, Any]:
        """V4 Flash 默认 thinking=high，CoT 会拖过读超时，强制关掉。"""
        payload.setdefault("thinking", {"type": "disabled"})
        return payload

    async def post_completions(self, payload: dict[str, Any], node: str) -> dict[str, Any]:
        """POST chat/completions。wait_for 卡墙钟；httpx 读超时只在分块之间重置。"""
        if self.provider not in ("deepseek",):
            raise LLMConfigError(f"Unsupported LLM_PROVIDER={self.provider}")
        if not settings.deepseek_api_key:
            raise LLMConfigError("DEEPSEEK_API_KEY is empty")

        timeout_s = settings.llm_timeout_seconds
        payload = self._with_thinking_disabled(payload)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
                # wait_for caps wall-clock time; httpx read timeout only resets between chunks.
                resp = await asyncio.wait_for(
                    client.post(self._completions_url(), headers=self._headers(), json=payload),
                    timeout=timeout_s,
                )
        except TimeoutError as exc:
            raise LLMTimeoutError(f"{node}: timeout after {timeout_s}s") from exc
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"{node}: timeout after {timeout_s}s") from exc
        except httpx.HTTPError as exc:
            raise LLMAPIError(f"{node}: transport error: {exc}") from exc

        if resp.status_code >= 400:
            raise LLMAPIError(
                f"{node}: HTTP {resp.status_code}: {resp.text[:300]}",
                status_code=resp.status_code,
            )
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise LLMParseError(f"{node}: response is not JSON") from exc

    async def structured_extract_brief(self, source_text: str) -> MeetingBriefExtract | None:
        """intake：失败返回 None，由节点保留规则底稿。"""
        if not self.enabled:
            return None
        try:
            window = sample_for_brief(source_text, settings.llm_brief_max_chars)
            return await self.chat_json(
                system=INTAKE_SYSTEM,
                user=INTAKE_USER.format(source_text=window),
                model_cls=MeetingBriefExtract,
                node="intake",
            )
        except LLMError as exc:
            logger.warning("LLM intake failed [%s]: %s — falling back to rules", exc.code, exc.message)
            return None

    async def structured_decompose(self, source_text: str, brief_json: str) -> ActionItemList | None:
        """单块拆解。超长块截断到 llm_chunk_chars，完整长文由节点分块 gather。"""
        if not self.enabled:
            return None
        try:
            window = source_text
            cap = settings.llm_chunk_chars + settings.llm_chunk_overlap
            if len(window) > cap:
                window = window[: settings.llm_chunk_chars]
            return await self.chat_json(
                system=DECOMPOSE_SYSTEM,
                user=DECOMPOSE_USER.format(brief_json=brief_json, source_text=window),
                model_cls=ActionItemList,
                node="decompose",
            )
        except LLMError as exc:
            logger.warning(
                "LLM decompose failed [%s]: %s — falling back to rules", exc.code, exc.message
            )
            return None

    async def structured_propose_owners(
        self,
        *,
        brief_json: str,
        pending_json: str,
        windows: str,
    ) -> OwnerSuggestionBatch | None:
        """只产出建议，不写主责。解析/网络失败返回 None。"""
        if not self.enabled:
            return None
        try:
            return await self.chat_json(
                system=PROPOSE_OWNERS_SYSTEM,
                user=PROPOSE_OWNERS_USER.format(
                    brief_json=brief_json,
                    pending_json=pending_json,
                    windows=windows,
                ),
                model_cls=OwnerSuggestionBatch,
                node="propose_owners",
            )
        except LLMError as exc:
            logger.warning(
                "LLM propose_owners failed [%s]: %s — returning empty suggestions",
                exc.code,
                exc.message,
            )
            return None

    async def chat_json(
        self,
        *,
        system: str,
        user: str,
        model_cls: type[T],
        node: str = "llm",
    ) -> T:
        """json_object 一轮：解析 content 再 Pydantic 校验。"""
        payload = {
            "model": settings.deepseek_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        }
        data = await self.post_completions(payload, node)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMParseError(f"{node}: unexpected response shape") from exc

        try:
            parsed = json.loads(content)
        except (TypeError, json.JSONDecodeError) as exc:
            raise LLMParseError(f"{node}: content is not valid JSON") from exc

        try:
            return model_cls.model_validate(parsed)
        except ValidationError as exc:
            raise LLMParseError(f"{node}: schema validation failed: {exc.errors()[:3]}") from exc

    async def chat_tools(
        self,
        *,
        messages: list[dict[str, Any]],
        node: str = "validate_repair",
    ) -> dict[str, Any]:
        """工具循环一轮：返回 assistant message（可能带 tool_calls）。"""
        payload = {
            "model": settings.deepseek_model,
            "messages": messages,
            "tools": TOOL_SCHEMAS,
            "tool_choice": "auto",
            "temperature": 0.1,
        }
        data = await self.post_completions(payload, node)
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMParseError(f"{node}: unexpected tools response shape") from exc
        if not isinstance(message, dict):
            raise LLMParseError(f"{node}: assistant message is not an object")
        return message


llm_client = LLMClient()
