"""Compressed image thumbs: local JPEG only, never Drive."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from app.config import Settings
from app.drive.image_thumbs import THUMB_MAX_EDGE, write_image_thumbnail
from app.routers.drive import thumbnail_drive_file


def test_write_image_thumbnail_is_smaller_jpeg(tmp_path: Path) -> None:
    src = tmp_path / "orig.jpg"
    Image.new("RGB", (2000, 1500), (40, 80, 120)).save(src, "JPEG", quality=95)
    settings = Settings(thumbnail_dir=str(tmp_path / "thumbs"))
    dest = write_image_thumbnail(src, "drive-1", settings, "orig.jpg")
    assert dest.suffix == ".jpg"
    assert dest.is_file()
    assert dest.stat().st_size < src.stat().st_size
    with Image.open(dest) as thumb:
        assert max(thumb.size) <= THUMB_MAX_EDGE
        assert thumb.format == "JPEG"


@pytest.mark.asyncio
async def test_thumbnail_endpoint_never_calls_drive(tmp_path: Path) -> None:
    settings = Settings(thumbnail_dir=str(tmp_path / "thumbs"))
    src = tmp_path / "orig.jpg"
    Image.new("RGB", (640, 480), (1, 2, 3)).save(src, "JPEG", quality=90)
    write_image_thumbnail(src, "drive-1", settings, "orig.jpg")

    drive_file = SimpleNamespace(id="drive-1", name="orig.jpg", mime_type="image/jpeg")
    session = AsyncMock()
    session.get.return_value = drive_file
    client_cls = MagicMock()

    with (
        patch("app.config.get_settings", return_value=settings),
        patch("app.routers.drive.DriveDirectClient", client_cls),
    ):
        response = await thumbnail_drive_file("drive-1", session)

    assert response.media_type == "image/jpeg"
    assert "max-age" in response.headers.get("cache-control", "").lower()
    client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_thumbnail_builds_from_media_cache_not_drive(tmp_path: Path) -> None:
    settings = Settings(
        thumbnail_dir=str(tmp_path / "thumbs"),
        media_cache_dir=str(tmp_path / "cache"),
    )
    cached = tmp_path / "cache" / "drive-2.jpg"
    cached.parent.mkdir(parents=True)
    Image.new("RGB", (900, 900), (9, 9, 9)).save(cached, "JPEG", quality=90)
    drive_file = SimpleNamespace(id="drive-2", name="orig.jpg", mime_type="image/jpeg")
    session = AsyncMock()
    session.get.return_value = drive_file
    client_cls = MagicMock()

    with (
        patch("app.config.get_settings", return_value=settings),
        patch("app.routers.drive.DriveDirectClient", client_cls),
        patch("app.drive.media_cache.resolve_cache_path", return_value=cached),
    ):
        response = await thumbnail_drive_file("drive-2", session)

    assert response.status_code == 200
    client_cls.assert_not_called()
    thumb = tmp_path / "thumbs" / "images" / "drive-2.jpg"
    assert thumb.is_file()


def test_grids_use_thumbs_enlarge_uses_preview() -> None:
    repo = Path(__file__).resolve().parents[2]
    ui = (repo / "frontend/src/components/ui.tsx").read_text()
    search = (repo / "frontend/src/app/search/page.tsx").read_text()
    api = (repo / "frontend/src/lib/api.ts").read_text()
    library = (repo / "frontend/src/app/library/page.tsx").read_text()
    assert "driveFileThumbnailUrl" in ui
    assert "src={thumbUrl}" in ui
    assert "driveFilePreviewUrl(previewFile.drive_file_id" in search
    assert "/thumbnail" in api
    assert "driveFileThumbnailUrl" in library
    assert 'src={`https://drive.google.com' not in ui
    assert "drive.google.com/thumbnail" not in api
    assert "lh3.googleusercontent.com" not in api
