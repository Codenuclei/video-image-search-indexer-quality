from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile, DriveFileStatus
from app.db.session import get_db, get_session_factory
from app.dependencies import get_indexing_worker
from app.schemas import DriveFileOut
from app.video.youtube_registry import (
    parse_youtube_video_id,
    register_youtube_video,
    youtube_id_from_drive_file,
)
from app.workers.indexer import IndexingWorker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/youtube", tags=["youtube"])


class YoutubeVideosIn(BaseModel):
    urls: list[str] = Field(..., min_length=1, max_length=50)
    index_now: bool = True
    download_local: bool = True


class YoutubeRegisterResult(BaseModel):
    drive_file_id: str
    name: str
    youtube_video_id: str | None = None
    linked_to_drive: bool = False
    download_queued: bool = False
    message: str


class YoutubeRegisterResponse(BaseModel):
    ok: bool = True
    registered: list[YoutubeRegisterResult]
    index_scheduled: bool = False


async def _process_youtube_feed(
    video_ids: list[str],
    worker: IndexingWorker,
    *,
    download_local: bool,
) -> None:
    """Download missing videos to shared volume, ingest transcripts, run full video pipeline."""
    from app.video.transcript_ingest import ingest_youtube_transcript_for_drive_file
    from app.video.youtube_local import ensure_youtube_video_local
    from app.video.youtube_registry import find_drive_file_for_youtube_id, youtube_drive_id

    session_factory = get_session_factory()

    for video_id in video_ids:
        async with session_factory() as session:
            try:
                drive_file = await find_drive_file_for_youtube_id(session, video_id)
                downloaded = False

                if drive_file is None and download_local:
                    placeholder_id = youtube_drive_id(video_id)
                    placeholder = await session.get(DriveFile, placeholder_id)
                    if placeholder is not None:
                        placeholder.status = DriveFileStatus.PROCESSING
                        placeholder.error_message = None
                        await session.commit()

                    drive_file, downloaded = await ensure_youtube_video_local(session, video_id)
                    await session.commit()
                elif drive_file is None:
                    continue

                await ingest_youtube_transcript_for_drive_file(session, drive_file, force=True)
                drive_file.status = DriveFileStatus.PENDING
                drive_file.error_message = None
                await session.commit()
                logger.info(
                    "YouTube feed ready for pipeline: %s (local_download=%s)",
                    drive_file.name,
                    downloaded,
                )
            except Exception as exc:  # noqa: BLE001
                await session.rollback()
                async with session_factory() as err_session:
                    failed = await find_drive_file_for_youtube_id(err_session, video_id)
                    if failed is None:
                        failed = await err_session.get(DriveFile, youtube_drive_id(video_id))
                    if failed is not None:
                        failed.status = DriveFileStatus.ERROR
                        failed.error_message = str(exc)[:2000]
                        await err_session.commit()
                logger.exception("YouTube feed failed for %s", video_id)

    try:
        await worker.ensure_parallel_video_indexing()
    except Exception:  # noqa: BLE001
        logger.exception("Could not start parallel video indexing after YouTube feed")


@router.get("/videos", response_model=list[DriveFileOut])
async def list_youtube_videos(
    session: AsyncSession = Depends(get_db),
    limit: int = 100,
) -> list[DriveFile]:
    rows = (
        await session.execute(
            select(DriveFile)
            .where(DriveFile.source == "youtube")
            .order_by(DriveFile.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


@router.post("/videos", response_model=YoutubeRegisterResponse)
async def add_youtube_videos(
    body: YoutubeVideosIn,
    background_tasks: BackgroundTasks,
    worker: IndexingWorker = Depends(get_indexing_worker),
    session: AsyncSession = Depends(get_db),
) -> YoutubeRegisterResponse:
    """Feed YouTube URLs: download to shared volume if missing, then full index."""
    registered: list[YoutubeRegisterResult] = []
    video_ids: list[str] = []

    for raw in body.urls:
        value = raw.strip()
        if not value:
            continue
        try:
            drive_file, linked, message = await register_youtube_video(
                session,
                value,
                download_local=body.download_local,
            )
            yt_id = parse_youtube_video_id(value) or youtube_id_from_drive_file(drive_file)
            if yt_id:
                video_ids.append(yt_id)
            registered.append(
                YoutubeRegisterResult(
                    drive_file_id=drive_file.id,
                    name=drive_file.name,
                    youtube_video_id=yt_id,
                    linked_to_drive=linked,
                    download_queued=not linked and body.download_local,
                    message=message,
                )
            )
        except ValueError as exc:
            registered.append(
                YoutubeRegisterResult(
                    drive_file_id="",
                    name=value,
                    message=str(exc),
                )
            )
        except Exception as exc:  # noqa: BLE001
            registered.append(
                YoutubeRegisterResult(
                    drive_file_id="",
                    name=value,
                    message=str(exc)[:500],
                )
            )

    await session.commit()

    index_scheduled = False
    if body.index_now and video_ids:
        background_tasks.add_task(
            _process_youtube_feed,
            video_ids,
            worker,
            download_local=body.download_local,
        )
        index_scheduled = True

    return YoutubeRegisterResponse(
        registered=registered,
        index_scheduled=index_scheduled,
    )
