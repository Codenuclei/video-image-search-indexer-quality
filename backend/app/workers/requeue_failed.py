"""Re-queue errored or skipped Drive files when auto-index retry toggles are enabled."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import DriveFile, DriveFileStatus
from app.config import get_settings
from app.drive.indexing_pause import (
    is_file_indexing_paused,
    is_indexing_paused_message,
    load_paused_folder_paths,
)
from app.pipelines.common import infer_image_mime, is_indexable_mime

logger = logging.getLogger(__name__)

REQUEUE_BATCH_LIMIT = 30


def _is_permanent_skip(drive_file: DriveFile) -> bool:
    msg = drive_file.error_message or ""
    if is_indexing_paused_message(msg):
        return True
    if msg.startswith("Unsupported mime type for indexing:"):
        return True
    return False


def _prepare_for_retry(drive_file: DriveFile) -> None:
    inferred = infer_image_mime(drive_file.mime_type or "", drive_file.name)
    if inferred and inferred != drive_file.mime_type:
        drive_file.mime_type = inferred
    drive_file.status = DriveFileStatus.PENDING
    drive_file.error_message = None
    drive_file.decode_attempts = 0
    drive_file.last_synced_at = datetime.now(timezone.utc)


async def requeue_failed_files(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    reindex_errored: bool,
    reindex_skipped: bool,
    batch_limit: int = REQUEUE_BATCH_LIMIT,
) -> dict[str, int]:
    """Move eligible ERROR/SKIPPED files back to PENDING for another index attempt."""
    if not reindex_errored and not reindex_skipped:
        return {"errored_requeued": 0, "skipped_requeued": 0}

    errored_requeued = 0
    skipped_requeued = 0

    async with session_factory() as session:
        paused_paths = await load_paused_folder_paths(session)

        if reindex_errored and errored_requeued < batch_limit:
            errored = list(
                (
                    await session.execute(
                        select(DriveFile)
                        .where(DriveFile.status == DriveFileStatus.ERROR)
                        .order_by(*pending_order_by(get_settings()))
                        .limit(batch_limit * 4)
                    )
                ).scalars().all()
            )
            for drive_file in errored:
                if errored_requeued >= batch_limit:
                    break
                if is_file_indexing_paused(drive_file.path, paused_paths):
                    continue
                if not is_indexable_mime(drive_file.mime_type, drive_file.name):
                    continue
                _prepare_for_retry(drive_file)
                errored_requeued += 1

        if reindex_skipped and skipped_requeued < batch_limit:
            skipped = list(
                (
                    await session.execute(
                        select(DriveFile)
                        .where(DriveFile.status == DriveFileStatus.SKIPPED)
                        .order_by(*pending_order_by(get_settings()))
                        .limit(batch_limit * 4)
                    )
                ).scalars().all()
            )
            for drive_file in skipped:
                if skipped_requeued >= batch_limit:
                    break
                if is_file_indexing_paused(drive_file.path, paused_paths):
                    continue
                if _is_permanent_skip(drive_file):
                    continue
                if not is_indexable_mime(drive_file.mime_type, drive_file.name):
                    continue
                _prepare_for_retry(drive_file)
                skipped_requeued += 1

        if errored_requeued or skipped_requeued:
            await session.commit()
            logger.info(
                "Requeued failed files: %d errored, %d skipped",
                errored_requeued,
                skipped_requeued,
            )

    return {"errored_requeued": errored_requeued, "skipped_requeued": skipped_requeued}
