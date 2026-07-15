"""Pause/resume indexing for folder subtrees in the media library."""
from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile, DriveFileStatus, IndexingFolderPause

logger = logging.getLogger(__name__)

INDEXING_PAUSED_PREFIX = "indexing_paused:"
CORRUPT_SKIPPED_PREFIX = "corrupt_file:"


def normalize_folder_path(folder_path: str) -> str:
    path = (folder_path or "/").replace("\\", "/").strip()
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path or "/"


def normalize_file_path(file_path: str) -> str:
    path = (file_path or "").replace("\\", "/").strip()
    if not path.startswith("/"):
        path = "/" + path
    return path


def file_under_folder(file_path: str, folder_path: str) -> bool:
    """True when file_path is inside folder_path (or equals it)."""
    fp = normalize_file_path(file_path)
    fd = normalize_folder_path(folder_path)
    if fd == "/":
        return True
    return fp.startswith(fd + "/") or fp == fd


def is_indexing_paused_message(error_message: str | None) -> bool:
    return (error_message or "").startswith(INDEXING_PAUSED_PREFIX)


def is_corrupt_skipped_message(error_message: str | None) -> bool:
    msg = error_message or ""
    return msg.startswith(CORRUPT_SKIPPED_PREFIX) or msg.startswith("decode_exhausted:")


async def load_paused_folder_paths(session: AsyncSession) -> list[str]:
    rows = (await session.execute(select(IndexingFolderPause.folder_path))).scalars().all()
    return [normalize_folder_path(p) for p in rows]


def is_file_indexing_paused(file_path: str, paused_paths: list[str]) -> bool:
    return any(file_under_folder(file_path, prefix) for prefix in paused_paths)


async def pause_folder_indexing(session: AsyncSession, folder_path: str) -> int:
    """Stop indexing for a folder and all files beneath it."""
    norm = normalize_folder_path(folder_path)
    existing = (
        await session.execute(
            select(IndexingFolderPause).where(IndexingFolderPause.folder_path == norm)
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(IndexingFolderPause(folder_path=norm))

    rows = list((await session.execute(select(DriveFile))).scalars().all())
    stopped = 0
    msg = f"{INDEXING_PAUSED_PREFIX} indexing stopped for folder {norm}"
    for drive_file in rows:
        if not file_under_folder(drive_file.path, norm):
            continue
        if drive_file.status in (
            DriveFileStatus.PENDING,
            DriveFileStatus.PROCESSING,
            DriveFileStatus.ERROR,
        ):
            drive_file.status = DriveFileStatus.SKIPPED
            drive_file.error_message = msg
            stopped += 1

    await session.flush()
    logger.info("Paused indexing for %s — %d file(s) skipped", norm, stopped)
    return stopped


async def resume_folder_indexing(session: AsyncSession, folder_path: str) -> int:
    """Re-enable indexing for a folder subtree."""
    norm = normalize_folder_path(folder_path)
    await session.execute(
        delete(IndexingFolderPause).where(IndexingFolderPause.folder_path == norm)
    )

    rows = list((await session.execute(select(DriveFile))).scalars().all())
    resumed = 0
    for drive_file in rows:
        if not file_under_folder(drive_file.path, norm):
            continue
        if drive_file.status != DriveFileStatus.SKIPPED:
            continue
        if not is_indexing_paused_message(drive_file.error_message):
            continue
        drive_file.status = DriveFileStatus.PENDING
        drive_file.error_message = None
        resumed += 1

    await session.flush()
    logger.info("Resumed indexing for %s — %d file(s) queued", norm, resumed)
    return resumed


async def skip_corrupt_files(session: AsyncSession) -> int:
    """Permanently skip decode-failed files so indexing can continue elsewhere."""
    from app.pipelines.decode_recovery import (
        is_decode_exhausted,
        is_decode_failure_error,
    )

    rows = list(
        (
            await session.execute(
                select(DriveFile).where(
                    DriveFile.status.in_(
                        [DriveFileStatus.PENDING, DriveFileStatus.ERROR, DriveFileStatus.PROCESSING]
                    )
                )
            )
        ).scalars().all()
    )
    skipped = 0
    for drive_file in rows:
        if is_corrupt_skipped_message(drive_file.error_message):
            continue
        if is_indexing_paused_message(drive_file.error_message):
            continue

        decode_err = is_decode_failure_error(drive_file.error_message)
        exhausted = is_decode_exhausted(drive_file)
        if not decode_err and not exhausted:
            continue

        drive_file.status = DriveFileStatus.SKIPPED
        if exhausted:
            drive_file.error_message = drive_file.error_message or (
                f"{CORRUPT_SKIPPED_PREFIX} decode retries exhausted"
            )
        else:
            drive_file.error_message = (
                f"{CORRUPT_SKIPPED_PREFIX} {(drive_file.error_message or 'decode failed')[:500]}"
            )
        skipped += 1

    if skipped:
        await session.flush()
        logger.info("Skipped %d corrupt/unreadable file(s)", skipped)
    return skipped
