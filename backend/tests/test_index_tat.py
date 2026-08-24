"""Unit tests for index TAT helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.workers.index_tat import empty_kind_bucket, index_kind_for_mime, tat_ms_from_started


def test_index_kind_for_mime() -> None:
    assert index_kind_for_mime("image/jpeg", "a.jpg") == "image"
    assert index_kind_for_mime("video/mp4", "a.mp4") == "video"
    assert index_kind_for_mime("application/pdf", "a.pdf") is None
    assert index_kind_for_mime(None) is None


def test_tat_ms_from_started() -> None:
    start = datetime(2026, 8, 21, 10, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(seconds=12, milliseconds=500)
    assert tat_ms_from_started(start, end) == 12500
    assert tat_ms_from_started(None, end) is None
    assert empty_kind_bucket()["count"] == 0
