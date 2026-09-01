"""Tests for cooperative search cancellation."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.search.cancel import (
    SearchCancelled,
    cancel_search,
    clear_search,
    ensure_search_active,
    is_search_cancelled,
    register_search,
)


@pytest.mark.asyncio
async def test_cancel_registry_marks_search_cancelled() -> None:
    await clear_search("t-cancel-1")
    await register_search("t-cancel-1")
    assert is_search_cancelled("t-cancel-1") is False
    assert await cancel_search("t-cancel-1") is True
    assert is_search_cancelled("t-cancel-1") is True
    await clear_search("t-cancel-1")
    assert is_search_cancelled("t-cancel-1") is False


@pytest.mark.asyncio
async def test_cancel_before_register_still_blocks() -> None:
    await clear_search("t-cancel-early")
    assert await cancel_search("t-cancel-early") is True
    assert is_search_cancelled("t-cancel-early") is True
    await register_search("t-cancel-early")
    assert is_search_cancelled("t-cancel-early") is True
    await clear_search("t-cancel-early")


@pytest.mark.asyncio
async def test_ensure_search_active_raises_when_cancelled() -> None:
    await clear_search("t-cancel-2")
    await register_search("t-cancel-2")
    await cancel_search("t-cancel-2")
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    with pytest.raises(SearchCancelled):
        await ensure_search_active(request, "t-cancel-2")  # type: ignore[arg-type]
    await clear_search("t-cancel-2")


@pytest.mark.asyncio
async def test_ensure_search_active_raises_when_disconnected() -> None:
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=True))
    with pytest.raises(SearchCancelled):
        await ensure_search_active(request, None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_search_cancel_endpoint() -> None:
    from app.routers.search import SearchCancelRequest, search_cancel

    await clear_search("t-cancel-ep")
    result = await search_cancel(SearchCancelRequest(search_id="t-cancel-ep"))
    assert result.cancelled is True
    assert result.search_id == "t-cancel-ep"
    assert is_search_cancelled("t-cancel-ep") is True
    await clear_search("t-cancel-ep")


@pytest.mark.asyncio
async def test_search_handler_returns_499_when_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.routers import search as search_router

    async def boom(**_kwargs):
        raise SearchCancelled()

    monkeypatch.setattr(search_router, "_run_search", boom)
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)
    with pytest.raises(HTTPException) as exc:
        await search_router.search(
            request=request,
            q="wine glass",
            search_id="t-cancel-499",
            session=AsyncMock(),
        )
    assert exc.value.status_code == 499
    assert is_search_cancelled("t-cancel-499") is False
