from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.app_settings_store import save_runtime_settings_to_db
from app.db.session import get_db
from app.runtime_settings import get_runtime_settings, update_runtime_settings
from app.schemas import SettingsOut, SettingsUpdate

router = APIRouter(prefix="/settings", tags=["settings"])


def _settings_out() -> SettingsOut:
    settings = get_settings()
    runtime = get_runtime_settings()
    return SettingsOut(
        gemini_model=settings.gemini_model,
        gemini_file_search_store_display_name=settings.gemini_file_search_store_display_name,
        auto_index_enabled=runtime.auto_index_enabled,
        auto_index_interval_seconds=runtime.auto_index_interval_seconds,
        reindex_errored_files=runtime.reindex_errored_files,
        reindex_skipped_files=runtime.reindex_skipped_files,
        follow_shortcut_folders=runtime.follow_shortcut_folders,
        gemini_file_search_search_enabled=runtime.gemini_file_search_search_enabled,
        search_parallel_variants_enabled=runtime.search_parallel_variants_enabled,
        search_use_captions=runtime.search_use_captions,
        search_rerank_enabled=runtime.search_rerank_enabled,
    )


@router.get("", response_model=SettingsOut)
async def read_settings() -> SettingsOut:
    return _settings_out()


@router.put("", response_model=SettingsOut)
async def write_settings(
    payload: SettingsUpdate,
    session: AsyncSession = Depends(get_db),
) -> SettingsOut:
    runtime = update_runtime_settings(
        auto_index_enabled=payload.auto_index_enabled,
        auto_index_interval_seconds=payload.auto_index_interval_seconds,
        reindex_errored_files=payload.reindex_errored_files,
        reindex_skipped_files=payload.reindex_skipped_files,
        follow_shortcut_folders=payload.follow_shortcut_folders,
        gemini_file_search_search_enabled=payload.gemini_file_search_search_enabled,
        search_parallel_variants_enabled=payload.search_parallel_variants_enabled,
        search_use_captions=payload.search_use_captions,
        search_rerank_enabled=payload.search_rerank_enabled,
    )
    await save_runtime_settings_to_db(session, runtime)
    await session.commit()
    return _settings_out()
