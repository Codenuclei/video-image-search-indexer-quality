"""Persist historical indexed Drive folders with stable Drive URLs."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveUser, IndexedFolder
from app.drive.content_hash import drive_url_for_folder

logger = logging.getLogger(__name__)


async def record_indexed_folder(
    session: AsyncSession,
    *,
    folder_id: str,
    folder_name: str,
    drive_user: DriveUser | None = None,
    file_count: int | None = None,
    mark_active: bool = True,
) -> IndexedFolder:
    """Upsert a folder history row and optionally mark it as the active sync root."""
    now = datetime.now(tz=timezone.utc)
    if mark_active:
        await session.execute(
            update(IndexedFolder).where(IndexedFolder.is_active.is_(True)).values(is_active=False)
        )

    row = await session.get(IndexedFolder, folder_id)
    if row is None:
        row = IndexedFolder(
            id=folder_id,
            name=folder_name,
            drive_url=drive_url_for_folder(folder_id),
            drive_user_id=drive_user.id if drive_user else None,
            drive_user_email=drive_user.email if drive_user else None,
            is_active=mark_active,
            first_indexed_at=now,
            last_indexed_at=now,
            last_file_count=file_count,
        )
        session.add(row)
        logger.info("Recorded new indexed folder %s (%s)", folder_name, folder_id)
    else:
        row.name = folder_name or row.name
        row.drive_url = drive_url_for_folder(folder_id)
        row.is_active = mark_active
        row.last_indexed_at = now
        if drive_user is not None:
            row.drive_user_id = drive_user.id
            row.drive_user_email = drive_user.email
        if file_count is not None:
            row.last_file_count = file_count
        logger.info("Updated indexed folder history %s (%s)", folder_name, folder_id)
    await session.flush()
    return row


async def list_indexed_folders(session: AsyncSession) -> list[IndexedFolder]:
    rows = list(
        (
            await session.execute(
                select(IndexedFolder).order_by(
                    IndexedFolder.is_active.desc(),
                    IndexedFolder.last_indexed_at.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    return rows


async def touch_active_folder_file_count(
    session: AsyncSession, *, file_count: int
) -> None:
    active = (
        await session.execute(select(IndexedFolder).where(IndexedFolder.is_active.is_(True)).limit(1))
    ).scalar_one_or_none()
    if active is not None:
        active.last_file_count = file_count
        active.last_indexed_at = datetime.now(tz=timezone.utc)
