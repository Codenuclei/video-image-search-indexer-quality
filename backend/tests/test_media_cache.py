"""Unit tests for durable media cache path helpers."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
from app.drive.media_cache import (
    cache_is_incomplete,
    cache_rel_path_for,
    media_cache_path,
    resolve_cache_path,
)


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


def test_cache_is_incomplete_short_or_truncated_svg(tmp_path: Path) -> None:
    dest = tmp_path / "cache" / "poster.svg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"><svg></svg><g')
    short = SimpleNamespace(
        id="s1",
        name="poster.svg",
        mime_type="image/svg+xml",
        cache_rel_path=None,
        size=10_000,
    )
    assert cache_is_incomplete(dest, short)
    complete = SimpleNamespace(
        id="s2",
        name="poster.svg",
        mime_type="image/svg+xml",
        cache_rel_path=None,
        size=dest.stat().st_size,
    )
    assert cache_is_incomplete(dest, complete)
    dest.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>\n')
    assert not cache_is_incomplete(dest, SimpleNamespace(
        id="s3",
        name="poster.svg",
        mime_type="image/svg+xml",
        cache_rel_path=None,
        size=dest.stat().st_size,
    ))
