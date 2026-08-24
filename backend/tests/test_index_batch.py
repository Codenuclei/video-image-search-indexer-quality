"""Unit tests for batched indexer DB writes."""

from __future__ import annotations

from app.db.models import DriveFileStatus
from app.workers.index_batch import StatusWrite


def test_status_write_grouping_keys_are_hashable():
    from datetime import datetime, timezone

    w = StatusWrite(
        file_id="abc",
        status=DriveFileStatus.PROCESSED,
        error_message=None,
        clear_gemini_document=True,
        bump_synced_at=True,
        finished_at=datetime.now(timezone.utc),
    )
    key = (
        w.status,
        w.error_message,
        w.gemini_document_name,
        w.clear_gemini_document,
    )
    assert hash(key) is not None
    assert w.finished_at is not None


def test_default_batch_size_is_100():
    from app.config import Settings

    assert Settings.model_fields["index_status_batch_size"].default == 100
    assert Settings.model_fields["index_db_max_concurrent"].default == 24
