from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile, DriveFileStatus
from app.db.session import get_db, get_session_factory
from app.dependencies import get_indexing_worker
from app.runtime_settings import get_runtime_settings
from app.schemas import IndexRunResult, IndexStatus
from app.workers.indexer import IndexingWorker
from app.workers.maintenance import (
    count_missing_captions,
    count_missing_embeddings,
    maintenance_status,
    run_caption_backfill,
    run_embedding_backfill,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["index"])


async def _run_cycle(worker: IndexingWorker) -> None:
    await worker.run_cycle()


async def _backfill_image_embeddings(worker: IndexingWorker) -> None:
    await run_embedding_backfill(worker)


@router.post("/index", response_model=IndexStatus)
async def trigger_index(
    background_tasks: BackgroundTasks,
    worker: IndexingWorker = Depends(get_indexing_worker),
    session: AsyncSession = Depends(get_db),
) -> IndexStatus:
    """Triggers an indexing run (sync file list + process pending files) in the background."""
    if worker.is_running:
        raise HTTPException(status_code=409, detail="An indexing run is already in progress")
    background_tasks.add_task(_run_cycle, worker)
    return await _build_index_status(worker, session)


@router.post("/reindex", response_model=IndexStatus)
async def trigger_reindex(
    background_tasks: BackgroundTasks,
    worker: IndexingWorker = Depends(get_indexing_worker),
    session: AsyncSession = Depends(get_db),
) -> IndexStatus:
    """Re-upload indexed files to Gemini (keeps existing face tags)."""
    if worker.is_running:
        raise HTTPException(status_code=409, detail="An indexing run is already in progress")
    await session.execute(
        DriveFile.__table__.update()
        .where(DriveFile.status.in_([DriveFileStatus.PROCESSED, DriveFileStatus.ERROR]))
        .values(status=DriveFileStatus.PENDING, error_message=None, gemini_document_name=None)
    )
    await session.commit()
    background_tasks.add_task(_run_cycle, worker)
    return await _build_index_status(worker, session)


