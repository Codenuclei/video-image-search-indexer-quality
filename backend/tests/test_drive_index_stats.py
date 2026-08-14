"""Unit tests for drive index stats aggregation."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.db.models import DriveFileStatus
from app.drive.index_stats import _folder_of, build_drive_index_stats
from app.drive.library_tree import build_library_tree, folder_node_to_dict


def test_folder_of() -> None:
    assert _folder_of("/Root/a/b.jpg") == "/Root/a"
    assert _folder_of("/Root/b.jpg") == "/Root"
    assert _folder_of("/alone.jpg") == "/"


def test_library_tree_excludes_apple_junk_and_folder_markers() -> None:
    files = [
        SimpleNamespace(
            id="1",
            name="ok.jpg",
            path="/Drive/ok.jpg",
            mime_type="image/jpeg",
            status=DriveFileStatus.PROCESSED,
            size=10,
            source="drive",
            error_message=None,
        ),
        SimpleNamespace(
            id="2",
            name="bad.jpg",
            path="/Drive/bad.jpg",
            mime_type="image/jpeg",
            status=DriveFileStatus.ERROR,
            size=10,
            source="drive",
            error_message="[Errno 28] No space left on device",
        ),
        SimpleNamespace(
            id="3",
            name="._junk",
            path="/Drive/._junk",
            mime_type="image/jpeg",
            status=DriveFileStatus.SKIPPED,
            size=1,
            source="drive",
            error_message="appledouble_junk: macOS resource fork",
        ),
        SimpleNamespace(
            id="4",
            name="EmptyDir",
            path="/Drive/EmptyDir",
            mime_type="application/vnd.google-apps.folder",
            status=DriveFileStatus.SKIPPED,
            size=None,
            source="drive",
            error_message="folder_marker",
        ),
    ]
    root, items, summary = build_library_tree(
        files,  # type: ignore[arg-type]
        captioned_ids=set(),
        embedded_ids=set(),
        caption_texts={},
    )
    # Apple junk + folder markers must not count as files.
    assert summary["total_files"] == 2
    assert summary["processed"] == 1
    assert summary["errors"] == 1
    assert summary["skipped"] == 0
    assert len(items) == 2
    assert root.processed_count == 1
    assert root.error_count == 1
    assert root.skipped_count == 0
    d = folder_node_to_dict(root)
    assert d["processed_count"] == 1
    assert any(r["reason"] == "enospc" for r in d["top_error_reasons"])
    assert not any(r["reason"] == "appledouble_junk" for r in d["top_skip_reasons"])
    assert not any(f["name"] == "._junk" for f in d["files"])
    assert not any(f["name"] == "EmptyDir" for f in d["files"])
    # Folder marker still creates empty folder nodes for navigation (not files).
    drive_node = next(c for c in d["folders"] if c["name"] == "Drive")
    assert any(c["name"] == "EmptyDir" for c in drive_node["folders"])
    assert drive_node["file_count"] == 2


def test_library_filters_block_apple_and_folder_markers() -> None:
    from app.drive.library_filters import is_blocked_library_row

    assert is_blocked_library_row(
        SimpleNamespace(name="._x", mime_type="image/jpeg", error_message=None)
    )
    assert is_blocked_library_row(
        SimpleNamespace(
            name="fold",
            mime_type="application/vnd.google-apps.folder",
            error_message="folder_marker",
        )
    )
    assert not is_blocked_library_row(
        SimpleNamespace(name="ok.jpg", mime_type="image/jpeg", error_message=None)
    )


@pytest.mark.asyncio
async def test_build_drive_index_stats_from_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeSession:
        async def execute(self, _stmt):  # noqa: ANN001
            class _R:
                def scalars(self):
                    return self

                def all(self):
                    return [
                        SimpleNamespace(
                            id="1",
                            name="ok.jpg",
                            path="/Prospectus/ok.jpg",
                            mime_type="image/jpeg",
                            status=DriveFileStatus.PROCESSED,
                            size=1,
                            source="drive",
                            error_message=None,
                            root_folder_id="root1",
                        ),
                        SimpleNamespace(
                            id="2",
                            name="err.jpg",
                            path="/Prospectus/err.jpg",
                            mime_type="image/jpeg",
                            status=DriveFileStatus.ERROR,
                            size=1,
                            source="drive",
                            error_message="index_stall: processing exceeded 900s",
                            root_folder_id="root1",
                        ),
                        SimpleNamespace(
                            id="3",
                            name="._x",
                            path="/Prospectus/._x",
                            mime_type="image/jpeg",
                            status=DriveFileStatus.SKIPPED,
                            size=1,
                            source="drive",
                            error_message="appledouble_junk: macOS",
                            root_folder_id="root1",
                        ),
                    ]

            return _R()

    stats = await build_drive_index_stats(_FakeSession())  # type: ignore[arg-type]
    assert stats["totals"]["total"] == 2
    assert stats["totals"]["processed"] == 1
    assert stats["totals"]["error"] == 1
    assert stats["top_error_reasons"][0]["reason"] == "index_stall"
    assert stats["by_root_folder"][0]["total"] == 2
    assert not any(r["reason"] == "appledouble_junk" for r in stats["top_skip_reasons"])
