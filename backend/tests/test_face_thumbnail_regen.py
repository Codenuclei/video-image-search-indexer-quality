"""On-demand face thumbnail regeneration from cached media / siblings."""
from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
import pytest
from PIL import Image

from app.config import Settings
from app.drive.media_cache import media_cache_path
from app.db.models import (
    ClusterStatus,
    DriveFile,
    DriveFileStatus,
    Face,
    FaceCluster,
    Media,
    MediaType,
)
from app.reid.face_thumbs import ensure_face_thumbnail_jpeg, resolve_face_thumbnail_id, thumb_exists_on_disk
from app.routers.faces import get_face_thumbnail
from tests.conftest import requires_postgres


async def _image_media(session, *, name: str = "photo.jpg") -> tuple[Media, DriveFile]:
    drive_file = DriveFile(
        id=f"drive-{uuid.uuid4().hex}",
        name=name,
        mime_type="image/jpeg",
        path=f"/{name}",
        status=DriveFileStatus.PROCESSED,
    )
    session.add(drive_file)
    await session.flush()
    media = Media(drive_file_id=drive_file.id, type=MediaType.IMAGE)
    session.add(media)
    await session.flush()
    return media, drive_file


async def _video_media(session, *, name: str = "clip.mp4") -> tuple[Media, DriveFile]:
    drive_file = DriveFile(
        id=f"drive-{uuid.uuid4().hex}",
        name=name,
        mime_type="video/mp4",
        path=f"/{name}",
        status=DriveFileStatus.PROCESSED,
    )
    session.add(drive_file)
    await session.flush()
    media = Media(drive_file_id=drive_file.id, type=MediaType.VIDEO, duration_seconds=60.0)
    session.add(media)
    await session.flush()
    return media, drive_file


def _face_on_media(
    media: Media,
    *,
    bbox=(10.0, 10.0, 40.0, 40.0),
    cluster_id: int | None = None,
    thumbnail_path: str | None = None,
    frame_timestamp: float | None = None,
) -> Face:
    x, y, w, h = bbox
    return Face(
        media_id=media.id,
        bbox_x=x,
        bbox_y=y,
        bbox_width=w,
        bbox_height=h,
        detection_confidence=0.99,
        cluster_id=cluster_id,
        thumbnail_path=thumbnail_path,
        frame_timestamp=frame_timestamp,
    )


@requires_postgres
@pytest.mark.asyncio
async def test_regen_image_face_from_media_cache(db_session, tmp_path: Path) -> None:
    settings = Settings(
        thumbnail_dir=str(tmp_path / "thumbs"),
        media_cache_dir=str(tmp_path / "cache"),
    )
    media, drive_file = await _image_media(db_session)
    face = _face_on_media(media, thumbnail_path=str(tmp_path / "thumbs" / "999.jpg"))
    db_session.add(face)
    await db_session.flush()

    cached = media_cache_path(settings, drive_file)
    cached.parent.mkdir(parents=True)
    img = np.zeros((120, 120, 3), dtype=np.uint8)
    img[10:50, 10:50] = (0, 255, 0)
    cv2.imwrite(str(cached), img)

    with patch("app.config.get_settings", return_value=settings):
        path, saved_face, source = await ensure_face_thumbnail_jpeg(db_session, face.id)

    assert source == "cache"
    assert path.is_file()
    assert saved_face.id == face.id
    assert thumb_exists_on_disk(face, settings)


@requires_postgres
@pytest.mark.asyncio
async def test_regen_video_face_from_extracted_frame(db_session, tmp_path: Path) -> None:
    settings = Settings(thumbnail_dir=str(tmp_path / "thumbs"))
    media, drive_file = await _video_media(db_session)
    face = _face_on_media(media, frame_timestamp=1.5)
    db_session.add(face)
    await db_session.flush()

    frame_path = tmp_path / "thumbs" / "video" / drive_file.id / "1.500.jpg"
    frame_path.parent.mkdir(parents=True)
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    frame[20:60, 20:60] = (255, 0, 0)
    cv2.imwrite(str(frame_path), frame)

    async def _fake_ensure(_drive_id, ts, out_path, _settings, _session):
        assert abs(ts - 1.5) < 0.01
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out_path), frame)
        return True

    with (
        patch("app.config.get_settings", return_value=settings),
        patch("app.routers.media.ensure_frame_extracted", _fake_ensure),
    ):
        path, saved_face, source = await ensure_face_thumbnail_jpeg(db_session, face.id)

    assert source == "frame"
    assert path.is_file()
    assert saved_face.id == face.id


@requires_postgres
@pytest.mark.asyncio
async def test_fallback_to_sibling_cluster_face(db_session, tmp_path: Path) -> None:
    settings = Settings(thumbnail_dir=str(tmp_path / "thumbs"))
    media, _drive_file = await _image_media(db_session)
    cluster = FaceCluster(status=ClusterStatus.UNKNOWN, member_count=2)
    db_session.add(cluster)
    await db_session.flush()

    missing = _face_on_media(
        media,
        cluster_id=cluster.id,
        thumbnail_path=str(tmp_path / "thumbs" / "missing.jpg"),
    )
    sibling = _face_on_media(media, cluster_id=cluster.id)
    db_session.add_all([missing, sibling])
    await db_session.flush()

    sibling_path = tmp_path / "thumbs" / f"{sibling.id}.jpg"
    sibling_path.parent.mkdir(parents=True)
    Image.new("RGB", (32, 32), (1, 2, 3)).save(sibling_path, "JPEG")
    sibling.thumbnail_path = str(sibling_path)
    await db_session.flush()

    with patch("app.config.get_settings", return_value=settings):
        path, used_face, source = await ensure_face_thumbnail_jpeg(db_session, missing.id)
        thumb_id, resolve_source = await resolve_face_thumbnail_id(db_session, missing.id)

    assert source == "sibling"
    assert used_face.id == sibling.id
    assert path.is_file()
    assert thumb_id == sibling.id
    assert resolve_source == "sibling"


@requires_postgres
@pytest.mark.asyncio
async def test_thumbnail_endpoint_regens_missing_file(db_session, tmp_path: Path) -> None:
    settings = Settings(
        thumbnail_dir=str(tmp_path / "thumbs"),
        media_cache_dir=str(tmp_path / "cache"),
    )
    media, drive_file = await _image_media(db_session)
    face = _face_on_media(media, thumbnail_path=str(tmp_path / "thumbs" / "gone.jpg"))
    db_session.add(face)
    await db_session.flush()

    cached = media_cache_path(settings, drive_file)
    cached.parent.mkdir(parents=True)
    Image.new("RGB", (80, 80), (10, 20, 30)).save(cached, "JPEG")

    with patch("app.config.get_settings", return_value=settings):
        response = await get_face_thumbnail(face.id, db_session)

    assert response.media_type == "image/jpeg"
    assert Path(settings.thumbnail_dir, f"{face.id}.jpg").is_file()
