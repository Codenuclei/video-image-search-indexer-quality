"""Allocate unique index display names when Drive basenames collide."""
from __future__ import annotations

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile, DriveFileStatus


def split_stem_ext(name: str) -> tuple[str, str]:
    base = name.rsplit("/", 1)[-1]
    if "." in base and not base.startswith("."):
        stem, dot, ext = base.rpartition(".")
        return stem, dot + ext
    return base, ""


def disambiguated_basename(base_name: str, n: int) -> str:
    """Return ``photo (n).jpg`` style name for the given Drive basename."""
    stem, ext = split_stem_ext(base_name)
    return f"{stem} ({n}){ext}"


def effective_index_name(drive_file: DriveFile) -> str:
    return (drive_file.index_name or drive_file.name or "").strip()


def _effective_name_sql():
    return func.coalesce(DriveFile.index_name, DriveFile.name)


async def find_by_effective_name(
    session: AsyncSession,
    *,
    name: str,
    exclude_id: str,
) -> DriveFile | None:
    """Find claimed/completed file with the same effective index name (case-insensitive)."""
    lowered = name.strip().lower()
    if not lowered:
        return None
    return (
        await session.execute(
            select(DriveFile)
            .where(
                func.lower(_effective_name_sql()) == lowered,
                DriveFile.id != exclude_id,
                DriveFile.error_message.is_distinct_from("folder_marker"),
                DriveFile.status.in_(
                    (
                        DriveFileStatus.PROCESSING,
                        DriveFileStatus.PROCESSED,
                        DriveFileStatus.ERROR,
                    )
                ),
            )
            .order_by(DriveFile.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def allocate_index_name(
    session: AsyncSession,
    drive_file: DriveFile,
    *,
    desired: str | None = None,
) -> str:
    """Pick the first unused effective index name: ``foo.jpg``, ``foo (1).jpg``, …"""
    seed = (desired or drive_file.name or "").strip()
    if not seed:
        return seed
    candidate = seed
    n = 1
    while await find_by_effective_name(session, name=candidate, exclude_id=drive_file.id):
        candidate = disambiguated_basename(seed, n)
        n += 1
    return candidate
