from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.app_settings_store import (
    refresh_runtime_settings_from_db,
    save_runtime_settings_to_db,
)
from app.db.models import AppSettings
from app.db.session import get_db
from app.llm.carousel_llm import carousel_llm_settings_public_live
from app.runtime_settings import get_runtime_settings, update_runtime_settings
from app.schemas import CarouselLlmModelOption, SettingsOut, SettingsUpdate

router = APIRouter(prefix="/settings", tags=["settings"])


async def _settings_out() -> SettingsOut:
    settings = get_settings()
    runtime = get_runtime_settings()
    llm_pub = await carousel_llm_settings_public_live()
    return SettingsOut(
        gemini_model=settings.gemini_model,
        gemini_file_search_store_display_name=settings.gemini_file_search_store_display_name,
        auto_index_enabled=runtime.auto_index_enabled,
        auto_index_interval_seconds=runtime.auto_index_interval_seconds,
        reindex_errored_files=runtime.reindex_errored_files,
        reindex_skipped_files=runtime.reindex_skipped_files,
        follow_shortcut_folders=runtime.follow_shortcut_folders,
        experimental_manual_face_tag=runtime.experimental_manual_face_tag,
        gemini_file_search_search_enabled=runtime.gemini_file_search_search_enabled,
        search_parallel_variants_enabled=runtime.search_parallel_variants_enabled,
        search_use_captions=runtime.search_use_captions,
        search_rerank_enabled=runtime.search_rerank_enabled,
        search_semantic_min_score=runtime.search_semantic_min_score,
        go_indexer_enabled=runtime.go_indexer_enabled,
        carousel_llm_provider=llm_pub["carousel_llm_provider"],
        openrouter_model=llm_pub["openrouter_model"],
        claude_model=llm_pub["claude_model"],
        openrouter_configured=llm_pub["openrouter_configured"],
        claude_configured=llm_pub["claude_configured"],
        carousel_llm_model_options=[
            CarouselLlmModelOption(**opt) for opt in llm_pub["carousel_llm_model_options"]
        ],
    )


@router.get("", response_model=SettingsOut)
async def read_settings(session: AsyncSession = Depends(get_db)) -> SettingsOut:
    await refresh_runtime_settings_from_db(session)
    return await _settings_out()


@router.get("/revision")
async def settings_revision(session: AsyncSession = Depends(get_db)) -> dict[str, str]:
    """Cheap settings freshness token; avoids rebuilding provider/model payloads."""
    row = (
        await session.execute(
            select(AppSettings.id, AppSettings.updated_at).where(AppSettings.id == 1)
        )
    ).one_or_none()
    settings = get_settings()
    updated = row.updated_at.isoformat() if row and row.updated_at else ""
    # Environment-backed display values change only on deploy, but must invalidate
    # browser caches even when the singleton DB row did not change.
    env_part = "|".join(
        (
            settings.gemini_model,
            settings.gemini_file_search_store_display_name,
            settings.openrouter_model,
            settings.claude_model,
        )
    )
    return {"revision": f"{updated}:{env_part}"}


@router.get("/carousel-llm-models")
async def read_carousel_llm_models(
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Live provider model picker + current selection (no API key)."""
    await refresh_runtime_settings_from_db(session)
    return await carousel_llm_settings_public_live()


@router.put("", response_model=SettingsOut)
async def write_settings(
    payload: SettingsUpdate,
    session: AsyncSession = Depends(get_db),
) -> SettingsOut:
    # Refresh first so partial updates merge onto DB truth, not a stale worker cache.
    await refresh_runtime_settings_from_db(session)
    runtime = update_runtime_settings(
        auto_index_enabled=payload.auto_index_enabled,
        auto_index_interval_seconds=payload.auto_index_interval_seconds,
        reindex_errored_files=payload.reindex_errored_files,
        reindex_skipped_files=payload.reindex_skipped_files,
        follow_shortcut_folders=payload.follow_shortcut_folders,
        experimental_manual_face_tag=payload.experimental_manual_face_tag,
        gemini_file_search_search_enabled=payload.gemini_file_search_search_enabled,
        search_parallel_variants_enabled=payload.search_parallel_variants_enabled,
        search_use_captions=payload.search_use_captions,
        search_rerank_enabled=payload.search_rerank_enabled,
        search_semantic_min_score=payload.search_semantic_min_score,
        go_indexer_enabled=payload.go_indexer_enabled,
        carousel_llm_provider=payload.carousel_llm_provider,
        openrouter_model=payload.openrouter_model,
        claude_model=payload.claude_model,
    )
    await save_runtime_settings_to_db(session, runtime)
    await session.commit()
    return await _settings_out()
