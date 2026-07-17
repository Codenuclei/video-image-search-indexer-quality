from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Face, Media, Person
from app.db.session import get_db
from app.matching.service import create_manual_face_box, tag_face_manual
from app.runtime_settings import get_runtime_settings
from app.schemas import FaceOut, ManualFaceBoxRequest, PersonOut, TagFaceRequest

router = APIRouter(prefix="/faces", tags=["faces"])


def _face_out(face: Face, person_name: str | None = None) -> dict:
    return {
        **{k: getattr(face, k) for k in (
            "id", "media_id", "bbox_x", "bbox_y", "bbox_width", "bbox_height",
            "detection_confidence", "frame_timestamp", "page_number",
            "cluster_id", "person_id",
        )},
        "has_thumbnail": bool(face.thumbnail_path),
        "person_name": person_name,
    }


@router.get("", response_model=list[FaceOut])
async def list_faces(
    media_id: int | None = None,
    person_id: int | None = None,
    cluster_id: int | None = None,
    limit: int = 200,
    session: AsyncSession = Depends(get_db),
) -> list[FaceOut]:
    stmt = select(Face).order_by(Face.id.desc()).limit(limit)
    if media_id is not None:
        stmt = stmt.where(Face.media_id == media_id)
    if person_id is not None:
        stmt = stmt.where(Face.person_id == person_id)
    if cluster_id is not None:
        stmt = stmt.where(Face.cluster_id == cluster_id)

    faces = (await session.execute(stmt)).scalars().all()
    return [
        FaceOut.model_validate({**f.__dict__, "has_thumbnail": bool(f.thumbnail_path)}) for f in faces
    ]


@router.get("/by-file/{drive_file_id}")
async def list_faces_for_drive_file(
    drive_file_id: str,
    session: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Faces on a Drive file (for experimental manual tagging overlay)."""
    media = (
        await session.execute(select(Media).where(Media.drive_file_id == drive_file_id))
    ).scalar_one_or_none()
    if media is None:
        return []
    faces = (
        await session.execute(select(Face).where(Face.media_id == media.id).order_by(Face.id))
    ).scalars().all()
    out: list[dict] = []
    for face in faces:
        person_name = None
        if face.person_id is not None:
            person = await session.get(Person, face.person_id)
            person_name = person.name if person else None
        out.append(_face_out(face, person_name))
    return out


@router.post("/manual-box")
async def create_manual_face_box_endpoint(
    body: ManualFaceBoxRequest,
    session: AsyncSession = Depends(get_db),
) -> dict:
    """
    Experimental: user-drawn bbox on an image. No detection, embedding, or Gemini.
    """
    if not get_runtime_settings().experimental_manual_face_tag:
        raise HTTPException(
            status_code=403,
            detail="Enable Experimental → Manual face tagging in Settings first",
        )
    try:
        face, person = await create_manual_face_box(
            session,
            drive_file_id=body.drive_file_id,
            bbox_x=body.bbox_x,
            bbox_y=body.bbox_y,
            bbox_width=body.bbox_width,
            bbox_height=body.bbox_height,
            name=body.name,
        )
        await session.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "face": _face_out(face, person.name if person else None),
        "person": (
            {
                "id": person.id,
                "name": person.name,
            }
            if person
            else None
        ),
    }


@router.post("/{face_id}/tag", response_model=PersonOut)
async def tag_face_endpoint(
    face_id: int,
    body: TagFaceRequest,
    session: AsyncSession = Depends(get_db),
) -> PersonOut:
    """
    Experimental manual tag: assign a name to one face only.
    Skips Gemini requeue / Drive status updates — append-only and cheap.
    """
    if not get_runtime_settings().experimental_manual_face_tag:
        raise HTTPException(
            status_code=403,
            detail="Enable Experimental → Manual face tagging in Settings first",
        )
    try:
        person = await tag_face_manual(session, face_id, body.name)
        await session.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    from app.routers.persons import _serialize_person

    return await _serialize_person(session, person)


@router.get("/{face_id}/thumbnail")
async def get_face_thumbnail(face_id: int, session: AsyncSession = Depends(get_db)) -> FileResponse:
    face = await session.get(Face, face_id)
    if face is None or not face.thumbnail_path or not os.path.exists(face.thumbnail_path):
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(face.thumbnail_path, media_type="image/jpeg")
