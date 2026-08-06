from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile, DriveFileStatus, Media
from app.gemini.service import GeminiFileSearchService

logger = logging.getLogger(__name__)

_ARCHIVE_PREFIX = "archived:"


async def restore_processed_when_media_exists(session: AsyncSession) -> int:
    """Re-mark PENDING/ERROR rows as PROCESSED when a media row already exists.

    Protects Completed/done counts after a bad re-pend (e.g. first-time content
    hash treated as a content change). Append-only: never deletes media/vectors.
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
