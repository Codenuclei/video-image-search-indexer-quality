"""Fast in-memory Drive file-list poll endpoint (Option 1).

Frontend indexing UI continues to use `/drive/files` (DB + status). This route
exposes the raw Drive listing cache that push notifications keep warm.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.drive.file_list_cache import get_file_list_cache
from app.drive.push_channels import get_push_channel_state, resolve_webhook_address
from app.config import get_settings

router = APIRouter(tags=["cache"])


@router.get("/api/cache/files")
async def get_cached_drive_files() -> dict:
    """Return the in-memory Drive file list. No Drive API call."""
    cache = get_file_list_cache()
    payload = cache.snapshot()
    settings = get_settings()
    payload["push"] = {
        **get_push_channel_state().status(),
        "webhook_address": resolve_webhook_address(settings),
    }
    return payload


@router.get("/api/cache/status")
async def get_cache_status() -> dict:
    """Lightweight cache + push-channel health (no file payload)."""
    cache = get_file_list_cache()
    settings = get_settings()
    snap = cache.snapshot()
    return {
        "warm": cache.is_warm(),
        "count": snap["count"],
        "file_count": snap["file_count"],
        "cached_at": snap["cached_at"],
        "age_seconds": snap["age_seconds"],
        "source": snap["source"],
        "from_memory": True,
        "last_error": snap["last_error"],
        "refresh_in_flight": snap["refresh_in_flight"],
        "push": {
            **get_push_channel_state().status(),
            "webhook_address": resolve_webhook_address(settings),
        },
    }
