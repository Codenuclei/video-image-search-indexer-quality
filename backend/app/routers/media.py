from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import DriveFile, Face, Media, MediaType, OcrPage, VideoSegment
from app.db.session import get_db
from app.schemas import FaceOut, MediaOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/media", tags=["media"])


async def _face_count(session: AsyncSession, media_id: int) -> int:
    stmt = select(func.count()).select_from(Face).where(Face.media_id == media_id)
    return (await session.execute(stmt)).scalar_one()


@router.get("", response_model=list[MediaOut])
async def list_media(
    media_type: str | None = None, limit: int = 200, session: AsyncSession = Depends(get_db)
) -> list[MediaOut]:
    stmt = select(Media).order_by(Media.created_at.desc()).limit(limit)
    if media_type:
        stmt = stmt.where(Media.type == media_type)
    items = (await session.execute(stmt)).scalars().all()
    return [
        MediaOut(
            id=m.id,
            drive_file_id=m.drive_file_id,
            type=m.type.value,
            page_count=m.page_count,
            duration_seconds=m.duration_seconds,
            face_count=await _face_count(session, m.id),
            created_at=m.created_at,
        )
        for m in items
    ]


@router.get("/{media_id}", response_model=MediaOut)
async def get_media(media_id: int, session: AsyncSession = Depends(get_db)) -> MediaOut:
    media = await session.get(Media, media_id)
    if media is None:
        raise HTTPException(status_code=404, detail="Media not found")
    return MediaOut(
        id=media.id,
        drive_file_id=media.drive_file_id,
        type=media.type.value,
        page_count=media.page_count,
        duration_seconds=media.duration_seconds,
        face_count=await _face_count(session, media.id),
        created_at=media.created_at,
    )


@router.get("/{media_id}/faces", response_model=list[FaceOut])
async def get_media_faces(media_id: int, session: AsyncSession = Depends(get_db)) -> list[FaceOut]:
    faces = (await session.execute(select(Face).where(Face.media_id == media_id))).scalars().all()
    return [FaceOut.model_validate({**f.__dict__, "has_thumbnail": bool(f.thumbnail_path)}) for f in faces]


@router.get("/carousel-ref/{file_id}")
async def get_carousel_ref_image(file_id: str) -> FileResponse:
    """Serve an image uploaded as a carousel theme/hook reference."""
    import re

    safe = (file_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}\.(jpg|jpeg|png|webp|gif)", safe, flags=re.IGNORECASE):
        raise HTTPException(status_code=400, detail="Invalid carousel ref id")
    path = Path(get_settings().thumbnail_dir) / "carousel_refs" / safe
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Carousel ref image not found")
    ext = path.suffix.lower()
    media_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "application/octet-stream")
    return FileResponse(path, media_type=media_type)


@router.get("/video/{drive_file_id}/frame")
async def get_video_frame(
    drive_file_id: str,
    ts: float = Query(..., ge=0),
    download: bool = Query(False),
    cache_only: bool = Query(False),
    filename: str | None = Query(None),
    session: AsyncSession = Depends(get_db),
) -> FileResponse:
    """
    Serve a keyframe JPEG for a video moment.

    Priority:
    1. Pre-extracted frame on disk (cached from pipeline or previous on-demand call)
    2. On-demand extraction from local video cache (Drive/YouTube/upload) when present
    3. On-demand extraction via ffmpeg streaming from Google Drive (requires OAuth)
    4. 404/401 if unreachable
    """
    settings = get_settings()
    frames_dir = Path(settings.thumbnail_dir) / "video" / drive_file_id
    out_path = frames_dir / f"{ts:.3f}.jpg"

    def _respond(path: Path) -> FileResponse:
        safe = (filename or f"{drive_file_id}_{ts:.3f}.jpg").replace('"', "").replace("\n", "")
        if not safe.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            safe = f"{safe}.jpg"
        headers = {}
        if download:
            headers["Content-Disposition"] = f'attachment; filename="{safe}"'
        return FileResponse(path, media_type="image/jpeg", headers=headers or None)

    # 1. Exact pre-extracted frame
    if out_path.is_file():
        return _respond(out_path)

    # Ready carousel artifacts use cache_only deliberately: a miss must fail
    # immediately rather than collapsing distinct panel timestamps onto the
    # same nearest JPEG (or turning an image GET into ffmpeg/Drive I/O).
    if cache_only:
        raise HTTPException(status_code=404, detail="Cached frame not available")

    # 2. Nearest pre-extracted frame (within ±5 s tolerance) — interactive only
    if frames_dir.is_dir():
        candidates = sorted(
            frames_dir.glob("*.jpg"),
            key=lambda p: abs(float(p.stem) - ts),
        )
        if candidates and abs(float(candidates[0].stem) - ts) <= 5.0:
            return _respond(candidates[0])

    # 3. Check VideoSegment.frame_path in DB (pre-indexed frames, any timestamp)
    seg = (
        await session.execute(
            select(VideoSegment)
            .join(Media, VideoSegment.media_id == Media.id)
            .where(
                Media.drive_file_id == drive_file_id,
                VideoSegment.frame_path.isnot(None),
            )
            .order_by(func.abs(VideoSegment.start_sec - ts))
            .limit(1)
        )
    ).scalar_one_or_none()

    if seg and seg.frame_path and Path(seg.frame_path).is_file():
        return _respond(Path(seg.frame_path))

    # 4. On-demand extraction (local cache first; live Drive stream last)
    frames_dir.mkdir(parents=True, exist_ok=True)
    ok = await _extract_frame_on_demand(drive_file_id, ts, out_path, settings, session)
    if ok and out_path.is_file():
        return _respond(out_path)

    from sqlalchemy import select as sa_select

    from app.db.models import DriveUser
    from app.video.youtube_cache import video_cache_path

    drive_file = await session.get(DriveFile, drive_file_id)
    has_local = bool(
        drive_file is not None and video_cache_path(settings, drive_file).is_file()
    )
    drive_user = (
        await session.execute(sa_select(DriveUser).limit(1))
    ).scalar_one_or_none()
    if drive_file is not None and not has_local and drive_user is None:
        raise HTTPException(
            status_code=401,
            detail=(
                "Reconnect Google Drive to extract frames for this video "
                "(not cached locally)."
            ),
        )

    raise HTTPException(status_code=404, detail="Frame not available")


