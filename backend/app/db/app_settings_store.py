"""Persist UI/runtime settings in Postgres (singleton row)."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db.models import AppSettings
from app.runtime_settings import RuntimeSettings, set_runtime_settings


def _defaults_from_env() -> RuntimeSettings:
    s = get_settings()
    return RuntimeSettings(
        auto_index_enabled=s.auto_index_enabled,
        auto_index_interval_seconds=max(30, s.auto_index_interval_seconds),
        follow_shortcut_folders=s.follow_shortcut_folders,
        gemini_file_search_search_enabled=s.gemini_file_search_search_enabled,
        search_parallel_variants_enabled=s.search_parallel_variants_enabled,
        search_use_captions=s.search_use_captions,
        search_rerank_enabled=s.search_rerank_enabled,
    )


def _row_to_runtime(row: AppSettings) -> RuntimeSettings:
    return RuntimeSettings(
        auto_index_enabled=row.auto_index_enabled,
        auto_index_interval_seconds=max(30, row.auto_index_interval_seconds),
        follow_shortcut_folders=getattr(row, "follow_shortcut_folders", True),
        gemini_file_search_search_enabled=row.gemini_file_search_search_enabled,
        search_parallel_variants_enabled=row.search_parallel_variants_enabled,
        search_use_captions=row.search_use_captions,
        search_rerank_enabled=row.search_rerank_enabled,
    )


def _apply_runtime_to_row(row: AppSettings, runtime: RuntimeSettings) -> None:
    row.auto_index_enabled = runtime.auto_index_enabled
    row.auto_index_interval_seconds = max(30, runtime.auto_index_interval_seconds)
    row.follow_shortcut_folders = runtime.follow_shortcut_folders
    row.gemini_file_search_search_enabled = runtime.gemini_file_search_search_enabled
    row.search_parallel_variants_enabled = runtime.search_parallel_variants_enabled
    row.search_use_captions = runtime.search_use_captions
    row.search_rerank_enabled = runtime.search_rerank_enabled


async def load_runtime_settings_from_db(session_factory: async_sessionmaker[AsyncSession]) -> RuntimeSettings:
    """Load persisted settings into the in-memory runtime cache (startup)."""
    async with session_factory() as session:
        row = await session.get(AppSettings, 1)
        if row is None:
            runtime = _defaults_from_env()
            row = AppSettings(id=1)
            _apply_runtime_to_row(row, runtime)
            session.add(row)
            await session.commit()
        else:
            runtime = _row_to_runtime(row)
    set_runtime_settings(runtime)
    return runtime


async def save_runtime_settings_to_db(
    session: AsyncSession,
    runtime: RuntimeSettings,
) -> None:
    """Upsert singleton settings row."""
    row = await session.get(AppSettings, 1)
    if row is None:
        row = AppSettings(id=1)
        session.add(row)
    _apply_runtime_to_row(row, runtime)
    await session.flush()
