"""Secret-gated Range-capable stream of an already-downloaded video cache file."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import DriveFile
from app.db.session import get_db
from app.faces.video_pull import verify_face_video_pull
from app.video.youtube_cache import video_cache_path

router = APIRouter(tags=["internal"])


@router.get("/internal/face-gpu-video/{drive_file_id:path}")
async def pull_face_gpu_video(
    drive_file_id: str,
    exp: int = Query(...),
    sig: str = Query(...),
    session: AsyncSession = Depends(get_db),
) -> FileResponse:
    settings = get_settings()
    secret = (settings.runpod_api_key or "").strip()
    if time.time() > int(exp):
        raise HTTPException(status_code=403, detail="expired")
    if not verify_face_video_pull(drive_file_id, exp, sig, secret):
        raise HTTPException(status_code=403, detail="invalid signature")
    drive_file = await session.get(DriveFile, drive_file_id)
    if drive_file is None:
        raise HTTPException(status_code=404, detail="not found")
    path = video_cache_path(settings, drive_file)
    if not path.is_file() or path.stat().st_size <= 0:
        raise HTTPException(status_code=404, detail="not cached")
    max_bytes = max(1, int(settings.runpod_face_video_max_bytes))
    if path.stat().st_size > max_bytes:
        raise HTTPException(status_code=413, detail="video too large")
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=path.name,
        content_disposition_type="attachment",
    )
