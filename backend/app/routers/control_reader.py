"""Constant-time indexing controls and isolated Library reader management."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AppSettings, IndexControlState
from app.db.session import get_db
from app.drive.indexing_pause import (
    global_indexing_is_paused,
    set_folder_pause_flag,
)
from app.drive.library_reader_runtime import get_library_reader_runtime
from app.drive.library_shell_cache import (
    compute_library_revision_sql,
    get_library_shell_cache,
)

router = APIRouter(prefix="/control", tags=["control-reader"])


def _heartbeat_age_seconds(state: IndexControlState | None) -> float | None:
    if state is None or state.watcher_heartbeat_at is None:
        return None
    heartbeat = state.watcher_heartbeat_at
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - heartbeat).total_seconds())


@router.get("/status")
async def control_status(session: AsyncSession = Depends(get_db)) -> dict[str, object]:
    paused = await global_indexing_is_paused(session)
    settings = await session.get(AppSettings, 1)
    state = await session.get(IndexControlState, 1)
    age = _heartbeat_age_seconds(state)
    return {
        "paused": paused,
        "auto_index_enabled": bool(settings and settings.auto_index_enabled),
        "watcher_alive": age is not None and age <= 10,
        "watcher_heartbeat_age_seconds": round(age, 3) if age is not None else None,
        "active_image_jobs": state.active_image_jobs if state else None,
        "active_video_jobs": state.active_video_jobs if state else None,
        "cancelled_jobs": state.cancelled_jobs if state else 0,
        "reader": get_library_reader_runtime().status(),
    }


async def _set_global_pause(session: AsyncSession, *, paused: bool) -> dict[str, object]:
    changed = await set_folder_pause_flag(session, "/", paused=paused)
    settings = await session.get(AppSettings, 1)
    if settings is None:
        settings = AppSettings(id=1)
        session.add(settings)
    settings.auto_index_enabled = not paused
    await session.commit()
    get_library_shell_cache().invalidate()
    return {
        "ok": True,
        "paused": paused,
        "changed": changed,
        "auto_index_enabled": settings.auto_index_enabled,
        "drive_files_mutated": 0,
    }


@router.post("/indexing/pause")
async def pause_all_indexing(session: AsyncSession = Depends(get_db)) -> dict[str, object]:
    return await _set_global_pause(session, paused=True)


@router.post("/indexing/resume")
async def resume_all_indexing(session: AsyncSession = Depends(get_db)) -> dict[str, object]:
    return await _set_global_pause(session, paused=False)


@router.post("/reader/restart")
async def restart_library_reader(session: AsyncSession = Depends(get_db)) -> dict[str, object]:
    cache = get_library_shell_cache()
    cache.invalidate()
    reader = get_library_reader_runtime()
    runtime = await reader.restart()
    revision = await compute_library_revision_sql(session, max_age_seconds=0)
    return {"ok": True, "revision": revision, "reader": runtime}
