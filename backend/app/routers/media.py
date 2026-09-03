from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_credentials import carousel_google
from app.config import get_settings
from app.db.models import DriveFile, Face, Media, MediaType, OcrPage, VideoSegment
from app.db.session import get_db, get_session_factory
from app.video.ffmpeg_utils import ffmpeg_carousel_still_cmd
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


# Instagram carousel portrait (1080x1350). Indexer frames are cached at the
# source video's native aspect (9:16 for reels, 16:9 for landscape), so
# carousel consumers request `ar=4x5` and get a cached serve-time crop.
_PORTRAIT_ASPECT = 4 / 5


def _ensure_portrait_crop(source: Path, variant: Path) -> Path:
    """Return a cached 4:5 crop of *source*; fall back to the original on error."""
    from PIL import Image

    try:
        if variant.is_file() and variant.stat().st_mtime >= source.stat().st_mtime:
            return variant
        with Image.open(source) as im:
            width, height = im.size
            if not width or not height:
                return source
            current = width / height
            if abs(current - _PORTRAIT_ASPECT) < 0.01:
                return source
            if current > _PORTRAIT_ASPECT:
                new_w = round(height * _PORTRAIT_ASPECT)
                x0 = (width - new_w) // 2
                box = (x0, 0, x0 + new_w, height)
            else:
                new_h = round(width / _PORTRAIT_ASPECT)
                # Bias upward: faces and on-screen text live in the upper part
                # of vertical reels.
                y0 = round((height - new_h) * 0.33)
                box = (0, y0, width, y0 + new_h)
            variant.parent.mkdir(parents=True, exist_ok=True)
            im.convert("RGB").crop(box).save(variant, "JPEG", quality=90)
        return variant
    except Exception as exc:  # noqa: BLE001
        logger.debug("portrait crop failed for %s: %s", source, exc)
        return source


@router.get("/video/{drive_file_id}/frame")
async def get_video_frame(
    drive_file_id: str,
    ts: float = Query(..., ge=0),
    download: bool = Query(False),
    cache_only: bool = Query(False),
    filename: str | None = Query(None),
    ar: str | None = Query(None),
    variant: str | None = Query(None),
    session: AsyncSession = Depends(get_db),
) -> FileResponse:
    """
    Serve a keyframe JPEG for a video moment.

    Priority:
    1. Pre-extracted frame on disk (cached from pipeline or previous on-demand call)
    2. On-demand extraction from local video cache (Drive/YouTube/upload) when present
    3. On-demand extraction via ffmpeg streaming from Google Drive (requires OAuth)
    4. 404/401 if unreachable

    ``ar=4x5`` serves an Instagram-carousel 4:5 crop of the frame (cached).
    ``variant=hdr`` serves a prebuilt natural-HDR derivative when present
    (cache_only misses do not invent HDR on the fly).
    """
    settings = get_settings()
    frames_dir = Path(settings.thumbnail_dir) / "video" / drive_file_id
    out_path = frames_dir / f"{ts:.3f}.jpg"
    # FastAPI injects Query defaults; unit tests may call this function directly.
    variant_raw = variant if isinstance(variant, str) else None
    want_hdr = (variant_raw or "").strip().lower() == "hdr"
    filename_raw = filename if isinstance(filename, str) else None
    ar_raw = ar if isinstance(ar, str) else None

    def _respond(path: Path) -> FileResponse:
        serve = path
        if want_hdr:
            from app.video.frame_enhance import ensure_hdr_variant, hdr_variant_path

            hdr_path = hdr_variant_path(str(settings.thumbnail_dir), drive_file_id, ts)
            # Prefer an already-built derivative. In interactive (non-cache_only)
            # mode we may materialize once; cache_only never invents bytes.
            if hdr_path.is_file():
                serve = hdr_path
            elif not cache_only:
                built = ensure_hdr_variant(path, hdr_path)
                if built is not None:
                    serve = built
            elif cache_only and not hdr_path.is_file():
                raise HTTPException(status_code=404, detail="HDR frame not available")
        if ar_raw == "4x5":
            if want_hdr:
                crop_root = frames_dir / "hdr" / "4x5"
            else:
                crop_root = frames_dir / "4x5"
            serve = _ensure_portrait_crop(serve, crop_root / Path(serve).name)
        safe = (filename_raw or f"{drive_file_id}_{ts:.3f}.jpg").replace('"', "").replace("\n", "")
        if not safe.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            safe = f"{safe}.jpg"
        headers = {}
        if download:
            headers["Content-Disposition"] = f'attachment; filename="{safe}"'
        return FileResponse(serve, media_type="image/jpeg", headers=headers or None)

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

    # 4. On-demand extraction (local cache first; live Drive stream last).
    # Coalesce so Close / a second click on the same timestamp reuses one ffmpeg job.
    frames_dir.mkdir(parents=True, exist_ok=True)
    ok = await ensure_frame_extracted(drive_file_id, ts, out_path, settings, session)
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


