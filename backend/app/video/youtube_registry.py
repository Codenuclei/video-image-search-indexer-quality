from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile, DriveFileStatus
from app.video.youtube_transcript import (
    _ANDROID_CONTEXT,
    _INNERTUBE_PLAYER_URL,
    _extract_api_key,
    _fetch_player_via_innertube,
    youtube_id_from_filename,
)

logger = logging.getLogger(__name__)

YOUTUBE_DRIVE_ID_PREFIX = "yt:"
_YT_URL_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)
_YT_ID_ONLY_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass(frozen=True)
class YoutubeMetadata:
    video_id: str
    title: str
    duration_seconds: int | None = None


def parse_youtube_video_id(value: str) -> str | None:
    """Parse a YouTube URL or bare 11-char id."""
    text = (value or "").strip()
    if not text:
        return None
    if _YT_ID_ONLY_RE.fullmatch(text):
        return text
    match = _YT_URL_RE.search(text)
    return match.group(1) if match else None


def youtube_drive_id(video_id: str) -> str:
    return f"{YOUTUBE_DRIVE_ID_PREFIX}{video_id}"


def is_youtube_source(drive_file: DriveFile) -> bool:
    source = getattr(drive_file, "source", None) or "drive"
    return source == "youtube" or drive_file.id.startswith(YOUTUBE_DRIVE_ID_PREFIX)


def youtube_id_from_drive_file(drive_file: DriveFile) -> str | None:
    if drive_file.id.startswith(YOUTUBE_DRIVE_ID_PREFIX):
        return drive_file.id[len(YOUTUBE_DRIVE_ID_PREFIX) :]
    return youtube_id_from_filename(drive_file.name)


def youtube_watch_url(video_id: str, timestamp_sec: float | None = None) -> str:
    url = f"https://www.youtube.com/watch?v={video_id}"
    if timestamp_sec is not None and timestamp_sec > 0:
        url += f"&t={int(timestamp_sec)}s"
    return url


async def fetch_youtube_metadata(video_id: str) -> YoutubeMetadata:
    embed_url = f"https://www.youtube.com/embed/{video_id}"
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        page = await client.get(embed_url)
        page.raise_for_status()
        api_key = _extract_api_key(page.text)
        if not api_key:
            return YoutubeMetadata(video_id=video_id, title=f"YouTube {video_id}")
        player = await _fetch_player_via_innertube(client, video_id, api_key)
        details = player.get("videoDetails") or {}
        title = (details.get("title") or f"YouTube {video_id}").strip()
        length_raw = details.get("lengthSeconds")
        duration = int(length_raw) if length_raw else None
        return YoutubeMetadata(video_id=video_id, title=title, duration_seconds=duration)


async def find_drive_file_for_youtube_id(
    session: AsyncSession,
    video_id: str,
) -> DriveFile | None:
    """Prefer an existing Drive-synced file whose name contains [videoId]."""
    pattern = f"%[{video_id}]%"
    return (
        await session.execute(
            select(DriveFile)
            .where(
                DriveFile.name.like(pattern),
                DriveFile.source != "youtube",
            )
            .order_by(DriveFile.modified_time.desc().nulls_last())
            .limit(1)
        )
    ).scalar_one_or_none()


async def register_youtube_video(
    session: AsyncSession,
    url_or_id: str,
    *,
    download_local: bool = True,
) -> tuple[DriveFile, bool, str]:
    """
    Register a YouTube video for indexing.

    Returns (drive_file, linked_to_drive, message).
    Uses existing company Drive file when already synced; otherwise stores on shared volume.
    """
    video_id = parse_youtube_video_id(url_or_id)
    if not video_id:
        raise ValueError(f"Invalid YouTube URL or id: {url_or_id!r}")

    linked = await find_drive_file_for_youtube_id(session, video_id)
    if linked is not None:
        linked.status = DriveFileStatus.PENDING
        linked.error_message = None
        await session.flush()
        return (
            linked,
            True,
            f"Already in company Drive folder — full index queued ({linked.name})",
        )

    meta = await fetch_youtube_metadata(video_id)
    drive_id = youtube_drive_id(video_id)
    existing = await session.get(DriveFile, drive_id)

    if download_local:
        message = f"Queued download to shared library: {meta.title}"
        status = DriveFileStatus.PROCESSING
    else:
        message = f"Added YouTube transcript-only: {meta.title}"
        status = DriveFileStatus.PENDING

    if existing is None:
        drive_file = DriveFile(
            id=drive_id,
            name=f"{meta.title} [{video_id}]",
            mime_type="video/youtube",
            path=f"/youtube/{video_id}",
            modified_time=datetime.now(timezone.utc),
            status=status,
            source="youtube",
        )
        session.add(drive_file)
        await session.flush()
        return (drive_file, False, message)

    existing.name = f"{meta.title} [{video_id}]"
    existing.mime_type = "video/youtube"
    existing.path = f"/youtube/{video_id}"
    existing.source = "youtube"
    existing.status = status
    existing.error_message = None
    await session.flush()
    return (existing, False, message)
