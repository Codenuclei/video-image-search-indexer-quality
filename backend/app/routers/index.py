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

logger = logging.getLogger(__name__)

router = APIRouter(tags=["index"])


async def _run_cycle(worker: IndexingWorker) -> None:
    await worker.run_cycle()


async def _backfill_image_embeddings(worker: IndexingWorker) -> None:
    """Embed already-processed images into Qdrant without re-detecting faces."""
    from app.pipelines.common import decode_image_bgr, download_to_memory
    from app.qdrant.images import existing_image_ids_sync
    from app.search.images import index_image_embedding

    import cv2

    session_factory = get_session_factory()
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(DriveFile.id).where(
                    DriveFile.status == DriveFileStatus.PROCESSED,
                    DriveFile.mime_type.like("image/%"),
                )
            )
        ).all()
    all_ids = [r[0] for r in rows]
    if not all_ids:
        logger.info("Backfill: no processed images found")
        return

    already = await asyncio.to_thread(existing_image_ids_sync, all_ids)
    todo = [fid for fid in all_ids if fid not in already]
    logger.info("Backfill: %d image(s) total, %d already embedded, %d to do",
                len(all_ids), len(already), len(todo))

    done = 0
    for fid in todo:
        try:
            raw = await download_to_memory(worker._client, fid)  # noqa: SLF001
            image_bgr = decode_image_bgr(raw)
            if image_bgr is None:
                continue
            ok, buf = cv2.imencode(".jpg", image_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            if not ok:
                continue
            await index_image_embedding(buf.tobytes(), fid)
            done += 1
            if done % 25 == 0:
                logger.info("Backfill progress: %d/%d", done, len(todo))
        except Exception:  # noqa: BLE001
            logger.exception("Backfill failed for image %s", fid)
    logger.info("Backfill complete: embedded %d image(s)", done)


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
