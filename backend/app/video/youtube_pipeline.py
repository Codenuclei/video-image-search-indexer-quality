from __future__ import annotations

import logging
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile, DriveFileStatus, Media, MediaType, VideoSegment
from app.video.transcript_ingest import ingest_youtube_transcript_for_drive_file
from app.video.youtube_registry import (
    fetch_youtube_metadata,
    is_youtube_source,
    youtube_id_from_drive_file,
)
from app.video.youtube_transcript import fetch_youtube_captions

logger = logging.getLogger(__name__)


async def process_youtube_video_file(
    session: AsyncSession,
    drive_file: DriveFile,
) -> int:
    """
    Transcript-first pipeline for dashboard-fed YouTube videos (no Drive download).

    Stores caption cues as VideoSegment rows and marks the file processed.
    """
    if not is_youtube_source(drive_file):
        raise ValueError(f"Not a YouTube source file: {drive_file.id}")

    yt_id = youtube_id_from_drive_file(drive_file)
    if not yt_id:
        raise ValueError(f"Could not resolve YouTube id for {drive_file.id}")

    meta = await fetch_youtube_metadata(yt_id)
    if meta.title and meta.title not in drive_file.name:
        drive_file.name = f"{meta.title} [{yt_id}]"
    drive_file.path = drive_file.path or f"/youtube/{yt_id}"

    cues = await fetch_youtube_captions(yt_id)
    if not cues:
        raise ValueError(f"No captions available for YouTube video {yt_id}")

    ingested = await ingest_youtube_transcript_for_drive_file(
        session,
        drive_file,
        force=True,
    )
    if ingested == 0:
        segment_count = (
            await session.execute(
                select(VideoSegment)
                .join(Media, VideoSegment.media_id == Media.id)
                .where(Media.drive_file_id == drive_file.id, VideoSegment.text != "")
            )
        ).scalars().all()
        ingested = len(segment_count)

    media = (
        await session.execute(select(Media).where(Media.drive_file_id == drive_file.id))
    ).scalar_one_or_none()
    if media is None:
        media = Media(
            drive_file_id=drive_file.id,
            type=MediaType.VIDEO,
            duration_seconds=float(meta.duration_seconds) if meta.duration_seconds else None,
        )
        session.add(media)
    elif meta.duration_seconds and not media.duration_seconds:
        media.duration_seconds = float(meta.duration_seconds)

    await session.flush()
    logger.info(
        "YouTube pipeline complete for %s (%s): %d cue(s)",
        drive_file.name,
        yt_id,
        ingested,
    )
    return ingested
