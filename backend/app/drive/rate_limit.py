"""Shared Drive API concurrency limits (list + download)."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from app.config import get_settings

_download_sem: asyncio.Semaphore | None = None
_download_sem_n: int | None = None
_list_sem: asyncio.Semaphore | None = None
_list_sem_n: int | None = None


def _sem(current: asyncio.Semaphore | None, current_n: int | None, n: int) -> tuple[asyncio.Semaphore, int]:
    n = max(1, int(n))
    if current is None or current_n != n:
        return asyncio.Semaphore(n), n
    return current, current_n


def drive_download_semaphore() -> asyncio.Semaphore:
    """Process-wide cap on concurrent Drive media downloads."""
    global _download_sem, _download_sem_n
    settings = get_settings()
    _download_sem, _download_sem_n = _sem(
        _download_sem, _download_sem_n, settings.drive_download_max_concurrent
    )
    return _download_sem


def drive_list_semaphore() -> asyncio.Semaphore:
    """Process-wide cap on concurrent Drive folder-list API calls."""
    global _list_sem, _list_sem_n
    settings = get_settings()
    _list_sem, _list_sem_n = _sem(
        _list_sem, _list_sem_n, settings.drive_list_max_concurrent
    )
    return _list_sem


@asynccontextmanager
async def drive_download_slot() -> AsyncIterator[None]:
    async with drive_download_semaphore():
        yield


@asynccontextmanager
async def drive_list_slot() -> AsyncIterator[None]:
    async with drive_list_semaphore():
        yield
