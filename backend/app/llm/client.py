"""DeepSeek Chat Completions 客户端。

enabled=False 时结构化方法返回 None，节点走规则。thinking 强制关闭，避免 Flash 超时。
propose_owners 失败返回 None（空建议），不抛到图外。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
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
from app.llm.runtime import http_slot, shared_client
from app.obs import add_llm_usage, reserve_llm_call

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
        """POST chat/completions。超时不重试。429/5xx/传输错误最多再试 llm_max_retries 次。

        chat_json 另有一轮 schema 重试，因此一次逻辑抽取的 HTTP 上限是
        2 * (1 + llm_max_retries)。退避睡眠总和不超过 llm_retry_budget_seconds。
        """
        if self.provider not in ("deepseek",):
            raise LLMConfigError(f"Unsupported LLM_PROVIDER={self.provider}")
        if not settings.deepseek_api_key:
            raise LLMConfigError("DEEPSEEK_API_KEY is empty")

        payload = self._with_thinking_disabled(payload)
        attempts = 1 + max(0, settings.llm_max_retries)
        slept = 0.0
        last_error: LLMAPIError | None = None
        for attempt in range(attempts):
            try:
                reserve_llm_call(settings.llm_max_calls_per_run)
            except RuntimeError as exc:
                raise LLMAPIError(str(exc), status_code=429) from exc
            started = time.perf_counter()
            try:
                async with http_slot():
                    data = await self._post_once(payload, node)
            except LLMTimeoutError:
                raise
            except LLMAPIError as exc:
                last_error = exc
                if not _retryable_status(exc.status_code) or attempt == attempts - 1:
                    raise
                slept += await self._backoff(attempt, slept)
                continue
            usage = data.get("usage") or {}
            elapsed_ms = max(1, int((time.perf_counter() - started) * 1000))
            add_llm_usage(
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
                elapsed_ms,
            )
            logger.info("llm ok node=%s latency_ms=%s", node, elapsed_ms)
            return data
        raise last_error or LLMAPIError(f"{node}: retries exhausted")

    async def _post_once(self, payload: dict[str, Any], node: str) -> dict[str, Any]:
        timeout_s = settings.llm_timeout_seconds
        client = shared_client()
        owned = client is None
        if owned:
            client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s))
        try:
            try:
                resp = await asyncio.wait_for(
                    client.post(
                        self._completions_url(),
                        headers=self._headers(),
                        json=payload,
                        timeout=timeout_s,
                    ),
                    timeout=timeout_s,
                )
            except TimeoutError as exc:
                raise LLMTimeoutError(f"{node}: timeout after {timeout_s}s") from exc
            except httpx.TimeoutException as exc:
                raise LLMTimeoutError(f"{node}: timeout after {timeout_s}s") from exc
            except httpx.HTTPError as exc:
                raise LLMAPIError(f"{node}: transport error: {exc}") from exc
        finally:
            if owned:
                await client.aclose()
        if resp.status_code >= 400:
            raise LLMAPIError(
                f"{node}: HTTP {resp.status_code}: {resp.text[:300]}",
                status_code=resp.status_code,
            )
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise LLMParseError(f"{node}: response is not JSON") from exc

    async def _backoff(self, attempt: int, slept: float) -> float:
        """指数退避加抖动。base 为 0 或预算用尽时不睡。"""
        room = max(0.0, float(settings.llm_retry_budget_seconds) - slept)
        raw = float(settings.llm_retry_base_seconds) * (2**attempt) * (0.5 + random.random())
        delay = min(raw, room)
        if delay > 0:
            await asyncio.sleep(delay)
        return delay

    async def structured_extract_brief(
        self, source_text: str, revision_notes: list[str] | None = None
    ) -> MeetingBriefExtract | None:
        """intake：失败返回 None，由节点保留规则底稿。"""
        if not self.enabled:
            return None
        try:
            window = sample_for_brief(source_text, settings.llm_brief_max_chars)
            return await self.chat_json(
                system=INTAKE_SYSTEM,
                user=INTAKE_USER.format(source_text=window) + _note_block(revision_notes),
                model_cls=MeetingBriefExtract,
                node="intake",
            )
        except LLMError as exc:
            logger.warning("LLM intake failed [%s]: %s — falling back to rules", exc.code, exc.message)
            return None

    async def structured_decompose(
        self,
        source_text: str,
        brief_json: str,
        revision_notes: list[str] | None = None,
    ) -> ActionItemList | None:
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
                user=DECOMPOSE_USER.format(brief_json=brief_json, source_text=window)
                + _note_block(revision_notes),
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
        """json_object。JSON 或 schema 失败时再请求一次。每轮各自走传输重试。"""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        data = await self.post_completions(_json_payload(messages), node)
        parsed, content, kind, detail = _parse_json_choice(data, model_cls)
        if kind == "ok":
            return parsed
        if kind == "shape":
            raise LLMParseError(f"{node}: unexpected response shape")
        messages.append(
            {
                "role": "user",
                "content": (
                    "上次输出无法通过校验，请只输出修正后的 JSON 对象。\n"
                    f"原始输出：\n{content}\n错误：\n{detail}"
                ),
            }
        )
        data = await self.post_completions(_json_payload(messages), node)
        parsed, _content, kind, detail = _parse_json_choice(data, model_cls)
        if kind == "json":
            raise LLMParseError(f"{node}: content is not valid JSON")
        if kind != "ok":
            raise LLMParseError(f"{node}: schema validation failed: {detail}")
        return parsed

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


def _note_block(notes: list[str] | None) -> str:
    """修订意见跟在提示后面，不写进会议原文。"""
    if not notes:
        return ""
    return "\n\n人工修订意见（不是会议原文，不得当作证据引用）：\n" + "\n".join(notes)


def _retryable_status(status: int | None) -> bool:
    """无状态码是传输错误。4xx 里只有 429 重试。"""
    if status is None:
        return True
    return status == 429 or status >= 500


def _json_payload(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "model": settings.deepseek_model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
    }


def _parse_json_choice(data: dict[str, Any], model_cls: type[T]):
    """返回 (模型, content, kind, detail)。kind 为 ok / shape / json / schema。"""
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None, "", "shape", "unexpected response shape"
    if not isinstance(content, str):
        return None, "", "shape", "content is not a string"
    try:
        parsed = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        return None, content, "json", str(exc)
    try:
        return model_cls.model_validate(parsed), content, "ok", ""
    except ValidationError as exc:
        return None, content, "schema", str(exc.errors()[:3])


llm_client = LLMClient()
