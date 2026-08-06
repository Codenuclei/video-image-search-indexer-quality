"""Multi-worker settings: DB must refresh into each process's in-memory cache."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.app_settings_store import refresh_runtime_settings_from_db
from app.runtime_settings import (
    RuntimeSettings,
    get_runtime_settings,
    set_runtime_settings,
)


def _runtime(**overrides: object) -> RuntimeSettings:
    base = RuntimeSettings(
        auto_index_enabled=False,
        auto_index_interval_seconds=30,
        reindex_errored_files=False,
        reindex_skipped_files=False,
        follow_shortcut_folders=True,
        experimental_manual_face_tag=False,
        gemini_file_search_search_enabled=False,
        search_parallel_variants_enabled=False,
        search_use_captions=True,
        search_rerank_enabled=False,
        go_indexer_enabled=False,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


@pytest.mark.asyncio
async def test_refresh_runtime_settings_from_db_updates_memory_cache():
    set_runtime_settings(_runtime(auto_index_enabled=False))
    assert get_runtime_settings().auto_index_enabled is False

    row = MagicMock()
    row.auto_index_enabled = True
    row.auto_index_interval_seconds = 45
    row.reindex_errored_files = False
    row.reindex_skipped_files = False
    row.follow_shortcut_folders = True
    row.experimental_manual_face_tag = False
    row.gemini_file_search_search_enabled = False
    row.search_parallel_variants_enabled = False
    row.search_use_captions = True
    row.search_rerank_enabled = False
    row.go_indexer_enabled = False

    session = AsyncMock()
    session.get = AsyncMock(return_value=row)

    runtime = await refresh_runtime_settings_from_db(session)
    assert runtime.auto_index_enabled is True
    assert runtime.auto_index_interval_seconds == 45
    assert get_runtime_settings().auto_index_enabled is True


@pytest.mark.asyncio
async def test_refresh_keeps_memory_when_row_missing():
    set_runtime_settings(_runtime(auto_index_enabled=True, auto_index_interval_seconds=60))
    session = AsyncMock()
    session.get = AsyncMock(return_value=None)

    runtime = await refresh_runtime_settings_from_db(session)
    assert runtime.auto_index_enabled is True
    assert runtime.auto_index_interval_seconds == 60


def test_auto_indexer_and_status_reload_settings_from_db():
    root = Path(__file__).resolve().parents[1] / "app"
    auto_src = (root / "workers" / "auto_indexer.py").read_text(encoding="utf-8")
    settings_src = (root / "routers" / "settings.py").read_text(encoding="utf-8")
    index_src = (root / "routers" / "index.py").read_text(encoding="utf-8")
    assert "refresh_runtime_settings_from_db" in auto_src
    assert "refresh_runtime_settings_from_db" in settings_src
    assert "refresh_runtime_settings_from_db" in index_src
