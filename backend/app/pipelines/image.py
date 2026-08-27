from __future__ import annotations

import logging

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import DriveFile, Face, FaceEmbedding, Media, MediaType
from app.drive.client import DriveConnectorClient
from app.faces.engine import FaceEngine, get_face_engine
from app.matching.service import assign_face
from app.pipelines.async_cpu import run_cpu_bound
from app.pipelines.common import clear_existing_media, decode_image_bgr, file_has_media, save_face_thumbnail
from app.pipelines.dedup import LocalIdentityTracker, passes_quality_filter

logger = logging.getLogger(__name__)


async def detect_faces_async(engine: FaceEngine, image_bgr: np.ndarray):
    return await run_cpu_bound(engine.detect_faces, image_bgr)


async def prepare_image_media(
    session: AsyncSession,
    drive_file: DriveFile,
    client: DriveConnectorClient,
    settings: Settings | None = None,
) -> Media | None:
    """Cache + hash + dedupe + Media stub. Does not run InsightFace."""
    settings = settings or get_settings()

    if await file_has_media(session, drive_file.id):
        existing = await session.scalar(
            select(Media).where(Media.drive_file_id == drive_file.id).limit(1)
        )
        return existing

    await clear_existing_media(session, drive_file.id)

    from app.drive.conflicts import apply_dedupe_on_upsert
    from app.drive.content_hash import sha256_bytes
    from app.drive.media_cache import ensure_media_cached, read_cached_bytes

    # If Drive already gave us a content hash, skip known-indexed twins before download.
    if drive_file.content_hash and drive_file.content_hash_algo:
        skip_key = await apply_dedupe_on_upsert(
            session,
            drive_file,
            algo=drive_file.content_hash_algo,
            digest=drive_file.content_hash,
        )
        if skip_key:
            logger.info(
                "index_skip reason=%s file_id=%s mime=%s size=%s name=%s (pre-download)",
                skip_key,
                drive_file.id,
                drive_file.mime_type,
                drive_file.size,
                drive_file.name,
            )
            return None

    cache_path = await ensure_media_cached(client, drive_file, settings)
    raw_bytes = await run_cpu_bound(read_cached_bytes, cache_path)
    if not drive_file.content_hash:
        drive_file.content_hash = sha256_bytes(raw_bytes)
        drive_file.content_hash_algo = "sha256"

    skip_key = await apply_dedupe_on_upsert(
        session,
        drive_file,
        algo=drive_file.content_hash_algo,
        digest=drive_file.content_hash,
    )
    if skip_key:
        logger.info(
            "index_skip reason=%s file_id=%s mime=%s size=%s name=%s",
            skip_key,
            drive_file.id,
            drive_file.mime_type,
            drive_file.size,
            drive_file.name,
        )
        return None

    # Validate decode early so face workers do not claim corrupt jobs.
    image_bgr = await run_cpu_bound(decode_image_bgr, raw_bytes, file_name=drive_file.name)

    from app.config import get_settings as _get_settings
    from app.drive.conflicts import apply_visual_dedupe_on_image
    from app.drive.perceptual_hash import dhash_hex_from_bgr

    _settings = settings or _get_settings()
    if _settings.visual_dedupe_enabled and (drive_file.mime_type or "").startswith("image/"):
        drive_file.visual_hash = await run_cpu_bound(dhash_hex_from_bgr, image_bgr)
        visual_skip = await apply_visual_dedupe_on_image(
            session,
            drive_file,
            max_hamming=_settings.visual_dedupe_max_hamming,
        )
        if visual_skip:
            logger.info(
                "index_skip reason=%s file_id=%s mime=%s size=%s name=%s (visual twin)",
                visual_skip,
                drive_file.id,
                drive_file.mime_type,
                drive_file.size,
                drive_file.name,
            )
            return None

    from app.drive.image_thumbs import write_image_thumbnail

    try:
        await run_cpu_bound(
            write_image_thumbnail,
            cache_path,
            drive_file.id,
            settings,
            drive_file.name,
        )
    except Exception:  # noqa: BLE001
        logger.warning("image thumb write failed for %s", drive_file.id, exc_info=True)

    media = Media(drive_file_id=drive_file.id, type=MediaType.IMAGE)
    session.add(media)
    await session.flush()
    return media


async def apply_faces_to_prepared_image(
    session: AsyncSession,
    drive_file: DriveFile,
    media: Media,
    *,
    client: DriveConnectorClient,
    settings: Settings | None = None,
    engine: FaceEngine | None = None,
    allow_redownload: bool = False,
) -> Media:
    """Run InsightFace on an already-prepared Media row (cache must exist)."""
    settings = settings or get_settings()
    engine = engine or get_face_engine()

    from app.drive.media_cache import ensure_media_cached, read_cached_bytes

    cache_path = await ensure_media_cached(
        client,
        drive_file,
        settings,
        allow_redownload=allow_redownload,
    )
    raw_bytes = await run_cpu_bound(read_cached_bytes, cache_path)
    image_bgr = await run_cpu_bound(decode_image_bgr, raw_bytes, file_name=drive_file.name)

    img_h, img_w = image_bgr.shape[:2]
    detections = await detect_faces_async(engine, image_bgr)

    # Download and inference above intentionally run before the first query so
    # face workers do not hold a Postgres connection during Drive/CPU work.
    existing_face = await session.scalar(
        select(Face.id).where(Face.media_id == media.id).limit(1)
    )
    if existing_face is not None:
        return media

    tracker = LocalIdentityTracker(settings.media_dedup_similarity_threshold)
    accepted = []

    for detection in detections:
        if not passes_quality_filter(detection, img_w, img_h, settings.min_face_area_fraction):
            continue
        local = tracker.match(detection.embedding)
        if local is not None:
            local.update(detection.embedding)
            continue
        tracker.register(detection.embedding)
        accepted.append(detection)

    faces = [
        Face(
            media_id=media.id,
            bbox_x=detection.bbox_x,
            bbox_y=detection.bbox_y,
            bbox_width=detection.bbox_width,
            bbox_height=detection.bbox_height,
            detection_confidence=detection.confidence,
        )
        for detection in accepted
    ]
    session.add_all(faces)
    if faces:
        await session.flush()

    for face, detection in zip(faces, accepted, strict=True):
        face.thumbnail_path = save_face_thumbnail(face.id, detection.thumbnail_jpeg, settings)
        session.add(FaceEmbedding(face_id=face.id, embedding=detection.embedding))
        await assign_face(session, face, detection.embedding)

    logger.info("Detected %d unique faces in %s", len(tracker._tracks), drive_file.name)
    return media


async def process_image_file(
    session: AsyncSession,
    drive_file: DriveFile,
    client: DriveConnectorClient,
    settings: Settings | None = None,
    engine: FaceEngine | None = None,
) -> Media | None:
    """Index one image: prepare Media, then faces (inline or enqueue face_job)."""
    settings = settings or get_settings()
    engine = engine or get_face_engine()

    media = await prepare_image_media(session, drive_file, client, settings)
    if media is None:
        return None

    if settings.face_jobs_enabled:
        from app.workers.face_queue import enqueue_face_job

        await enqueue_face_job(session, drive_file.id)
        # Faces run on dfi-face-worker; keep DriveFile PROCESSING until face+embed.
        return media

    return await apply_faces_to_prepared_image(
        session,
        drive_file,
        media,
        client=client,
        settings=settings,
        engine=engine,
    )
