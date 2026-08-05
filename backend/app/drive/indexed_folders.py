"""Persist historical indexed Drive folders with stable Drive URLs."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile, DriveUser, IndexedFolder
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
        row.is_active = mark_active if mark_active else row.is_active
        if mark_active:
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


async def _guess_root_folder_name(session: AsyncSession, root_folder_id: str) -> str:
    """Best-effort display name from a folder marker or a file path under the root."""
    marker = await session.get(DriveFile, root_folder_id)
    if marker is not None and marker.name:
        return marker.name
    sample = (
        await session.execute(
            select(DriveFile.path)
            .where(
                DriveFile.root_folder_id == root_folder_id,
                DriveFile.path.is_not(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if sample:
        # "/Root Name/sub/file.jpg" → "Root Name"
        parts = [p for p in str(sample).split("/") if p]
        if parts:
            return parts[0]
    return root_folder_id


async def ensure_indexed_folder_history(session: AsyncSession) -> int:
    """
    Additive backfill of ``indexed_folders`` from the connected user + existing
    ``drive_files.root_folder_id`` values.

    Never deletes rows, never changes file statuses, never wipes indexed data.
    Safe to call on every ``GET /index/folders``.
    """
    added_or_updated = 0
    user = (await session.execute(select(DriveUser).limit(1))).scalar_one_or_none()
    active_id = user.selected_folder_id if user and user.selected_folder_id else None

    if active_id and user is not None:
        existing_active = await session.get(IndexedFolder, active_id)
        if existing_active is None or not existing_active.is_active:
            await record_indexed_folder(
                session,
                folder_id=active_id,
                folder_name=user.selected_folder_name or active_id,
                drive_user=user,
                file_count=existing_active.last_file_count if existing_active else None,
                mark_active=True,
            )
            added_or_updated += 1
        elif user.selected_folder_name and existing_active.name != user.selected_folder_name:
            existing_active.name = user.selected_folder_name
            existing_active.drive_url = drive_url_for_folder(active_id)
            added_or_updated += 1

    root_counts = (
        await session.execute(
            select(DriveFile.root_folder_id, func.count())
            .where(
                DriveFile.root_folder_id.is_not(None),
                DriveFile.source == "drive",
                # Folder markers are structural; still count real media + markers for size.
                DriveFile.error_message.is_distinct_from("folder_marker"),
            )
            .group_by(DriveFile.root_folder_id)
        )
    ).all()

    for root_id, count in root_counts:
        if not root_id:
            continue
        existing = await session.get(IndexedFolder, root_id)
        if existing is None:
            name = await _guess_root_folder_name(session, root_id)
            await record_indexed_folder(
                session,
                folder_id=root_id,
                folder_name=name,
                drive_user=user if (user and user.selected_folder_id == root_id) else None,
                file_count=int(count),
                mark_active=False,
            )
            added_or_updated += 1
        else:
            if existing.last_file_count is None:
                existing.last_file_count = int(count)
                added_or_updated += 1
            if not existing.drive_url:
                existing.drive_url = drive_url_for_folder(root_id)
                added_or_updated += 1

    if added_or_updated:
        await session.flush()
        logger.info("Indexed-folder history backfill touched %d row(s)", added_or_updated)
    return added_or_updated


async def touch_active_folder_file_count(
    session: AsyncSession, *, file_count: int
) -> None:
    active = (
        await session.execute(select(IndexedFolder).where(IndexedFolder.is_active.is_(True)).limit(1))
    ).scalar_one_or_none()
    if active is not None:
        active.last_file_count = file_count
        active.last_indexed_at = datetime.now(tz=timezone.utc)
