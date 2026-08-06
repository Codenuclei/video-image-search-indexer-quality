from __future__ import annotations

import asyncio
import logging

import cv2
import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import DriveFile, Face, FaceEmbedding, Media, MediaType
from app.drive.client import DriveConnectorClient
from app.faces.engine import FaceEngine, get_face_engine
from app.matching.service import assign_face
from app.pipelines.async_cpu import run_cpu_bound
from app.pipelines.common import clear_existing_media, decode_image_bgr, file_has_media, save_face_thumbnail
from app.pipelines.dedup import LocalIdentityTracker, passes_quality_filter
from app.search.images import index_image_embedding

logger = logging.getLogger(__name__)


async def detect_faces_async(engine: FaceEngine, image_bgr: np.ndarray):
    return await run_cpu_bound(engine.detect_faces, image_bgr)


async def process_image_file(
    session: AsyncSession,
    drive_file: DriveFile,
    client: DriveConnectorClient,
    settings: Settings | None = None,
    engine: FaceEngine | None = None,
) -> Media | None:
    settings = settings or get_settings()
    engine = engine or get_face_engine()

    await clear_existing_media(session, drive_file.id)

    from app.drive.content_hash import sha256_bytes
    from app.drive.media_cache import ensure_media_cached, read_cached_bytes

    # Durable cache copy (temp → atomic move) before face/embed work.
    cache_path = await ensure_media_cached(client, drive_file, settings)
    raw_bytes = await run_cpu_bound(read_cached_bytes, cache_path)
    if not drive_file.content_hash:
        drive_file.content_hash = sha256_bytes(raw_bytes)
        drive_file.content_hash_algo = "sha256"

    from app.drive.conflicts import apply_dedupe_on_upsert

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

    image_bgr = await run_cpu_bound(decode_image_bgr, raw_bytes, file_name=drive_file.name)

    media = Media(drive_file_id=drive_file.id, type=MediaType.IMAGE)
    session.add(media)
    await session.flush()

    img_h, img_w = image_bgr.shape[:2]
    detections = await detect_faces_async(engine, image_bgr)
    tracker = LocalIdentityTracker(settings.media_dedup_similarity_threshold)

    for detection in detections:
        if not passes_quality_filter(detection, img_w, img_h, settings.min_face_area_fraction):
            continue
        local = tracker.match(detection.embedding)
        if local is not None:
            local.update(detection.embedding)
            continue

        face = Face(
            media_id=media.id,
            bbox_x=detection.bbox_x,
            bbox_y=detection.bbox_y,
            bbox_width=detection.bbox_width,
            bbox_height=detection.bbox_height,
            detection_confidence=detection.confidence,
        )
        session.add(face)
        await session.flush()
        face.thumbnail_path = save_face_thumbnail(face.id, detection.thumbnail_jpeg, settings)
        session.add(FaceEmbedding(face_id=face.id, embedding=detection.embedding))
        tracker.register(detection.embedding)
        await assign_face(session, face, detection.embedding)

    # Embed full image for vector search (DeepImageSearch-style, via Gemini Embedding 2 + Qdrant)
    if settings.gemini_api_key:
        ok, buf = cv2.imencode(".jpg", image_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if ok:
            jpeg = buf.tobytes()
            await index_image_embedding(jpeg, drive_file.id)
            # Captions are generated in batched backfill (image_caption_batch_size × parallel).

    logger.info("Detected %d unique faces in %s", len(tracker._tracks), drive_file.name)
    return media
