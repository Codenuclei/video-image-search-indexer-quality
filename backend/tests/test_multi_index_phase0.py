"""Tests for Phase 0 cache unlink, disk readiness, and error-bucket requeue."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.drive.media_cache import media_cache_path, unlink_drive_source_cache
from app.storage import disk_usage_report, indexing_disk_ready
from app.workers.requeue_failed import (
    NON_RETRYABLE_ERROR_BUCKETS,
    normalize_error_bucket,
)


def test_unlink_drive_source_cache_removes_drive_file(tmp_path: Path) -> None:
    settings = Settings(media_cache_dir=str(tmp_path / "cache"))
    drive_file = SimpleNamespace(
        id="fileABC",
        name="Photo.JPG",
        mime_type="image/jpeg",
        source="drive",
        cache_rel_path=None,
    )
    dest = media_cache_path(settings, drive_file)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"abc")
    drive_file.cache_rel_path = dest.name

    assert unlink_drive_source_cache(drive_file, settings) is True
    assert not dest.exists()
    assert drive_file.cache_rel_path is None


def test_unlink_skips_upload_and_youtube(tmp_path: Path) -> None:
    settings = Settings(media_cache_dir=str(tmp_path / "cache"))
    for source, file_id in (("upload", "upload:1"), ("youtube", "yt:abc")):
        drive_file = SimpleNamespace(
            id=file_id,
            name="clip.mp4",
            mime_type="video/mp4",
            source=source,
            cache_rel_path="clip.mp4",
        )
        assert unlink_drive_source_cache(drive_file, settings) is False


def test_disk_usage_report_and_ready(tmp_path: Path) -> None:
    report = disk_usage_report(tmp_path)
    assert report["free_bytes"] > 0
    assert report["ok"] is True
    assert indexing_disk_ready(tmp_path, high_water_bytes=1) is True
    assert indexing_disk_ready(tmp_path, high_water_bytes=10**18) is False


@pytest.mark.parametrize(
    "msg,bucket",
    [
        ("[Errno 28] No space left on device", "enospc"),
        ("retryable_disk_full: insufficient free space", "enospc"),
        ("index_stall: processing exceeded 900s without completion", "index_stall"),
        ("No Google Drive account connected. Open the DFI frontend", "drive_not_connected"),
        ("Temporary network or service timeout. Retry this file.", "timeout"),
        ("ConnectError", "connection"),
        ("", "unknown"),
    ],
)
def test_normalize_error_bucket(msg: str, bucket: str) -> None:
    assert normalize_error_bucket(msg) == bucket


def test_non_retryable_error_buckets_exclude_junk() -> None:
    assert "duplicate_content" in NON_RETRYABLE_ERROR_BUCKETS
    assert "appledouble_junk" not in NON_RETRYABLE_ERROR_BUCKETS
