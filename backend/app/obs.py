"""请求上下文。request_id 进日志；LLM usage 先累加，workflow 再写入快照。"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

logger = logging.getLogger(__name__)

request_id_var: ContextVar[str] = ContextVar("request_id", default="")
run_id_var: ContextVar[str] = ContextVar("run_id", default="")


class _Bucket:
    def __init__(self) -> None:
        self.token_in = 0
        self.token_out = 0
        self.latency_ms = 0
        # 额度占用累计：整个请求段不清零，供 reserve_llm_call 判断上限。
        self.calls = 0
        self.retry_transport = 0  # 传输层重试次数（增量，flush 后清零）
        self.retry_schema = 0  # 结构化解析重试次数（增量，flush 后清零）
        self.failures = 0  # 失败的 HTTP 尝试数（增量，flush 后清零）
        self.flushed_calls = 0  # 已写入快照的 calls 水位，避免多次 flush 重复累加


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


def count_transport_retry() -> None:
    """记一次传输层重试（429/5xx/传输错误）。不占事件数，只用于观测。"""
    bucket = _bucket.get()
    if bucket is None:
        return
    bucket.retry_transport += 1


def count_schema_retry() -> None:
    """记一次结构化解析重试（JSON/schema 不合法后的第二次请求）。"""
    bucket = _bucket.get()
    if bucket is None:
        return
    bucket.retry_schema += 1


def count_failure() -> None:
    """记一次失败的 HTTP 尝试（含重试中间的失败与超时；解析失败不计，它没有 HTTP 错误）。"""
    bucket = _bucket.get()
    if bucket is None:
        return
    bucket.failures += 1


def add_llm_usage(prompt_tokens: int, completion_tokens: int, latency_ms: int) -> None:
    """没有 bind_request 时丢掉，避免评测脚本之外的调用写到别人的快照。"""
    bucket = _bucket.get()
    if bucket is None:
        return
    bucket.token_in += int(prompt_tokens or 0)
    bucket.token_out += int(completion_tokens or 0)
    bucket.latency_ms += int(latency_ms or 0)


def flush_metrics(snapshot) -> None:
    """把本执行段的增量加进快照并清零增量，避免同一次保存加两次。

    snapshot 上的 token / 调用数是「线程累计值」，bucket 里的是「本次执行段增量」：
    每次 flush 只把增量追加一次。calls 是整段额度占用累计、不能清零，用
    flushed_calls 水位记录已写进快照的部分，保证多次 flush 不重复累加 llc_calls。
    失败尝试只在真正发出并失败的 HTTP 请求上打点，解析失败不计入 llm_failures；
    token_in/out/latency 只统计拿到成功响应的调用，失败请求成本自然为 0。
    """
    bucket = _bucket.get()
    if bucket is None:
        return
    snapshot.token_in += bucket.token_in
    snapshot.token_out += bucket.token_out
    snapshot.latency_ms += bucket.latency_ms
    snapshot.llm_calls += bucket.calls - bucket.flushed_calls
    snapshot.transport_retries += bucket.retry_transport
    snapshot.schema_retries += bucket.retry_schema
    snapshot.llm_failures += bucket.failures
    if bucket.calls or bucket.retry_transport or bucket.retry_schema or bucket.failures:
        logger.info(
            "llm usage calls=%s transport_retry=%s schema_retry=%s failures=%s",
            bucket.calls,
            bucket.retry_transport,
            bucket.retry_schema,
            bucket.failures,
        )
    # 增量字段清零；calls / flushed_calls 保留，额度累计不能丢。
    bucket.flushed_calls = bucket.calls
    bucket.token_in = 0
    bucket.token_out = 0
    bucket.latency_ms = 0
    bucket.retry_transport = 0
    bucket.retry_schema = 0
    bucket.failures = 0


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
