from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile, Face, Media, Person
from app.db.session import get_db
from app.matching.service import rename_person
from app.schemas import MediaOccurrence, PersonOut, RenamePersonRequest

router = APIRouter(prefix="/persons", tags=["persons"])


async def _occurrence_count(session: AsyncSession, person_id: int) -> int:
    stmt = select(func.count()).select_from(Face).where(Face.person_id == person_id)
    return (await session.execute(stmt)).scalar_one()


async def _best_face_id(session: AsyncSession, person: Person) -> int | None:
    """Return the representative face ID, auto-picking the best available if not set."""
    if person.representative_face_id is not None:
        return person.representative_face_id
    # Pick the highest-confidence face with a saved thumbnail for this person
    face = (
        await session.execute(
            select(Face)
            .where(Face.person_id == person.id, Face.thumbnail_path.isnot(None))
            .order_by(Face.detection_confidence.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return face.id if face else None


@router.get("", response_model=list[PersonOut])
async def list_persons(session: AsyncSession = Depends(get_db)) -> list[PersonOut]:
    persons = (await session.execute(select(Person).order_by(Person.name))).scalars().all()
    return [
        PersonOut(
            id=p.id,
            name=p.name,
            representative_face_id=await _best_face_id(session, p),
            occurrence_count=await _occurrence_count(session, p.id),
            created_at=p.created_at,
        )
        for p in persons
    ]


@router.get("/{person_id}", response_model=PersonOut)
async def get_person(person_id: int, session: AsyncSession = Depends(get_db)) -> PersonOut:
    person = await session.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return PersonOut(
        id=person.id,
        name=person.name,
        representative_face_id=await _best_face_id(session, person),
        occurrence_count=await _occurrence_count(session, person.id),
        created_at=person.created_at,
    )


@router.patch("/{person_id}", response_model=PersonOut)
async def update_person_name(
    person_id: int,
    body: RenamePersonRequest,
    session: AsyncSession = Depends(get_db),
) -> PersonOut:
    """Rename a tagged person and refresh Gemini metadata on linked files."""
    try:
        person = await rename_person(session, person_id, body.name)
        await session.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return PersonOut(
        id=person.id,
        name=person.name,
        representative_face_id=await _best_face_id(session, person),
        occurrence_count=await _occurrence_count(session, person.id),
        created_at=person.created_at,
    )


@router.get("/{person_id}/media", response_model=list[MediaOccurrence])
async def get_person_media(person_id: int, session: AsyncSession = Depends(get_db)) -> list[MediaOccurrence]:
    """Every piece of media this person appears in."""
    person = await session.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")

    stmt = (
        select(
            Media.id,
            DriveFile.id,
            DriveFile.name,
            DriveFile.path,
            Media.type,
            func.min(Face.frame_timestamp),
        )
        .join(Face, Face.media_id == Media.id)
        .join(DriveFile, DriveFile.id == Media.drive_file_id)
        .where(Face.person_id == person_id)
        .group_by(Media.id, DriveFile.id, DriveFile.name, DriveFile.path, Media.type)
    )
    rows = (await session.execute(stmt)).all()
    return [
        MediaOccurrence(
            media_id=media_id,
            drive_file_id=drive_id,
            name=name,
            path=path,
            media_type=media_type.value,
            frame_timestamp=frame_timestamp,
        )
        for media_id, drive_id, name, path, media_type, frame_timestamp in rows
    ]
