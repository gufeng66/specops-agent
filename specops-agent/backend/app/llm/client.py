from __future__ import annotations

import json
import logging
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.agent.prompts import (
    DECOMPOSE_SYSTEM,
    DECOMPOSE_USER,
    INTAKE_SYSTEM,
    INTAKE_USER,
    REPAIR_SYSTEM,
    REPAIR_USER,
)
from app.config import settings
from app.domain.models import ActionItemList, MeetingBriefExtract, TaskDraft, ValidationResult
from app.llm.errors import LLMAPIError, LLMConfigError, LLMError, LLMParseError, LLMTimeoutError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMClient:
    def __init__(self):
        self.provider = settings.llm_provider.lower()

    @property
    def enabled(self) -> bool:
        return self.provider not in ("none", "") and bool(settings.deepseek_api_key)

    async def structured_extract_brief(self, source_text: str) -> MeetingBriefExtract | None:
        if not self.enabled:
            return None
        try:
            return await self.chat_json(
                system=INTAKE_SYSTEM,
                user=INTAKE_USER.format(source_text=source_text[:4000]),
                model_cls=MeetingBriefExtract,
                node="intake",
            )
        except LLMError as exc:
            logger.warning("LLM intake failed [%s]: %s — falling back to rules", exc.code, exc.message)
            return None

    async def structured_decompose(self, source_text: str, brief_json: str) -> ActionItemList | None:
        if not self.enabled:
            return None
        try:
            return await self.chat_json(
                system=DECOMPOSE_SYSTEM,
                user=DECOMPOSE_USER.format(brief_json=brief_json, source_text=source_text[:4000]),
                model_cls=ActionItemList,
                node="decompose",
            )
        except LLMError as exc:
            logger.warning(
                "LLM decompose failed [%s]: %s — falling back to rules", exc.code, exc.message
            )
            return None

    async def structured_repair_suggest(
        self,
        source_text: str,
        draft: TaskDraft,
        validation: ValidationResult,
    ) -> ActionItemList | None:
        """Optional LLM repair suggestions; caller may ignore and use rule repair."""
        if not self.enabled:
            return None
        try:
            return await self.chat_json(
                system=REPAIR_SYSTEM,
                user=REPAIR_USER.format(
                    source_text=source_text[:4000],
                    draft_json=draft.model_dump_json(),
                    issues_json=validation.model_dump_json(),
                ),
                model_cls=ActionItemList,
                node="llm_repair",
            )
        except LLMError as exc:
            logger.warning("LLM repair suggest failed [%s]: %s", exc.code, exc.message)
            return None

    async def chat_json(
        self,
        *,
        system: str,
        user: str,
        model_cls: type[T],
        node: str = "llm",
    ) -> T:
        if self.provider not in ("deepseek",):
            raise LLMConfigError(f"Unsupported LLM_PROVIDER={self.provider}")
        if not settings.deepseek_api_key:
            raise LLMConfigError("DEEPSEEK_API_KEY is empty")

        url = f"{settings.deepseek_base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": settings.deepseek_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        }
        headers = {"Authorization": f"Bearer {settings.deepseek_api_key}"}

        try:
            async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
                resp = await client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"{node}: timeout after {settings.llm_timeout_seconds}s") from exc
        except httpx.HTTPError as exc:
            raise LLMAPIError(f"{node}: transport error: {exc}") from exc

        if resp.status_code >= 400:
            raise LLMAPIError(
                f"{node}: HTTP {resp.status_code}: {resp.text[:300]}",
                status_code=resp.status_code,
            )

        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LLMParseError(f"{node}: unexpected response shape") from exc

        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMParseError(f"{node}: content is not valid JSON") from exc

        try:
            return model_cls.model_validate(data)
        except ValidationError as exc:
            raise LLMParseError(f"{node}: schema validation failed: {exc.errors()[:3]}") from exc


llm_client = LLMClient()
