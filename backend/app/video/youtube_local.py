from __future__ import annotations

import asyncio
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import DriveFile, DriveFileStatus
from app.video.youtube_cache import video_cache_path
from app.video.youtube_download import download_youtube_video_sync
from app.video.youtube_registry import fetch_youtube_metadata, youtube_drive_id

logger = logging.getLogger(__name__)


async def ensure_youtube_video_local(
    session: AsyncSession,
    video_id: str,
) -> tuple[DriveFile, bool]:
    """
    Download a YouTube video to the shared Railway volume (video_cache_dir).

    Returns (drive_file, downloaded_now). File is kept on disk for team search/playback.
    """
    settings = get_settings()
    meta = await fetch_youtube_metadata(video_id)
    drive_id = youtube_drive_id(video_id)

    drive_file = await session.get(DriveFile, drive_id)
    if drive_file is None:
        drive_file = DriveFile(
            id=drive_id,
            name=f"{meta.title} [{video_id}]",
            mime_type="video/webm",
            path=f"/youtube/local/{video_id}",
            modified_time=datetime.now(timezone.utc),
            status=DriveFileStatus.PENDING,
            source="youtube",
        )
        session.add(drive_file)
        await session.flush()

    dest = video_cache_path(settings, drive_file)
    if dest.is_file() and dest.stat().st_size > 0:
        drive_file.size = dest.stat().st_size
        drive_file.status = DriveFileStatus.PENDING
        drive_file.error_message = None
        await session.flush()
        logger.info("YouTube local cache hit: %s", dest)
        return drive_file, False

    local_path, filename = await asyncio.to_thread(
        download_youtube_video_sync,
        video_id,
        title=meta.title,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(local_path, dest)
    try:
        parent = Path(local_path).parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass

    drive_file.name = filename if filename else drive_file.name
    drive_file.mime_type = "video/webm" if dest.suffix == ".webm" else "video/mp4"
    drive_file.path = f"/youtube/local/{video_id}"
    drive_file.source = "youtube"
    drive_file.size = dest.stat().st_size
    drive_file.status = DriveFileStatus.PENDING
    drive_file.error_message = None
    await session.flush()
    logger.info(
        "YouTube %s saved to shared volume: %s (%.1f MB)",
        video_id,
        dest,
        drive_file.size / (1024 * 1024),
    )
    return drive_file, True
