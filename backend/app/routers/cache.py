"""Fast Drive file-list poll endpoint (Option 1).

Frontend indexing UI continues to use `/drive/files` (DB + status). This route
exposes the Drive listing for fast reads.

Multi-worker (Gunicorn): in-memory cache is per-process. Prefer Postgres
(already synced by the webhook-receiving worker / leader) so any worker can
serve a consistent list. Fall back to local memory when DB has no rows yet.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import get_db
from app.drive.db_file_list import snapshot_drive_files_from_db
from app.drive.file_list_cache import get_file_list_cache
from app.drive.push_channels import get_push_channel_state, resolve_webhook_address

router = APIRouter(tags=["cache"])


def _push_payload(settings) -> dict:
    return {
        **get_push_channel_state().status(),
        "webhook_address": resolve_webhook_address(settings),
        "note": (
            "push.channel_id is process-local; token verification uses "
            "DRIVE_WEBHOOK_CHANNEL_TOKEN / WEBHOOK_SECRET shared across workers"
        ),
    }


@router.get("/api/cache/files")
async def get_cached_drive_files(
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Return Drive file list. Prefers DB (multi-worker safe); memory fallback."""
    settings = get_settings()
    db_payload = await snapshot_drive_files_from_db(session)
    if db_payload["count"] > 0 or db_payload.get("folder"):
        db_payload["push"] = _push_payload(settings)
        return db_payload

    cache = get_file_list_cache()
    payload = cache.snapshot()
    payload["from_db"] = False
    payload["push"] = _push_payload(settings)
    return payload


@router.get("/api/cache/status")
async def get_cache_status(
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Lightweight cache + push-channel health (no file payload)."""
    settings = get_settings()
    cache = get_file_list_cache()
    mem = cache.snapshot()
    db_payload = await snapshot_drive_files_from_db(session)
    warm = bool(db_payload["count"] > 0 or db_payload.get("folder") or cache.is_warm())
    return {
        "warm": warm,
        "count": db_payload["count"] if db_payload["count"] else mem["count"],
        "file_count": (
            db_payload["file_count"] if db_payload["count"] else mem["file_count"]
        ),
        "cached_at": db_payload["cached_at"] or mem["cached_at"],
        "age_seconds": mem["age_seconds"],
        "source": "db" if db_payload["count"] else mem["source"],
        "from_memory": False if db_payload["count"] else mem.get("from_memory", False),
        "from_db": bool(db_payload["count"] or db_payload.get("folder")),
        "memory_warm": cache.is_warm(),
        "memory_count": mem["count"],
        "last_error": mem["last_error"],
        "refresh_in_flight": mem["refresh_in_flight"],
        "push": _push_payload(settings),
    }
