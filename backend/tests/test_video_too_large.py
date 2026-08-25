"""Tests for video size skip (>10GB → SKIPPED, never PROCESSED)."""

from __future__ import annotations

from app.drive.video_limits import (
    VIDEO_MAX_INDEX_BYTES,
    is_video_too_large,
    video_too_large_message,
)
from app.workers.requeue_failed import normalize_skip_reason


def test_video_too_large_threshold():
    assert not is_video_too_large(None)
    assert not is_video_too_large(VIDEO_MAX_INDEX_BYTES)
    assert not is_video_too_large(VIDEO_MAX_INDEX_BYTES - 1)
    assert is_video_too_large(VIDEO_MAX_INDEX_BYTES + 1)


def test_video_too_large_skip_reason_key():
    msg = video_too_large_message(VIDEO_MAX_INDEX_BYTES + 5)
    assert msg.startswith("video_too_large:")
    assert normalize_skip_reason(msg) == "video_too_large"
