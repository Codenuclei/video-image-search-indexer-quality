"""Re-queue images that failed to decode (HEIC/AVIF/TIFF/ARW/RAW) — with retry limits."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db.models import DriveFile, DriveFileStatus
from app.pipelines.common import is_image_mime, needs_jpeg_normalization
from app.pipelines.image_formats import (
    error_suggests_decode_failure,
    infer_image_mime,
    is_recoverable_image_extension,
)

logger = logging.getLogger(__name__)

DECODE_EXHAUSTED_PREFIX = "decode_exhausted:"


def decode_max_attempts() -> int:
    return max(1, get_settings().decode_max_attempts)


def is_decode_exhausted(drive_file: DriveFile) -> bool:
    msg = (drive_file.error_message or "").lower()
    if msg.startswith(DECODE_EXHAUSTED_PREFIX):
        return True
    return (drive_file.decode_attempts or 0) >= decode_max_attempts()


def is_decode_failure_error(error_message: str | None) -> bool:
    return error_suggests_decode_failure(error_message)


def is_decode_recoverable(drive_file: DriveFile) -> bool:
    """True when an ERROR/SKIPPED file likely failed due to codec / format support."""
    if is_decode_exhausted(drive_file):
        return False

    name_lower = (drive_file.name or "").lower()
    mime = infer_image_mime(drive_file.mime_type or "", drive_file.name)
    is_image = is_image_mime(mime, drive_file.name) or is_recoverable_image_extension(drive_file.name)
    if not is_image:
        return False

    msg = (drive_file.error_message or "").lower()

    if drive_file.status == DriveFileStatus.SKIPPED:
        if msg.startswith(DECODE_EXHAUSTED_PREFIX):
            return False
        return "unsupported mime" in msg and is_recoverable_image_extension(drive_file.name)

    if drive_file.status != DriveFileStatus.ERROR:
        return False

    if not is_decode_failure_error(drive_file.error_message):
        return False

    return is_recoverable_image_extension(drive_file.name) or needs_jpeg_normalization(
        mime, drive_file.name
    )


def exhausted_decode_message(file_name: str, attempts: int) -> str:
    return (
        f"{DECODE_EXHAUSTED_PREFIX} gave up after {attempts} decode attempt(s) "
        f"for {file_name or 'image'} — install libraw/rawpy support or convert to JPEG"
    )


def apply_decode_failure(
    drive_file: DriveFile,
    error_message: str,
    *,
    max_attempts: int | None = None,
) -> DriveFileStatus:
    """Record a decode/index failure; permanently skip when retries are exhausted."""
    limit = max_attempts if max_attempts is not None else decode_max_attempts()
    drive_file.decode_attempts = (drive_file.decode_attempts or 0) + 1
    attempts = drive_file.decode_attempts

    if is_decode_failure_error(error_message) and attempts >= limit:
        drive_file.status = DriveFileStatus.SKIPPED
        drive_file.error_message = exhausted_decode_message(drive_file.name, attempts)
        return DriveFileStatus.SKIPPED

    drive_file.status = DriveFileStatus.ERROR
    drive_file.error_message = error_message[:2000]
    return DriveFileStatus.ERROR


async def quarantine_stuck_decode_errors(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """Stop pre-migration infinite loops (ERROR + decode failure but decode_attempts still 0)."""
    limit = decode_max_attempts()
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(DriveFile).where(
                    DriveFile.status.in_([DriveFileStatus.ERROR, DriveFileStatus.SKIPPED])
                )
            )
        ).scalars().all()

        quarantined = 0
        for drive_file in rows:
            if is_decode_exhausted(drive_file):
                continue

            attempts = drive_file.decode_attempts or 0
            if attempts >= limit and drive_file.status == DriveFileStatus.ERROR:
                drive_file.status = DriveFileStatus.SKIPPED
                drive_file.error_message = exhausted_decode_message(drive_file.name, attempts)
                quarantined += 1
                continue

            if attempts != 0:
                continue
            if drive_file.status != DriveFileStatus.ERROR:
                continue
            if not is_recoverable_image_extension(drive_file.name):
                continue
            if not is_decode_failure_error(drive_file.error_message):
                continue

            drive_file.decode_attempts = limit
            drive_file.status = DriveFileStatus.SKIPPED
            drive_file.error_message = exhausted_decode_message(drive_file.name, limit)
            quarantined += 1

        if quarantined:
            await session.commit()
            logger.warning(
                "Quarantined %d stuck decode-error image(s) — will not retry automatically",
                quarantined,
            )
        return quarantined


async def reset_codec_skipped_files(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """Give TIFF/RAW files marked decode_exhausted a fresh attempt after codec upgrades."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(DriveFile).where(DriveFile.status == DriveFileStatus.SKIPPED)
            )
        ).scalars().all()

        reset = 0
        for drive_file in rows:
            msg = drive_file.error_message or ""
            if not msg.startswith(DECODE_EXHAUSTED_PREFIX):
                continue
            if not is_recoverable_image_extension(drive_file.name):
                continue
            drive_file.status = DriveFileStatus.PENDING
            drive_file.decode_attempts = 0
            drive_file.error_message = None
            reset += 1

        if reset:
            await session.commit()
            logger.info("Reset %d codec-skipped image(s) for retry after decode upgrade", reset)
        return reset


async def requeue_decode_errors(session_factory: async_sessionmaker[AsyncSession]) -> list[str]:
    """Reset recoverable decode failures to pending (startup only, capped retries)."""
    limit = decode_max_attempts()
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(DriveFile).where(
                    DriveFile.status.in_([DriveFileStatus.ERROR, DriveFileStatus.SKIPPED])
                )
            )
        ).scalars().all()

        requeued: list[str] = []
        for drive_file in rows:
            if not is_decode_recoverable(drive_file):
                continue
            if (drive_file.decode_attempts or 0) >= limit:
                drive_file.status = DriveFileStatus.SKIPPED
                drive_file.error_message = exhausted_decode_message(
                    drive_file.name, drive_file.decode_attempts or limit
                )
                continue
            inferred = infer_image_mime(drive_file.mime_type or "", drive_file.name)
            if inferred and inferred != drive_file.mime_type:
                drive_file.mime_type = inferred
            drive_file.status = DriveFileStatus.PENDING
            drive_file.error_message = None
            drive_file.last_synced_at = datetime.now(timezone.utc)
            requeued.append(drive_file.id)

        if requeued:
            await session.commit()
            logger.info(
                "Requeued %d decode-error image(s) for re-indexing: %s",
                len(requeued),
                ", ".join(requeued[:8]) + ("…" if len(requeued) > 8 else ""),
            )

        return requeued
