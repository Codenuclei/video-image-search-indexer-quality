from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Face, Media, Person


async def person_names_for_drive_file(session: AsyncSession, drive_file_id: str) -> list[str]:
    """Manually tagged person names linked to this Drive file (never Gemini-guessed)."""
    stmt = (
        select(Person.name)
        .join(Face, Face.person_id == Person.id)
        .join(Media, Media.id == Face.media_id)
        .where(Media.drive_file_id == drive_file_id)
        .distinct()
        .order_by(Person.name)
    )
    return list((await session.execute(stmt)).scalars().all())