_EXTRACT_JOBS: dict[str, asyncio.Future[bool]] = {}
_EXTRACT_JOBS_LOCK: asyncio.Lock | None = None
_EXTRACT_SEM = asyncio.Semaphore(2)


def _extract_job_key(drive_file_id: str, ts: float) -> str:
    return f"{drive_file_id}:{round(float(ts), 3):.3f}"


def _extract_jobs_lock() -> asyncio.Lock:
    global _EXTRACT_JOBS_LOCK
    if _EXTRACT_JOBS_LOCK is None:
        _EXTRACT_JOBS_LOCK = asyncio.Lock()
    return _EXTRACT_JOBS_LOCK


async def ensure_frame_extracted(
    drive_file_id: str,
    ts: float,
    out_path: Path,
    settings,
    _session: AsyncSession,
) -> bool:
    """One in-flight ffmpeg per (video, timestamp). Closing the client does not cancel it."""
    if out_path.is_file():
        return True
    key = _extract_job_key(drive_file_id, ts)
    lock = _extract_jobs_lock()
    async with lock:
        fut = _EXTRACT_JOBS.get(key)
        if fut is None:
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            _EXTRACT_JOBS[key] = fut

            async def _run() -> None:
                try:
                    async with _EXTRACT_SEM:
                        if out_path.is_file():
                            ok = True
                        else:
                            factory = get_session_factory()
                            async with factory() as own:
                                ok = await _extract_frame_on_demand(
                                    drive_file_id, ts, out_path, settings, own
                                )
                    if not fut.done():
                        fut.set_result(bool(ok))
                except Exception as exc:  # noqa: BLE001
                    if not fut.done():
                        fut.set_exception(exc)
                finally:
                    _EXTRACT_JOBS.pop(key, None)

            asyncio.create_task(_run())
    return await asyncio.shield(fut)


def schedule_frame_extract(drive_file_id: str, ts: float) -> None:
    """Kick on-demand extract without blocking the list/API caller."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_bg_frame_extract(drive_file_id, ts))


async def _bg_frame_extract(drive_file_id: str, ts: float) -> None:
    settings = get_settings()
    frames_dir = Path(settings.thumbnail_dir) / "video" / drive_file_id
    out_path = frames_dir / f"{float(ts):.3f}.jpg"
    if out_path.is_file():
        return
    frames_dir.mkdir(parents=True, exist_ok=True)
    try:
        factory = get_session_factory()
        async with factory() as session:
            await ensure_frame_extracted(drive_file_id, ts, out_path, settings, session)
    except Exception as exc:  # noqa: BLE001
        logger.debug("background frame extract %s@%.3f: %s", drive_file_id, ts, exc)


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
            cmd = ffmpeg_carousel_still_cmd(str(src), ts, str(out_path))

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
            creds = carousel_google(settings)
            new_token, new_expiry = await asyncio.to_thread(
                _do_token_refresh,
                user.refresh_token,
                creds.client_id,
                creds.client_secret,
            )
            user.access_token = new_token
            user.token_expiry = new_expiry
            await session.commit()
        except Exception as exc:
            logger.warning("Frame on-demand: token refresh failed: %s", exc)
            return False

    access_token = user.access_token
    drive_url = f"https://www.googleapis.com/drive/v3/files/{drive_file_id}?alt=media"

    cmd = ffmpeg_carousel_still_cmd(
        drive_url,
        ts,
        str(out_path),
        extra_input_args=["-headers", f"Authorization: Bearer {access_token}\r\n"],
    )

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
