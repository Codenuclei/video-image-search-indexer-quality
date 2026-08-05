"""Unit tests for durable media cache path helpers."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
from app.drive.media_cache import cache_rel_path_for, media_cache_path, resolve_cache_path


def test_media_cache_path_stable_by_file_id(tmp_path: Path) -> None:
    settings = Settings(media_cache_dir=str(tmp_path / "cache"))
    drive_file = SimpleNamespace(id="fileABC", name="Photo.JPG", mime_type="image/jpeg", cache_rel_path=None)
    path = media_cache_path(settings, drive_file)
    assert path.name == "fileABC.jpg"
    assert path.parent == tmp_path / "cache"


def test_resolve_cache_path_requires_nonzero_file(tmp_path: Path) -> None:
    settings = Settings(media_cache_dir=str(tmp_path / "cache"))
    drive_file = SimpleNamespace(id="f1", name="a.png", mime_type="image/png", cache_rel_path=None)
    assert resolve_cache_path(settings, drive_file) is None
    dest = media_cache_path(settings, drive_file)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"")
    assert resolve_cache_path(settings, drive_file) is None
    dest.write_bytes(b"abc")
    found = resolve_cache_path(settings, drive_file)
    assert found == dest
    assert cache_rel_path_for(settings, found) == "f1.png"
