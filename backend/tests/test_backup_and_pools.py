"""Backup retention, pool budget, and deep-dive forever archive tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from app.config import Settings
from app.workers.backup import (
    prune_daily_backups,
    prune_forever_backups,
    restore_dry_run,
)
from scripts.compact_volume import compact_thumbnails


def test_db_pool_fits_under_max_connections_200():
    # Crash-safe default is WEB_CONCURRENCY=4; budget still fine if raised briefly.
    pool = Settings.model_fields["db_pool_size"].default
    overflow = Settings.model_fields["db_max_overflow"].default
    assert pool >= 5
    assert overflow >= 5
    # Prefer 4 workers (OpenBLAS-safe). Allow a brief 8-worker spike under 200.
    assert 4 * (pool + overflow) <= 120
    assert 8 * (pool + overflow) < 200


def test_backup_retention_prunes_daily_and_forever(tmp_path: Path):
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
    old_archive = forever / "carousel-deep-dives-old.json.gz"
    old_archive.write_text("[]", encoding="utf-8")
    os.utime(old_archive, (old_ts, old_ts))

    removed = prune_daily_backups(daily, retention_days=3)
    removed_forever = prune_forever_backups(forever, retention_days=3)
    assert removed == 1
    assert removed_forever == 1
    assert not old.exists()
    assert not old_archive.exists()


def test_compact_thumbnails_reduces_large_jpeg(tmp_path: Path):
    source = tmp_path / "video" / "file" / "1.000.jpg"
    source.parent.mkdir(parents=True)
    Image.effect_noise((2000, 1500), 80).convert("RGB").save(
        source,
        "JPEG",
        quality=95,
    )
    before = source.stat().st_size

    scanned, compacted, reclaimed = compact_thumbnails(tmp_path, apply=True)

    assert scanned == 1
    assert compacted == 1
    assert reclaimed > 0
    assert source.stat().st_size < before
    with Image.open(source) as compact:
        assert max(compact.size) <= 1280


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
