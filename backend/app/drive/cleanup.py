from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile, DriveFileStatus, FaceJob, FaceJobStatus, Media, VideoSegment
from app.gemini.service import GeminiFileSearchService

logger = logging.getLogger(__name__)

_ARCHIVE_PREFIX = "archived:"

_FACE_JOB_IN_FLIGHT = frozenset(
    {
        FaceJobStatus.PENDING,
        FaceJobStatus.PROCESSING,
        FaceJobStatus.PENDING.value,
        FaceJobStatus.PROCESSING.value,
        FaceJobStatus.PENDING.name,
        FaceJobStatus.PROCESSING.name,
    }
)


def archived_image_qualifies_for_processed(
    *,
    has_media: bool,
    has_qdrant_embed: bool,
    has_valid_caption: bool,
    face_job_status: object | None,
) -> bool:
    """True when an archived image already has processed-quality artifacts.

    Face detections may be zero (no people in the photo). An in-flight FaceJob
    means InsightFace has not finished, so the row must stay archived.
    """
    if not (has_media and has_qdrant_embed and has_valid_caption):
        return False
    if face_job_status in _FACE_JOB_IN_FLIGHT:
        return False
    return True


def archived_video_qualifies_for_processed(
    *,
    has_media: bool,
    has_transcript_segment: bool,
) -> bool:
    """True when an archived video already has Media + transcript cues."""
    return bool(has_media and has_transcript_segment)


def _is_image_row(mime_type: str | None, name: str | None) -> bool:
    mime = (mime_type or "").lower()
    if mime.startswith("image/"):
        return True
    lower = (name or "").lower()
    return lower.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif", ".tif", ".tiff"))


def _is_video_row(mime_type: str | None) -> bool:
    return (mime_type or "").lower().startswith("video/")


def _clear_archive_values() -> dict:
    return {
        "status": DriveFileStatus.PROCESSED,
        "archived_at": None,
        "error_message": None,
    }


async def restore_processed_when_media_exists(session: AsyncSession) -> int:
    """Re-mark PENDING/ERROR rows as PROCESSED when a media row already exists.

    Protects Completed/done counts after a bad re-pend (e.g. first-time content
    hash treated as a content change). Append-only: never deletes media/vectors.
    ARCHIVED rows are handled by ``restore_archived_when_index_complete``.
    """
    ids = list(
        (
            await session.execute(
                select(DriveFile.id).where(
                    DriveFile.status.in_(
                        (DriveFileStatus.PENDING, DriveFileStatus.ERROR)
                    ),
                    DriveFile.id.in_(select(Media.drive_file_id)),
                )
            )
        )
        .scalars()
        .all()
    )
    if not ids:
        return 0
    await session.execute(
        update(DriveFile)
        .where(DriveFile.id.in_(ids))
        .values(status=DriveFileStatus.PROCESSED, error_message=None)
    )
    await session.flush()
    logger.info("Restored PROCESSED for %d file(s) that already have media rows", len(ids))
    return len(ids)


