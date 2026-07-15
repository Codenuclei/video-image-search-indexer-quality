from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile, Media, MediaType, VideoSegment
from app.video.youtube_transcript import fetch_youtube_captions, youtube_id_from_filename

logger = logging.getLogger(__name__)


async def ingest_youtube_transcript_for_drive_file(
    session: AsyncSession,
    drive_file: DriveFile,
    *,
    force: bool = False,
) -> int:
    """
    Fetch YouTube captions for a Drive video and store as VideoSegment rows.

    Non-destructive: skips when transcript text already exists unless force=True.
    Does not clear faces, frames, or other media data.
    """
    yt_id = youtube_id_from_filename(drive_file.name)
    if not yt_id:
        return 0

    cues = await fetch_youtube_captions(yt_id)
    if not cues:
        return 0

    media = (
        await session.execute(select(Media).where(Media.drive_file_id == drive_file.id))
    ).scalar_one_or_none()
    if media is None:
        media = Media(drive_file_id=drive_file.id, type=MediaType.VIDEO)
        session.add(media)
        await session.flush()

    existing_text_segments = (
        await session.execute(
            select(func.count())
            .select_from(VideoSegment)
            .where(VideoSegment.media_id == media.id, VideoSegment.text != "")
        )
    ).scalar_one()

    if existing_text_segments > 0 and not force:
        logger.info(
            "YouTube transcript skip %s: %d segment(s) already have text",
            drive_file.name,
            existing_text_segments,
        )
        return 0

    if force and existing_text_segments > 0:
        rows = (
            await session.execute(select(VideoSegment).where(VideoSegment.media_id == media.id))
        ).scalars().all()
        for seg in rows:
            if seg.text:
                await session.delete(seg)
        await session.flush()

    for cue in cues:
        session.add(
            VideoSegment(
                media_id=media.id,
                start_sec=cue.start_sec,
                end_sec=cue.end_sec,
                text=cue.text,
            )
        )
    await session.flush()
    logger.info("YouTube transcript ingested %d cue(s) for %s", len(cues), drive_file.name)
    return len(cues)
