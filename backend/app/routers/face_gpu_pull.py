"""Secret-gated Range-capable stream of an already-downloaded video cache file."""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.config import get_settings
from app.db.models import DriveFile
from app.db.session import get_db
from app.faces.video_pull import verify_face_video_pull
from app.video.youtube_cache import video_cache_path

router = APIRouter(tags=["internal"])
_CHUNK = 8 * 1024 * 1024


def _parse_byte_range(header: str | None, file_size: int) -> tuple[int, int] | None:
    """Return inclusive (start, end) for a single ``bytes=`` range, or None for full file."""
    if not header:
        return None
    spec = header.strip()
    if not spec.lower().startswith("bytes="):
        raise HTTPException(status_code=416, detail="invalid range")
    part = spec.split("=", 1)[1].split(",", 1)[0].strip()
    if "-" not in part:
        raise HTTPException(status_code=416, detail="invalid range")
    start_s, end_s = part.split("-", 1)
    if start_s == "" and end_s == "":
        raise HTTPException(status_code=416, detail="invalid range")
    if start_s == "":
        suffix = int(end_s)
        if suffix <= 0:
            raise HTTPException(status_code=416, detail="invalid range")
        start = max(0, file_size - suffix)
        end = file_size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else file_size - 1
    if start < 0 or end < start or start >= file_size:
        raise HTTPException(
            status_code=416,
            detail="range not satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )
    return start, min(end, file_size - 1)


def range_file_response(path: Path, request: Request) -> Response:
    """Serve the cached video with HTTP Range so RunPod can parallel GET."""
    file_size = path.stat().st_size
    filename = path.name
    spanned = _parse_byte_range(request.headers.get("range"), file_size)
    if spanned is None:
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=filename,
            content_disposition_type="attachment",
            headers={"Accept-Ranges": "bytes"},
        )
    start, end = spanned
    length = end - start + 1

    def _iter():
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        _iter(),
        status_code=206,
        media_type="application/octet-stream",
        headers={
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(length),
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.get("/internal/face-gpu-video/{drive_file_id:path}")
async def pull_face_gpu_video(
    drive_file_id: str,
    request: Request,
    exp: int = Query(...),
    sig: str = Query(...),
    session: AsyncSession = Depends(get_db),
) -> Response:
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
    return range_file_response(path, request)
