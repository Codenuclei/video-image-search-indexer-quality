"""Re-queue errored or skipped Drive files when auto-index retry toggles are enabled."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db.models import DriveFile, DriveFileStatus
from app.drive.indexing_pause import (
    is_file_indexing_paused,
    is_indexing_paused_message,
    load_paused_folder_paths,
    resume_folder_indexing,
)
from app.pipelines.common import infer_image_mime, is_indexable_mime
from app.workers.claim_order import pending_order_by

logger = logging.getLogger(__name__)

REQUEUE_BATCH_LIMIT = 30
# Manual "Retry all" from Indexer status — process a large but bounded set.
RETRY_BY_REASON_LIMIT = 20_000

# Reasons that cannot be fixed by requeue (wrong type / structural).
NON_RETRYABLE_REASONS = frozenset({
    "unsupported_mime",
    "folder_marker",
    "duplicate_content",
    "name_conflict",
})

# ERROR buckets that must not be blindly retried (need ops / conflict UI).
NON_RETRYABLE_ERROR_BUCKETS = frozenset({
    "unsupported_mime",
    "folder_marker",
    "duplicate_content",
    "name_conflict",
})


def normalize_skip_reason(error_message: str | None) -> str:
    """Map a DriveFile.error_message to the skip-stats reason key."""
    raw = (error_message or "unknown").strip()
    if raw.startswith("indexing_paused"):
        return "indexing_paused"
    if raw.startswith("Unsupported mime type"):
        return "unsupported_mime"
    if raw.startswith("corrupt_file"):
        return "corrupt_file"
    if raw.startswith("decode_exhausted"):
        return "decode_exhausted"
    if raw.startswith("duplicate_content"):
        return "duplicate_content"
    if raw.startswith("name_conflict"):
        return "name_conflict"
    if raw == "folder_marker":
        return "folder_marker"
    if not raw:
        return "unknown"
    return raw.split(":")[0][:64]


def normalize_error_bucket(error_message: str | None) -> str:
    """Map ERROR error_message to a retry bucket key."""
    raw = (error_message or "").strip()
    lower = raw.lower()
    if not raw:
        return "unknown"
    if "can't start new thread" in lower or "cannot start new thread" in lower:
        return "cant_start_thread"
    if "not associated with a value" in lower and "media" in lower:
        return "unbound_media"
    if "errno 28" in lower or "no space left" in lower or "retryable_disk_full" in lower:
        return "enospc"
    if "index_stall" in lower:
        return "index_stall"
    if "no google drive account" in lower or "drive account not connected" in lower:
        return "drive_not_connected"
    if "deadlock" in lower:
        return "deadlock"
    if "timeout" in lower or "timed out" in lower:
        return "timeout"
    if "429" in lower or "resource exhausted" in lower or "quota" in lower:
        return "quota"
    if "404" in lower or "not found" in lower:
        return "not_found"
    if "ffmpeg" in lower:
        return "ffmpeg"
    if "connecterror" in lower or "connection" in lower:
        return "connection"
    if lower.startswith("readerror") or "readerror" in lower:
        return "read_error"
    # Fall back to skip-style prefix when present.
    return normalize_skip_reason(raw)


def _is_permanent_skip(drive_file: DriveFile) -> bool:
    msg = drive_file.error_message or ""
    if is_indexing_paused_message(msg):
        return True
    if msg.startswith("Unsupported mime type for indexing:"):
        return True
    if msg.startswith("duplicate_content"):
        return True
    if msg.startswith("name_conflict"):
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


async def requeue_skipped_by_reason(
    session: AsyncSession,
    reason: str,
    *,
    limit: int = RETRY_BY_REASON_LIMIT,
) -> dict[str, object]:
    """Retry all SKIPPED media matching a skip-stats reason key.

    - ``indexing_paused``: resume every paused folder (same path as Library resume),
      then clear any orphaned paused skips whose folders are no longer paused.
    - ``unsupported_mime`` / ``folder_marker``: no-op (cannot be indexed).
    - decode/corrupt/other: clear skip and set PENDING (respects active folder pauses).
    """
    key = (reason or "").strip() or "unknown"
    if key in NON_RETRYABLE_REASONS:
        matched = await _count_skipped_for_reason(session, key)
        if key == "unsupported_mime":
            message = (
                "Unsupported file types cannot be indexed. "
                "Convert to an image/video format or ignore these skips."
            )
        elif key == "duplicate_content":
            message = (
                "Identical content is already indexed — these rows are marked complete, "
                "not skipped. Refresh if you still see a stale skip count."
            )
        elif key == "name_conflict":
            message = (
                "Same filename as another file with different content. "
                "Use Replace or Skip on the dashboard conflict."
            )
        else:
            message = "Folder markers are structural and cannot be retried."
        return {
            "reason": key,
            "requeued": 0,
            "ineligible": matched,
            "action": "unsupported",
            "message": message,
        }

    if key == "indexing_paused":
        paused_paths = list(await load_paused_folder_paths(session))
        requeued = 0
        for folder_path in paused_paths:
            requeued += await resume_folder_indexing(session, folder_path)

        # Orphaned: pause row gone but files still marked indexing_paused.
        paused_after = await load_paused_folder_paths(session)
        orphans = list(
            (
                await session.execute(
                    select(DriveFile).where(DriveFile.status == DriveFileStatus.SKIPPED)
                )
            ).scalars().all()
        )
        for drive_file in orphans:
            if normalize_skip_reason(drive_file.error_message) != "indexing_paused":
                continue
            if is_file_indexing_paused(drive_file.path, paused_after):
                continue
            if not is_indexable_mime(drive_file.mime_type, drive_file.name):
                continue
            _prepare_for_retry(drive_file)
            requeued += 1

        await session.flush()
        logger.info(
            "Retry-all indexing_paused: resumed %d folder(s), requeued=%d",
            len(paused_paths),
            requeued,
        )
        return {
            "reason": key,
            "requeued": requeued,
            "ineligible": 0,
            "folders_resumed": len(paused_paths),
            "action": "resume_paused",
            "message": (
                f"Resumed {len(paused_paths)} paused folder(s) and queued {requeued} file(s)."
                if paused_paths or requeued
                else "No paused folders or paused skips to resume."
            ),
        }

    # Corrupt / decode / unknown / custom reasons — requeue matching skips.
    paused_paths = await load_paused_folder_paths(session)
    candidates = list(
        (
            await session.execute(
                select(DriveFile)
                .where(DriveFile.status == DriveFileStatus.SKIPPED)
                .order_by(*pending_order_by(get_settings()))
                .limit(max(limit * 2, limit))
            )
        ).scalars().all()
    )

    requeued = 0
    ineligible = 0
    for drive_file in candidates:
        if normalize_skip_reason(drive_file.error_message) != key:
            continue
        if requeued >= limit:
            break
        if is_file_indexing_paused(drive_file.path, paused_paths):
            ineligible += 1
            continue
        if not is_indexable_mime(drive_file.mime_type, drive_file.name):
            ineligible += 1
            continue
        _prepare_for_retry(drive_file)
        requeued += 1

    if requeued:
        await session.flush()
    logger.info("Retry-all reason=%s requeued=%d ineligible=%d", key, requeued, ineligible)
    return {
        "reason": key,
        "requeued": requeued,
        "ineligible": ineligible,
        "action": "requeue",
        "message": (
            f"Requeued {requeued} file(s) for another index attempt."
            + (f" {ineligible} still ineligible (paused folder or non-indexable)." if ineligible else "")
        ),
    }


async def _count_skipped_for_reason(session: AsyncSession, reason: str) -> int:
    rows = list(
        (
            await session.execute(
                select(DriveFile.error_message).where(DriveFile.status == DriveFileStatus.SKIPPED)
            )
        ).scalars().all()
    )
    return sum(1 for msg in rows if normalize_skip_reason(msg) == reason)


async def requeue_errored_by_bucket(
    session: AsyncSession,
    bucket: str,
    *,
    limit: int = RETRY_BY_REASON_LIMIT,
) -> dict[str, object]:
    """Retry ERROR rows matching a normalized error bucket.

    Never requeues structural / junk buckets. ``enospc`` is allowed only when
    the caller has confirmed disk headroom (API documents this).
    """
    key = (bucket or "").strip() or "unknown"
    if key in NON_RETRYABLE_ERROR_BUCKETS:
        matched = await _count_errored_for_bucket(session, key)
        return {
            "bucket": key,
            "requeued": 0,
            "ineligible": matched,
            "action": "unsupported",
            "message": (
                f"Error bucket '{key}' cannot be fixed by requeue "
                "(structural / conflict). Resolve via conflict UI or ignore."
            ),
        }

    paused_paths = await load_paused_folder_paths(session)
    candidates = list(
        (
            await session.execute(
                select(DriveFile)
                .where(DriveFile.status == DriveFileStatus.ERROR)
                .order_by(*pending_order_by(get_settings()))
                .limit(max(limit * 2, limit))
            )
        ).scalars().all()
    )

    requeued = 0
    ineligible = 0
    for drive_file in candidates:
        if normalize_error_bucket(drive_file.error_message) != key:
            continue
        if requeued >= limit:
            break
        if is_file_indexing_paused(drive_file.path, paused_paths):
            ineligible += 1
            continue
        if not is_indexable_mime(drive_file.mime_type, drive_file.name):
            ineligible += 1
            continue
        _prepare_for_retry(drive_file)
        requeued += 1

    if requeued:
        await session.flush()
    logger.info("Retry-errors bucket=%s requeued=%d ineligible=%d", key, requeued, ineligible)
    return {
        "bucket": key,
        "requeued": requeued,
        "ineligible": ineligible,
        "action": "requeue",
        "message": (
            f"Requeued {requeued} ERROR file(s) in bucket '{key}'."
            + (f" {ineligible} still ineligible." if ineligible else "")
        ),
    }


async def _count_errored_for_bucket(session: AsyncSession, bucket: str) -> int:
    rows = list(
        (
            await session.execute(
                select(DriveFile.error_message).where(DriveFile.status == DriveFileStatus.ERROR)
            )
        ).scalars().all()
    )
    return sum(1 for msg in rows if normalize_error_bucket(msg) == bucket)
