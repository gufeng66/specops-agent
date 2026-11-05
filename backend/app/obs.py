"""请求上下文。request_id 进日志；LLM usage 先累加，workflow 再写入快照。"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

request_id_var: ContextVar[str] = ContextVar("request_id", default="")
run_id_var: ContextVar[str] = ContextVar("run_id", default="")


class _Bucket:
    def __init__(self) -> None:
        self.token_in = 0
        self.token_out = 0
        self.latency_ms = 0
        self.calls = 0


_bucket: ContextVar[_Bucket | None] = ContextVar("llm_bucket", default=None)


@contextmanager
def bind_request(request_id: str, run_id: str) -> Iterator[_Bucket]:
    """一次 SSE / resume 的范围。离开后 contextvar 复位，避免串到下一个请求。"""
    token_req = request_id_var.set(request_id)
    token_run = run_id_var.set(run_id)
    bucket = _Bucket()
    token_bucket = _bucket.set(bucket)
    try:
        yield bucket
    finally:
        request_id_var.reset(token_req)
        run_id_var.reset(token_run)
        _bucket.reset(token_bucket)


def reserve_llm_call(limit: int) -> None:
    """每次真实 HTTP 前占用额度，含传输重试和 schema 重试。无请求桶时不限制。"""
    bucket = _bucket.get()
    if bucket is None:
        return
    if bucket.calls >= limit:
        raise RuntimeError("llm call budget exhausted")
    bucket.calls += 1


def add_llm_usage(prompt_tokens: int, completion_tokens: int, latency_ms: int) -> None:
    """没有 bind_request 时丢掉，避免评测脚本之外的调用写到别人的快照。"""
    bucket = _bucket.get()
    if bucket is None:
        return
    bucket.token_in += int(prompt_tokens or 0)
    bucket.token_out += int(completion_tokens or 0)
    bucket.latency_ms += int(latency_ms or 0)


def flush_metrics(snapshot) -> None:
    """把本请求的增量加进快照并清零，避免同一次保存加两次。"""
    bucket = _bucket.get()
    if bucket is None:
        return
    snapshot.token_in += bucket.token_in
    snapshot.token_out += bucket.token_out
    snapshot.latency_ms += bucket.latency_ms
    bucket.token_in = 0
    bucket.token_out = 0
    bucket.latency_ms = 0


class RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()  # type: ignore[attr-defined]
        record.run_id = run_id_var.get()  # type: ignore[attr-defined]
        return True


class JsonFormatter(logging.Formatter):
    """一行一条 JSON。不加 structlog。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", ""),
            "run_id": getattr(record, "run_id", ""),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    """挂到 root。重复调用不叠 handler。"""
    root = logging.getLogger()
    if any(getattr(handler, "_specops_json", False) for handler in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestContextFilter())
    handler._specops_json = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
