from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile, Face, Media, Person
from app.db.session import get_db
from app.matching.service import delete_person, update_person
from app.schemas import MediaOccurrence, PersonOut, RenamePersonRequest, UpdatePersonRequest

router = APIRouter(prefix="/persons", tags=["persons"])


async def _occurrence_count(session: AsyncSession, person_id: int) -> int:
    stmt = select(func.count()).select_from(Face).where(Face.person_id == person_id)
    return (await session.execute(stmt)).scalar_one()


async def _best_face_id(session: AsyncSession, person: Person) -> int | None:
    """Return the representative face ID, auto-picking the best available if not set."""
    if person.representative_face_id is not None:
        return person.representative_face_id
    face = (
        await session.execute(
            select(Face)
            .where(Face.person_id == person.id, Face.thumbnail_path.isnot(None))
            .order_by(Face.detection_confidence.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return face.id if face else None


def _person_out(session: AsyncSession, person: Person, occurrence_count: int, face_id: int | None) -> PersonOut:
    return PersonOut(
        id=person.id,
        name=person.name,
        role=person.role,
        representative_face_id=face_id,
        occurrence_count=occurrence_count,
        created_at=person.created_at,
    )


async def _serialize_person(session: AsyncSession, person: Person) -> PersonOut:
    return _person_out(
        session,
        person,
        await _occurrence_count(session, person.id),
        await _best_face_id(session, person),
    )


@router.get("", response_model=list[PersonOut])
async def list_persons(session: AsyncSession = Depends(get_db)) -> list[PersonOut]:
    persons = (await session.execute(select(Person).order_by(Person.name))).scalars().all()
    return [await _serialize_person(session, p) for p in persons]


@router.get("/search", response_model=list[PersonOut])
async def search_persons(
    q: str = "",
    limit: int = 20,
    session: AsyncSession = Depends(get_db),
) -> list[PersonOut]:
    """Search named people by substring (for merge picker in review queue)."""
    query = q.strip()
    if not query:
        return []
    limit = max(1, min(limit, 50))
    persons = (
        await session.execute(
            select(Person)
            .where(Person.name.ilike(f"%{query}%"))
            .order_by(Person.name)
            .limit(limit)
        )
    ).scalars().all()
    return [await _serialize_person(session, p) for p in persons]


@router.get("/{person_id}", response_model=PersonOut)
async def get_person(person_id: int, session: AsyncSession = Depends(get_db)) -> PersonOut:
    person = await session.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return await _serialize_person(session, person)


@router.patch("/{person_id}", response_model=PersonOut)
async def update_person_endpoint(
    person_id: int,
    body: UpdatePersonRequest,
    session: AsyncSession = Depends(get_db),
) -> PersonOut:
    """Rename and/or set student / non-student role on a tagged person."""
    if body.name is None and "role" not in body.model_fields_set:
        raise HTTPException(status_code=400, detail="Provide name and/or role to update")

    try:
        person = await update_person(
            session,
            person_id,
            name=body.name,
            set_role="role" in body.model_fields_set,
            role=body.role,
        )
        await session.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return await _serialize_person(session, person)


@router.put("/{person_id}/name", response_model=PersonOut, include_in_schema=False)
async def update_person_name_legacy(
    person_id: int,
    body: RenamePersonRequest,
    session: AsyncSession = Depends(get_db),
) -> PersonOut:
    """Backward-compatible rename endpoint."""
    try:
        person = await update_person(session, person_id, name=body.name)
        await session.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await _serialize_person(session, person)


@router.delete("/{person_id}", status_code=204)
async def delete_person_endpoint(person_id: int, session: AsyncSession = Depends(get_db)) -> None:
    """Delete a person name; faces unlink and clusters return to the review queue."""
    try:
        await delete_person(session, person_id)
        await session.commit()
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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
