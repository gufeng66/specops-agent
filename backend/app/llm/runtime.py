"""进程级 HTTP 客户端和并发槽。lifespan 创建，退出时关闭。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx

from app.config import settings

_client: httpx.AsyncClient | None = None
_sem: asyncio.Semaphore | None = None
_sem_size = 0


def start_llm_runtime() -> None:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(settings.llm_timeout_seconds))
    _semaphore()


async def stop_llm_runtime() -> None:
    global _client, _sem, _sem_size
    if _client is not None:
        await _client.aclose()
    _client = None
    _sem = None
    _sem_size = 0


def shared_client() -> httpx.AsyncClient | None:
    return _client


def _semaphore() -> asyncio.Semaphore:
    global _sem, _sem_size
    size = max(1, settings.llm_max_concurrency)
    if _sem is None or _sem_size != size:
        _sem = asyncio.Semaphore(size)
        _sem_size = size
    return _sem


@asynccontextmanager
async def http_slot():
    """只包住实际 HTTP。退避发生在调用方、槽外。"""
    sem = _semaphore()
    await sem.acquire()
    try:
        yield
    finally:
        sem.release()