@router.post("/admin/purge-file-search")
async def purge_file_search(
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Delete the Gemini File Search store to reclaim its 10GB quota.

    Images are now searched via Qdrant, so the store only needs PDFs/docs.
    Clears gemini_document_name and requeues ERROR files so docs re-upload.
    """
    from app.gemini.service import get_gemini_service

    gemini = get_gemini_service()
    result = await asyncio.to_thread(gemini.purge_store)
    await session.execute(
        DriveFile.__table__.update().values(gemini_document_name=None)
    )
    await session.execute(
        DriveFile.__table__.update()
        .where(DriveFile.status == DriveFileStatus.ERROR)
        .values(status=DriveFileStatus.PENDING, error_message=None)
    )
    await session.commit()
    return {"ok": True, **result}


@router.post("/backfill/image-embeddings")
async def backfill_image_embeddings(
    background_tasks: BackgroundTasks,
    worker: IndexingWorker = Depends(get_indexing_worker),
) -> dict[str, bool | str]:
    """Backfill Qdrant image embeddings for already-processed images (non-destructive)."""
    background_tasks.add_task(_backfill_image_embeddings, worker)
    return {"ok": True, "scheduled": True}


async def _backfill_image_captions(worker: IndexingWorker) -> None:
    await run_caption_backfill(worker)


@router.post("/backfill/image-captions")
async def backfill_image_captions(
    background_tasks: BackgroundTasks,
    worker: IndexingWorker = Depends(get_indexing_worker),
) -> dict[str, bool | str]:
    """Batch-caption processed images (Gemini Flash, N per call) for fast search-time precision."""
    background_tasks.add_task(_backfill_image_captions, worker)
    return {"ok": True, "scheduled": True}


async def _backfill_youtube_transcripts() -> None:
    """Fetch YouTube captions for Drive videos with [videoId] in the filename."""
    from app.db.models import Media, VideoSegment
    from app.video.transcript_ingest import ingest_youtube_transcript_for_drive_file
    from app.video.youtube_transcript import youtube_id_from_filename

    session_factory = get_session_factory()
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(DriveFile).where(DriveFile.mime_type.like("video/%"))
            )
        ).scalars().all()

    candidates = [df for df in rows if youtube_id_from_filename(df.name)]
    if not candidates:
        logger.info("YouTube transcript backfill: no video files with [videoId] in name")
        return

    ingested = 0
    skipped = 0
    for drive_file in candidates:
        async with session_factory() as session:
            df = await session.get(DriveFile, drive_file.id)
            if df is None:
                continue
            n = await ingest_youtube_transcript_for_drive_file(session, df)
            await session.commit()
            if n:
                ingested += n
            else:
                skipped += 1

    async with session_factory() as session:
        video_count = (
            await session.execute(
                select(func.count(func.distinct(Media.drive_file_id))).select_from(VideoSegment).join(
                    Media, VideoSegment.media_id == Media.id
                ).where(VideoSegment.text != "")
            )
        ).scalar_one()

    logger.info(
        "YouTube transcript backfill done: %d video(s), %d cue(s) ingested, %d skipped, %d with text",
        len(candidates),
        ingested,
        skipped,
        video_count,
    )


@router.post("/backfill/youtube-transcripts")
async def backfill_youtube_transcripts(
    background_tasks: BackgroundTasks,
) -> dict[str, bool | str]:
    """Fetch public YouTube captions for indexed Drive videos (non-destructive addon)."""
    background_tasks.add_task(_backfill_youtube_transcripts)
    return {"ok": True, "scheduled": True}


@router.get("/index/caption-audit")
async def caption_audit(
    session: AsyncSession = Depends(get_db),
    filter: str = "all",
    limit: int = 200,
    offset: int = 0,
) -> dict[str, object]:
    """List processed images with stored caption text for debugging gaps."""
    from app.qdrant.image_captions import (
        caption_quality_stats_sync,
        caption_word_count,
        existing_caption_ids_sync,
        get_captions_by_ids_sync,
        is_valid_caption,
    )

    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    mode = (filter or "all").strip().lower()
    if mode not in ("all", "missing", "invalid", "valid"):
        raise HTTPException(status_code=400, detail="filter must be all, missing, invalid, or valid")

    rows = list(
        (
            await session.execute(
                select(DriveFile)
                .where(
                    DriveFile.status == DriveFileStatus.PROCESSED,
                    DriveFile.mime_type.like("image/%"),
                )
                .order_by(DriveFile.path)
                .offset(offset)
                .limit(limit)
            )
        ).scalars().all()
    )
    ids = [df.id for df in rows]
    existing = await asyncio.to_thread(existing_caption_ids_sync, ids)
    captions = await asyncio.to_thread(get_captions_by_ids_sync, ids)

    items: list[dict[str, object]] = []
    for df in rows:
        cap = (captions.get(df.id) or "").strip()
        has_point = df.id in existing
        valid = is_valid_caption(cap)
        if not has_point:
            status = "missing"
        elif valid:
            status = "valid"
        else:
            status = "invalid"
        if mode != "all" and status != mode:
            continue
        items.append(
            {
                "drive_file_id": df.id,
                "name": df.name,
                "path": df.path,
                "caption": cap or None,
                "word_count": caption_word_count(cap),
                "valid": valid,
                "has_qdrant_point": has_point,
                "status": status,
            }
        )

    all_image_rows = (
        await session.execute(
            select(DriveFile.id).where(
                DriveFile.status == DriveFileStatus.PROCESSED,
                DriveFile.mime_type.like("image/%"),
            )
        )
    ).all()
    all_ids = [r[0] for r in all_image_rows]
    quality = await asyncio.to_thread(caption_quality_stats_sync, all_ids)

    return {
        "filter": mode,
        "offset": offset,
        "limit": limit,
        "returned": len(items),
        "summary": quality,
        "items": items,
    }


@router.get("/index/captions")
async def caption_stats(session: AsyncSession = Depends(get_db)) -> dict[str, object]:
    """How many images are captioned vs embedded vs processed."""
    from app.qdrant.image_captions import collection_info_sync
    from app.qdrant.images import collection_info_sync as image_collection_info_sync

    processed_images = (
        await session.execute(
            select(func.count()).select_from(DriveFile).where(
                DriveFile.status == DriveFileStatus.PROCESSED,
                DriveFile.mime_type.like("image/%"),
            )
        )
    ).scalar_one()

    captions_info = await asyncio.to_thread(collection_info_sync)
    images_info = await asyncio.to_thread(image_collection_info_sync)
    qdrant_points = int(captions_info.get("points") or 0)
    embedded = int(images_info.get("points") or 0)

    from app.qdrant.image_captions import caption_quality_stats_sync

    image_rows = (
        await session.execute(
            select(DriveFile.id).where(
                DriveFile.status == DriveFileStatus.PROCESSED,
                DriveFile.mime_type.like("image/%"),
            )
        )
    ).all()
    image_ids = [r[0] for r in image_rows]
    quality = await asyncio.to_thread(caption_quality_stats_sync, image_ids)
    captioned = int(quality["valid"])
    invalid = int(quality["invalid"])

    return {
        "processed_images": processed_images,
        "visual_embeddings": embedded,
        "captioned": captioned,
        "valid_captions": captioned,
        "invalid_captions": invalid,
        "qdrant_caption_points": qdrant_points,
        "remaining": max(0, processed_images - captioned),
        "pct_captioned": round(100.0 * captioned / processed_images, 1) if processed_images else 0.0,
        "missing_captions": await count_missing_captions(),
        "missing_embeddings": await count_missing_embeddings(),
        "maintenance": maintenance_status(),
        "collections": {
            "images": images_info.get("collection"),
            "captions": captions_info.get("collection"),
        },
    }


@router.get("/index", response_model=IndexStatus)
async def index_status(
    worker: IndexingWorker = Depends(get_indexing_worker),
    session: AsyncSession = Depends(get_db),
) -> IndexStatus:
    return await _build_index_status(worker, session)


async def _build_index_status(worker: IndexingWorker, session: AsyncSession) -> IndexStatus:
    stmt = select(DriveFile.status, func.count()).group_by(DriveFile.status)
    rows = (await session.execute(stmt)).all()
    counts = {status.value: count for status, count in rows}

    processing = (
        await session.execute(
            select(DriveFile.name).where(DriveFile.status == DriveFileStatus.PROCESSING).limit(1)
        )
    ).scalar_one_or_none()

    pending_count = (
        await session.execute(
            select(func.count()).select_from(DriveFile).where(DriveFile.status == DriveFileStatus.PENDING)
        )
    ).scalar_one()

    last_run = IndexRunResult(**worker.last_run_summary) if worker.last_run_summary else None
    runtime = get_runtime_settings()

    return IndexStatus(
        is_running=worker.is_running,
        counts_by_status=counts,
        last_run=last_run,
        last_run_at=worker.last_run_at,
        current_file=processing,
        auto_index_enabled=runtime.auto_index_enabled,
        auto_index_interval_seconds=runtime.auto_index_interval_seconds,
        pending_count=pending_count,
    )
