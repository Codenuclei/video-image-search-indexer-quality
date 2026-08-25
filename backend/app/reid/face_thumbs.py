"""On-demand face thumbnail regeneration from indexed media / Drive."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import cv2
import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import DriveFile, Face, Media, MediaType
from app.pipelines.common import decode_image_bgr, save_face_thumbnail

logger = logging.getLogger(__name__)

ThumbSource = str  # disk | cache | frame | stream | sibling | regen


def face_thumbnail_path(face_id: int, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return Path(settings.thumbnail_dir) / f"{face_id}.jpg"


def thumb_exists_on_disk(face: Face, settings: Settings | None = None) -> bool:
    if face.thumbnail_path and os.path.isfile(face.thumbnail_path):
        return True
    path = face_thumbnail_path(face.id, settings)
    return path.is_file()


def crop_face_jpeg(image_bgr: np.ndarray, face: Face) -> bytes | None:
    h, w = image_bgr.shape[:2]
    ix1 = max(0, int(face.bbox_x))
    iy1 = max(0, int(face.bbox_y))
    ix2 = min(w, int(face.bbox_x + face.bbox_width))
    iy2 = min(h, int(face.bbox_y + face.bbox_height))
    crop = image_bgr[iy1:iy2, ix1:ix2]
    if crop.size == 0:
        return None
    ok, buf = cv2.imencode(".jpg", crop)
    return buf.tobytes() if ok else None


def _load_frame_jpeg(path: Path) -> np.ndarray | None:
    if not path.is_file():
        return None
    arr = cv2.imread(str(path), cv2.IMREAD_COLOR)
    return arr


async def _load_image_bgr_from_cache(
    settings: Settings,
    drive_file: DriveFile,
) -> np.ndarray | None:
    from app.drive.media_cache import resolve_cache_path

    cached = resolve_cache_path(settings, drive_file)
    if cached is None or not cached.is_file():
        return None
    raw = await asyncio.to_thread(cached.read_bytes)
    try:
        return await asyncio.to_thread(decode_image_bgr, raw, file_name=drive_file.name or "")
    except Exception as exc:  # noqa: BLE001
        logger.debug("face thumb cache decode failed drive=%s: %s", drive_file.id, exc)
        return None


async def _load_image_bgr_from_drive(
    drive_file_id: str,
    *,
    timeout_sec: float,
) -> bytes | None:
    from app.config import get_settings
    from app.db.session import get_session_factory
    from app.drive.google_client import DriveDirectClient
    from app.pipelines.common import download_to_memory

    try:
        settings = get_settings()
        client = DriveDirectClient(session_factory=get_session_factory(), settings=settings)
        return await asyncio.wait_for(download_to_memory(client, drive_file_id), timeout=timeout_sec)
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "face thumb Drive stream failed face drive_file_id=%s: %s",
            drive_file_id,
            exc,
        )
        return None


async def _load_video_frame_path(
    session: AsyncSession,
    settings: Settings,
    drive_file: DriveFile,
    ts: float | None,
) -> Path | None:
    if ts is None:
        return None
    from app.routers.media import ensure_frame_extracted

    frames_dir = Path(settings.thumbnail_dir) / "video" / drive_file.id
    out_path = frames_dir / f"{float(ts):.3f}.jpg"
    ok = await ensure_frame_extracted(drive_file.id, float(ts), out_path, settings, session)
    return out_path if ok and out_path.is_file() else None


async def _regen_face_thumbnail(
    session: AsyncSession,
    face: Face,
    *,
    timeout_sec: float = 30.0,
) -> tuple[Path, ThumbSource] | None:
    settings = get_settings()
    media = face.media if face.media is not None else await session.get(Media, face.media_id)
    if media is None:
        return None
    drive_file = media.drive_file
    if drive_file is None and media.drive_file_id:
        drive_file = await session.get(DriveFile, media.drive_file_id)
    if drive_file is None:
        return None

    image_bgr: np.ndarray | None = None
    source: ThumbSource = "regen"

    if media.type == MediaType.VIDEO:
        frame_path = await _load_video_frame_path(session, settings, drive_file, face.frame_timestamp)
        if frame_path is not None:
            image_bgr = await asyncio.to_thread(_load_frame_jpeg, frame_path)
            source = "frame"
    else:
        image_bgr = await _load_image_bgr_from_cache(settings, drive_file)
        if image_bgr is not None:
            source = "cache"
        else:
            raw = await _load_image_bgr_from_drive(drive_file.id, timeout_sec=timeout_sec)
            if raw:
                try:
                    image_bgr = await asyncio.to_thread(
                        decode_image_bgr, raw, file_name=drive_file.name or ""
                    )
                    source = "stream"
                except Exception as exc:  # noqa: BLE001
                    logger.debug("face thumb stream decode failed face=%s: %s", face.id, exc)

    if image_bgr is None:
        return None

    jpeg = crop_face_jpeg(image_bgr, face)
    if not jpeg:
        return None

    path_str = save_face_thumbnail(face.id, jpeg, settings)
    if not path_str:
        return None
    face.thumbnail_path = path_str
    await session.flush()
    logger.info(
        "face thumb regen face_id=%s cluster_id=%s person_id=%s drive_file_id=%s source=%s",
        face.id,
        face.cluster_id,
        face.person_id,
        drive_file.id,
        source,
    )
    return Path(path_str), source


async def _find_sibling_with_thumb(
    session: AsyncSession,
    face: Face,
    *,
    try_regen: bool,
    timeout_sec: float,
) -> tuple[Face, ThumbSource] | None:
    if face.cluster_id is not None:
        stmt = (
            select(Face)
            .where(Face.cluster_id == face.cluster_id, Face.id != face.id)
            .order_by(Face.detection_confidence.desc())
            .limit(20)
        )
    elif face.person_id is not None:
        stmt = (
            select(Face)
            .where(Face.person_id == face.person_id, Face.id != face.id)
            .order_by(Face.detection_confidence.desc())
            .limit(20)
        )
    else:
        return None

    siblings = (await session.execute(stmt)).scalars().all()
    settings = get_settings()
    for sib in siblings:
        if thumb_exists_on_disk(sib, settings):
            return sib, "sibling"
        if try_regen:
            regen = await _regen_face_thumbnail(session, sib, timeout_sec=timeout_sec)
            if regen is not None:
                return sib, "sibling"
    return None


async def ensure_face_thumbnail_jpeg(
    session: AsyncSession,
    face_id: int,
    *,
    timeout_sec: float = 30.0,
    allow_fallback: bool = True,
) -> tuple[Path, Face, ThumbSource]:
    """Ensure a JPEG exists for *face_id*; regen from media or fall back to cluster/person sibling."""
    face = await session.get(Face, face_id)
    if face is None:
        raise ValueError(f"Face {face_id} not found")

    settings = get_settings()
    if thumb_exists_on_disk(face, settings):
        path = Path(face.thumbnail_path) if face.thumbnail_path else face_thumbnail_path(face.id, settings)
        return path, face, "disk"

    regen = await _regen_face_thumbnail(session, face, timeout_sec=timeout_sec)
    if regen is not None:
        path, source = regen
        return path, face, source

    if allow_fallback:
        fallback = await _find_sibling_with_thumb(
            session, face, try_regen=True, timeout_sec=min(timeout_sec, 15.0)
        )
        if fallback is not None:
            sib, source = fallback
            path = (
                Path(sib.thumbnail_path)
                if sib.thumbnail_path
                else face_thumbnail_path(sib.id, settings)
            )
            logger.warning(
                "face thumb fallback face_id=%s -> sibling=%s cluster_id=%s person_id=%s source=%s",
                face.id,
                sib.id,
                face.cluster_id,
                face.person_id,
                source,
            )
            return path, sib, source

    media = face.media if face.media is not None else await session.get(Media, face.media_id)
    drive_file_id = media.drive_file_id if media else None
    logger.warning(
        "face thumb ensure failed face_id=%s cluster_id=%s person_id=%s media_id=%s drive_file_id=%s",
        face.id,
        face.cluster_id,
        face.person_id,
        face.media_id,
        drive_file_id,
    )
    raise ValueError(f"Could not ensure thumbnail for face {face_id}")


async def resolve_face_thumbnail_id(
    session: AsyncSession,
    face_id: int,
) -> tuple[int, ThumbSource | None]:
    """Lightweight display id for search/UI — prefers disk thumb, then sibling, else original id."""
    face = await session.get(Face, face_id)
    if face is None:
        return face_id, None
    settings = get_settings()
    if thumb_exists_on_disk(face, settings):
        return face.id, "disk"
    fallback = await _find_sibling_with_thumb(session, face, try_regen=False, timeout_sec=0)
    if fallback is not None:
        sib, source = fallback
        return sib.id, source
    return face.id, None


async def count_persons_with_valid_rep_thumb(session: AsyncSession) -> int:
    """Persons whose representative (or best) face has a JPEG on disk."""
    from app.db.models import Person

    persons = (await session.execute(select(Person))).scalars().all()
    count = 0
    for person in persons:
        fid: int | None = person.representative_face_id
        if fid is not None:
            face = await session.get(Face, fid)
            if face and thumb_exists_on_disk(face):
                count += 1
                continue
        face = (
            await session.execute(
                select(Face)
                .where(Face.person_id == person.id, Face.thumbnail_path.isnot(None))
                .order_by(Face.detection_confidence.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if face and thumb_exists_on_disk(face):
            count += 1
    return count
