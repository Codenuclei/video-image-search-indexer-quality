"""Backup retention, pool budget, and deep-dive forever archive tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.config import Settings
from app.workers.backup import prune_daily_backups, restore_dry_run


def test_db_pool_fits_under_max_connections_200():
    # Crash-safe default is WEB_CONCURRENCY=4; budget still fine if raised briefly.
    pool = Settings.model_fields["db_pool_size"].default
    overflow = Settings.model_fields["db_max_overflow"].default
    assert pool >= 5
    assert overflow >= 5
    # Prefer 4 workers (OpenBLAS-safe). Allow a brief 8-worker spike under 200.
    assert 4 * (pool + overflow) <= 120
    assert 8 * (pool + overflow) < 200


def test_prune_daily_never_touches_forever(tmp_path: Path):
    daily = tmp_path / "daily"
    forever = tmp_path / "forever"
    daily.mkdir()
    forever.mkdir()
    old = daily / "oldday"
    old.mkdir()
    # Backdate mtime
    old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
    import os

    os.utime(old, (old_ts, old_ts))
    keep = forever / "carousel-deep-dives-keep.json"
    keep.write_text("[]", encoding="utf-8")

    removed = prune_daily_backups(daily, retention_days=14)
    assert removed == 1
    assert not old.exists()
    assert keep.exists()


@pytest.mark.asyncio
async def test_restore_dry_run_reports_artifacts(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    from app.config import get_settings

    get_settings.cache_clear()

    day = tmp_path / "daily" / "20260101"
    day.mkdir(parents=True)
    (tmp_path / "forever").mkdir(parents=True)
    (day / "postgres-meta.json").write_text("{}", encoding="utf-8")
    (day / "summary.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
    (tmp_path / "forever" / "carousel-deep-dives-x.json").write_text("{}", encoding="utf-8")

    with patch("app.workers.backup.get_settings") as gs:
        gs.return_value = MagicMock(
            backup_dir=str(tmp_path),
            backup_enabled=True,
            backup_retention_days=14,
        )
        result = await restore_dry_run("20260101")

    assert result["ok"] is True
    assert result["would_restore"] is False
    assert result["forever_carousel_archives"] >= 1
    get_settings.cache_clear()