async def restore_archived_when_index_complete(session: AsyncSession) -> int:
    """Save-only: un-archive files that already qualify as PROCESSED.

    Images need Media + Qdrant image embed + valid caption, and must not have
    an in-flight FaceJob. Videos need Media + at least one transcript segment.
    Never deletes DriveFile/Media/faces/vectors/Gemini docs. Empty archived
    leftovers stay archived.
    """
    rows = list(
        (
            await session.execute(
                select(DriveFile).where(
                    DriveFile.status == DriveFileStatus.ARCHIVED,
                    DriveFile.id.in_(select(Media.drive_file_id)),
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0

    image_rows = [r for r in rows if _is_image_row(r.mime_type, r.name)]
    video_rows = [r for r in rows if _is_video_row(r.mime_type)]
    qualify_ids: list[str] = []

    if image_rows:
        image_ids = [r.id for r in image_rows]
        face_jobs = list(
            (
                await session.execute(
                    select(FaceJob.drive_file_id, FaceJob.status).where(
                        FaceJob.drive_file_id.in_(image_ids)
                    )
                )
            ).all()
        )
        job_by_id: dict[str, object] = {}
        for fid, status in face_jobs:
            prev = job_by_id.get(fid)
            if prev in _FACE_JOB_IN_FLIGHT:
                continue
            job_by_id[fid] = status

        embedded: set[str] = set()
        captioned: set[str] = set()
        try:
            from app.qdrant.image_captions import valid_caption_ids_sync
            from app.qdrant.images import existing_image_ids_sync

            embedded, captioned = await asyncio.gather(
                asyncio.to_thread(existing_image_ids_sync, image_ids),
                asyncio.to_thread(valid_caption_ids_sync, image_ids),
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Qdrant lookup failed while restoring archived images — leaving them archived"
            )
            embedded, captioned = set(), set()

        for row in image_rows:
            if archived_image_qualifies_for_processed(
                has_media=True,
                has_qdrant_embed=row.id in embedded,
                has_valid_caption=row.id in captioned,
                face_job_status=job_by_id.get(row.id),
            ):
                qualify_ids.append(row.id)

    if video_rows:
        video_ids = [r.id for r in video_rows]
        cued_ids = set(
            (
                await session.execute(
                    select(Media.drive_file_id)
                    .join(VideoSegment, VideoSegment.media_id == Media.id)
                    .where(
                        Media.drive_file_id.in_(video_ids),
                        VideoSegment.text != "",
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        for row in video_rows:
            if archived_video_qualifies_for_processed(
                has_media=True,
                has_transcript_segment=row.id in cued_ids,
            ):
                qualify_ids.append(row.id)

    qualify_ids = list(dict.fromkeys(qualify_ids))
    if not qualify_ids:
        return 0

    await session.execute(
        update(DriveFile)
        .where(DriveFile.id.in_(qualify_ids))
        .values(**_clear_archive_values())
    )
    await session.flush()
    logger.info(
        "Restored PROCESSED for %d archived file(s) that already have index artifacts",
        len(qualify_ids),
    )
    return len(qualify_ids)


async def archive_drive_file(
    session: AsyncSession,
    drive_file: DriveFile,
    *,
    reason: str = "removed from live Drive listing",
    gemini: GeminiFileSearchService | None = None,
) -> None:
    """Soft-detach a file from the live Drive tree without erasing indexed artifacts.

    Preserves Postgres media/faces, on-disk thumbnails/caches, Gemini documents,
    and all Qdrant vectors (image, caption, video frames). ``gemini`` is accepted
    for call-site compatibility and is intentionally unused.
    """
    del gemini  # never delete Gemini docs on archive
    if drive_file.status == DriveFileStatus.ARCHIVED and drive_file.archived_at is not None:
        return
    drive_file.status = DriveFileStatus.ARCHIVED
    drive_file.archived_at = datetime.now(timezone.utc)
    drive_file.error_message = f"{_ARCHIVE_PREFIX} {reason}"[:500]
    await session.flush()
    logger.info(
        "Archived drive file %s (%s) — vectors/thumbs retained",
        drive_file.id,
        drive_file.name,
    )


async def remove_drive_file(
    session: AsyncSession,
    drive_file: DriveFile,
    *,
    gemini: GeminiFileSearchService | None = None,
    reason: str = "detached (never-delete policy)",
) -> None:
    """Detach a tracked file without deleting indexed data.

    Historically this hard-deleted the Postgres row plus Qdrant image/caption
    points. That path is retired: folder change, disconnect, 404, conflict
    replace, and API delete all soft-archive instead so embeddings and
    thumbnails are permanent.
    """
    await archive_drive_file(
        session,
        drive_file,
        reason=reason,
        gemini=gemini,
    )


def restore_archived_drive_file(drive_file: DriveFile) -> bool:
    """Clear archive markers when a file reappears in a live Drive listing.

    Returns True if the row was archived and was restored.
    """
    if drive_file.status != DriveFileStatus.ARCHIVED and drive_file.archived_at is None:
        return False
    # Prefer avoiding re-index when prior successful sync markers exist.
    if drive_file.last_synced_at or drive_file.gemini_document_name or drive_file.cache_rel_path:
        drive_file.status = DriveFileStatus.PROCESSED
    else:
        drive_file.status = DriveFileStatus.PENDING
    drive_file.archived_at = None
    if (drive_file.error_message or "").startswith(_ARCHIVE_PREFIX):
        drive_file.error_message = None
    return True
