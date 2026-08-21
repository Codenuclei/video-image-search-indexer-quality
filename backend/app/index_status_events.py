"""Lightweight index-status revision for SSE (no full IndexStatus build)."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile, DriveFileStatus
from app.drive.library_filters import sql_exclude_blocked_drive_files


async def cheap_index_revision(session: AsyncSession) -> tuple[str, bool]:
    """Return (revision, is_busy). Busy = pending or processing > 0."""
    stmt = (
        select(DriveFile.status, func.count())
        .where(sql_exclude_blocked_drive_files(DriveFile))
        .group_by(DriveFile.status)
    )
    rows = (await session.execute(stmt)).all()
    counts = {status.value: int(count) for status, count in rows}
    pending = int(counts.get(DriveFileStatus.PENDING.value, 0) or 0)
    processing = int(counts.get(DriveFileStatus.PROCESSING.value, 0) or 0)
    counts_part = ",".join(f"{k}:{counts[k]}" for k in sorted(counts.keys()))
    revision = f"{pending}|{processing}|{counts_part}"
    return revision, pending > 0 or processing > 0
