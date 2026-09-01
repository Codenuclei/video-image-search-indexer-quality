"""Cooperative cancellation for in-flight GET /search requests.

Clients abort the HTTP connection and POST /search/cancel with the same
search_id so the handler can skip expensive Gemini/Qdrant stages even when
the ASGI task has not yet been cancelled.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request

# Drop stale registrations so a crashed client cannot leak forever.
_TTL_SEC = 15 * 60
_MAX_ENTRIES = 2000


@dataclass
class _Entry:
    event: asyncio.Event
    created_at: float


_registry: dict[str, _Entry] = {}
_lock = asyncio.Lock()


class SearchCancelled(Exception):
    """Raised when the client aborted or explicitly cancelled the search."""


async def _prune_locked(now: float) -> None:
    stale = [sid for sid, entry in _registry.items() if now - entry.created_at > _TTL_SEC]
    for sid in stale:
        _registry.pop(sid, None)
    if len(_registry) <= _MAX_ENTRIES:
        return
    # Evict oldest first.
    ordered = sorted(_registry.items(), key=lambda item: item[1].created_at)
    for sid, _ in ordered[: max(0, len(_registry) - _MAX_ENTRIES)]:
        _registry.pop(sid, None)


async def register_search(search_id: str | None) -> None:
    sid = (search_id or "").strip()
    if not sid or len(sid) > 128:
        return
    async with _lock:
        await _prune_locked(time.monotonic())
        existing = _registry.get(sid)
        if existing is not None and existing.event.is_set():
            # Cancel arrived before GET /search registered — keep cancelled.
            existing.created_at = time.monotonic()
            return
        _registry[sid] = _Entry(event=asyncio.Event(), created_at=time.monotonic())


async def cancel_search(search_id: str | None) -> bool:
    sid = (search_id or "").strip()
    if not sid:
        return False
    async with _lock:
        entry = _registry.get(sid)
        if entry is None:
            # Register a pre-cancelled event so a late-arriving search still stops.
            _registry[sid] = _Entry(event=asyncio.Event(), created_at=time.monotonic())
            _registry[sid].event.set()
            return True
        entry.event.set()
        return True


async def clear_search(search_id: str | None) -> None:
    sid = (search_id or "").strip()
    if not sid:
        return
    async with _lock:
        _registry.pop(sid, None)


def is_search_cancelled(search_id: str | None) -> bool:
    sid = (search_id or "").strip()
    if not sid:
        return False
    entry = _registry.get(sid)
    return bool(entry and entry.event.is_set())


async def ensure_search_active(request: Request | None, search_id: str | None = None) -> None:
    """Raise SearchCancelled if the client disconnected or cancelled search_id."""
    if is_search_cancelled(search_id):
        raise SearchCancelled()
    if request is not None and await request.is_disconnected():
        raise SearchCancelled()


def cancelled_http_exception() -> HTTPException:
    return HTTPException(status_code=499, detail="Search cancelled")
