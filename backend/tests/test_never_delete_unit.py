"""Pure unit tests for never-delete soft-archive (no Postgres required)."""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from types import SimpleNamespace

from app.db.models import DriveFileStatus
from app.drive import cleanup as cleanup_mod
from app.drive.cleanup import restore_archived_drive_file
from app.drive.library_tree import build_library_tree


def test_cleanup_module_never_hard_deletes_vectors_or_rows():
    src = inspect.getsource(cleanup_mod)
    assert "delete_image_sync" not in src
    assert "delete_caption_sync" not in src
    assert "session.delete" not in src


def test_restore_archived_prefers_processed_when_synced():
    df = SimpleNamespace(
        status=DriveFileStatus.ARCHIVED,
        archived_at=datetime.now(timezone.utc),
        error_message="archived: removed from live Drive listing",
        last_synced_at=datetime.now(timezone.utc),
        gemini_document_name=None,
        cache_rel_path="cache/x.jpg",
    )
    assert restore_archived_drive_file(df) is True
    assert df.status == DriveFileStatus.PROCESSED
    assert df.archived_at is None
    assert df.error_message is None


def test_restore_archived_pending_without_prior_sync():
    df = SimpleNamespace(
        status=DriveFileStatus.ARCHIVED,
        archived_at=datetime.now(timezone.utc),
        error_message="archived: x",
        last_synced_at=None,
        gemini_document_name=None,
        cache_rel_path=None,
    )
    assert restore_archived_drive_file(df) is True
    assert df.status == DriveFileStatus.PENDING


def test_library_tree_includes_archived_globally():
    rows = [
        SimpleNamespace(
            id="a",
            name="a.jpg",
            path="/RootA/a.jpg",
            mime_type="image/jpeg",
            status=DriveFileStatus.PROCESSED,
            size=1,
            source="drive",
            error_message=None,
        ),
        SimpleNamespace(
            id="b",
            name="b.jpg",
            path="/RootB/b.jpg",
            mime_type="image/jpeg",
            status=DriveFileStatus.ARCHIVED,
            size=1,
            source="drive",
            error_message="archived: stale",
        ),
    ]
    root, files, summary = build_library_tree(
        rows,
        captioned_ids=set(),
        embedded_ids={"a", "b"},
        caption_texts={},
    )
    assert {f.id for f in files} == {"a", "b"}
    assert summary["total_files"] == 2
    assert summary["archived"] == 1
    assert summary["embedded"] == 2
    # a is processed + missing caption; b is archived (not in needs_work caption term)
    assert summary["missing_captions"] == 1
    assert summary["needs_work"] == summary["pending"] + summary["errors"] + summary["missing_captions"]
    assert root.archived_count == 1
