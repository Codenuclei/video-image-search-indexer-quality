from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Face
from app.db.session import get_db
from app.schemas import FaceOut

router = APIRouter(prefix="/faces", tags=["faces"])


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


@router.get("/{face_id}/thumbnail")
async def get_face_thumbnail(face_id: int, session: AsyncSession = Depends(get_db)) -> FileResponse:
    face = await session.get(Face, face_id)
    if face is None or not face.thumbnail_path or not os.path.exists(face.thumbnail_path):
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(face.thumbnail_path, media_type="image/jpeg")
