from __future__ import annotations

from fastapi import APIRouter, Depends

from app.config import get_settings
from app.runtime_settings import get_runtime_settings, update_runtime_settings
from app.schemas import SettingsOut, SettingsUpdate

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_model=SettingsOut)
async def read_settings() -> SettingsOut:
    settings = get_settings()
    runtime = get_runtime_settings()
    return SettingsOut(
        gemini_model=settings.gemini_model,
        gemini_file_search_store_display_name=settings.gemini_file_search_store_display_name,
        auto_index_enabled=runtime.auto_index_enabled,
        auto_index_interval_seconds=runtime.auto_index_interval_seconds,
    )


@router.put("", response_model=SettingsOut)
async def write_settings(payload: SettingsUpdate) -> SettingsOut:
    update_runtime_settings(
        auto_index_enabled=payload.auto_index_enabled,
        auto_index_interval_seconds=payload.auto_index_interval_seconds,
    )
    return await read_settings()
