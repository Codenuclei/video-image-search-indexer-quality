"""Unit tests for library shell helpers (no DB / Qdrant)."""
from __future__ import annotations

from types import SimpleNamespace

from app.drive.library_shell_cache import LibraryShellCache
from app.drive.library_tree import (
    compute_library_revision,
    file_folder_path,
    folder_node_to_shell_dict,
    is_direct_child_of_folder,
    build_library_shell,
)


def _df(**kwargs):
    defaults = dict(
        id="x",
        name="a.jpg",
        path="/Root/a.jpg",
        mime_type="image/jpeg",
        status=SimpleNamespace(value="pending"),
        size=10,
        source="drive",
        error_message=None,
        last_synced_at=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_file_folder_path_and_direct_child():
    assert file_folder_path("/Root/sub/a.jpg") == "/Root/sub"
    assert is_direct_child_of_folder("/Root/sub/a.jpg", "/Root/sub")
    assert not is_direct_child_of_folder("/Root/sub/deep/a.jpg", "/Root/sub")
    assert is_direct_child_of_folder("/solo.jpg", "/")


def test_compute_library_revision_stable():
    rows = [
        _df(id="1", status=SimpleNamespace(value="pending")),
        _df(id="2", status=SimpleNamespace(value="processed")),
    ]
    assert compute_library_revision(rows) == compute_library_revision(rows)
    assert "2:" in compute_library_revision(rows)
    assert "pending:1" in compute_library_revision(rows)


def test_build_library_shell_omits_files_and_qdrant_stats():
    rows = [
        _df(id="1", name="a.jpg", path="/Root/a.jpg", status=SimpleNamespace(value="processed")),
        _df(id="2", name="b.jpg", path="/Root/b.jpg", status=SimpleNamespace(value="pending")),
    ]
    root, summary = build_library_shell(rows)
    shell = folder_node_to_shell_dict(root)
    assert shell["files"] == []
    assert summary["caption_stats_ready"] is False
    assert summary["total_files"] == 2
    # Nested folder Root should exist with counts, no file payloads
    assert len(shell["folders"]) == 1
    assert shell["folders"][0]["files"] == []
    assert shell["folders"][0]["file_count"] == 2


def test_build_library_shell_with_caption_ids():
    rows = [
        _df(id="1", name="a.jpg", path="/Root/a.jpg", status=SimpleNamespace(value="processed")),
        _df(id="2", name="b.jpg", path="/Root/b.jpg", status=SimpleNamespace(value="processed")),
    ]
    root, summary = build_library_shell(
        rows,
        captioned_ids={"1", "2"},
        embedded_ids={"1"},
        caption_stats_ready=True,
    )
    shell = folder_node_to_shell_dict(root)
    assert summary["caption_stats_ready"] is True
    assert summary["captioned"] == 2
    assert summary["embedded"] == 1
    assert shell["folders"][0]["captioned_count"] == 2
    assert shell["folders"][0]["embedded_count"] == 1


def test_library_shell_cache_hit_miss():
    cache = LibraryShellCache()
    assert cache.get("r1") is None
    cache.put("r1", {"revision": "r1"})
    assert cache.get("r1") == {"revision": "r1"}
    assert cache.get("r2") is None
    cache.put_revision("r2")
    assert cache.get_recent_revision(10) == "r2"
    # Updating only the cheap SQL revision must not relabel stale shell JSON.
    assert cache.get("r2") is None
    assert cache.get("r1") == {"revision": "r1"}
    cache.invalidate()
    assert cache.get("r1") is None