async def _extract_frame_on_demand(
    drive_file_id: str,
    ts: float,
    out_path: Path,
    settings,
    session: AsyncSession,
) -> bool:
    """
    Ask ffmpeg to seek to *ts* and save one JPEG frame.

    Prefer local video cache (Drive, YouTube, upload) so generation keeps working
    after Drive OAuth disconnect. Live Drive API streaming is a last resort and
    requires a connected account.
    """
    from app.db.models import DriveFile
    from app.video.youtube_cache import video_cache_path
    from app.video.youtube_registry import is_youtube_source

    drive_file = await session.get(DriveFile, drive_file_id)
    if drive_file is not None:
        src = video_cache_path(settings, drive_file)
        if src.is_file():
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(ts),
                "-i", str(src),
                "-frames:v", "1",
                "-q:v", "3",
                str(out_path),
            ]

            def _run_local() -> bool:
                try:
                    result = subprocess.run(cmd, capture_output=True, timeout=120)
                    return result.returncode == 0
                except (subprocess.TimeoutExpired, FileNotFoundError):
                    return False

            return await asyncio.to_thread(_run_local)

        if is_youtube_source(drive_file):
            logger.warning("Frame on-demand: YouTube local file missing for %s", drive_file_id)
            return False

    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select as sa_select

    from app.db.models import DriveUser
    from app.drive.google_client import _do_token_refresh

    # Live Drive stream — requires OAuth (uncached Drive bytes only).
    user: DriveUser | None = (
        await session.execute(sa_select(DriveUser).limit(1))
    ).scalar_one_or_none()
    if user is None:
        logger.warning(
            "Frame on-demand: no local cache and no Drive account connected for %s",
            drive_file_id,
        )
        return False

    now = datetime.now(tz=timezone.utc)
    if user.token_expiry is None or user.token_expiry - timedelta(minutes=5) <= now:
        if not user.refresh_token:
            logger.warning("Frame on-demand: token expired and no refresh_token")
            return False
        try:
            new_token, new_expiry = await asyncio.to_thread(
                _do_token_refresh,
                user.refresh_token,
                settings.google_client_id,
                settings.google_client_secret,
            )
            user.access_token = new_token
            user.token_expiry = new_expiry
            await session.commit()
        except Exception as exc:
            logger.warning("Frame on-demand: token refresh failed: %s", exc)
            return False

    access_token = user.access_token
    drive_url = f"https://www.googleapis.com/drive/v3/files/{drive_file_id}?alt=media"

    cmd = [
        "ffmpeg", "-y",
        "-headers", f"Authorization: Bearer {access_token}\r\n",
        "-ss", str(ts),
        "-i", drive_url,
        "-frames:v", "1",
        "-q:v", "3",
        str(out_path),
    ]

    def _run() -> bool:
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=120)
            if result.returncode != 0:
                logger.warning(
                    "ffmpeg on-demand frame extraction failed for %s@%.2fs: %s",
                    drive_file_id, ts,
                    result.stderr[-400:].decode(errors="replace"),
                )
                return False
            return True
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.warning("Frame extraction error (%s): %s", type(exc).__name__, exc)
            return False

    return await asyncio.to_thread(_run)


@router.get("/{media_id}/ocr")
async def get_media_ocr(media_id: int, session: AsyncSession = Depends(get_db)) -> list[dict]:
    pages = (
        (await session.execute(select(OcrPage).where(OcrPage.media_id == media_id).order_by(OcrPage.page_number)))
        .scalars()
        .all()
    )
    return [{"page_number": p.page_number, "text": p.text} for p in pages]
