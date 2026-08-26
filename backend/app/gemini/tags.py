from __future__ import annotations

from sqlalchemy import select, union
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Face, FaceCluster, Media, Person


async def person_names_for_drive_file(session: AsyncSession, drive_file_id: str) -> list[str]:
    """Person names linked directly or through a tagged face cluster."""
    return (await person_names_for_drive_files(session, [drive_file_id])).get(
        drive_file_id,
        [],
    )


async def person_names_for_drive_files(
    session: AsyncSession,
    drive_file_ids: list[str],
) -> dict[str, list[str]]:
    """Bulk variant of person_names_for_drive_file for large search result sets."""
    ids = list(dict.fromkeys(fid for fid in drive_file_ids if fid))
    if not ids:
        return {}
    direct = (
        select(Media.drive_file_id, Person.name)
        .join(Face, Face.media_id == Media.id)
        .join(Person, Person.id == Face.person_id)
        .where(Media.drive_file_id.in_(ids))
    )
    clustered = (
        select(Media.drive_file_id, Person.name)
        .join(Face, Face.media_id == Media.id)
        .join(FaceCluster, FaceCluster.id == Face.cluster_id)
        .join(Person, Person.id == FaceCluster.person_id)
        .where(Media.drive_file_id.in_(ids))
    )
    stmt = union(direct, clustered)
    names_by_file: dict[str, list[str]] = {fid: [] for fid in ids}
    for drive_file_id, person_name in (await session.execute(stmt)).all():
        names_by_file.setdefault(drive_file_id, []).append(person_name)
    for names in names_by_file.values():
        names.sort(key=str.casefold)
    return names_by_file
