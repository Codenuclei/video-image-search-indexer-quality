from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.app_settings_store import refresh_runtime_settings_from_db
from app.db.models import DriveFile, DriveFileStatus
from app.db.session import get_db, get_session_factory
from app.dependencies import get_indexing_worker
from app.runtime_settings import get_runtime_settings
from app.schemas import (
    GoIndexerClaimItem,
    GoIndexerClaimResponse,
    GoIndexerReportIn,
    GoIndexerStatusOut,
    IndexLaneSlots,
    IndexRunResult,
    IndexStatus,
    ResolveConflictIn,
    RetryErroredByBucketIn,
    RetrySkippedByReasonIn,
)
from app.workers.indexer import IndexingWorker
from app.workers.go_indexer_state import (
    get_go_indexer_state,
    go_claimed_ids,
    go_heartbeat,
    go_is_alive,
    go_report_stats,
    go_stale_claims,
    go_track_claim,
    go_untrack,
)
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
    from app.config import get_settings
    from app.workers.maintenance import _recover_status_from_qdrant

    await _recover_status_from_qdrant(force=True)

    if not get_settings().run_indexer:
        # API replica: discover new Drive files only; indexer service drains PENDING.
        try:
            seen = await worker.sync_file_list(cache_source="manual_api")
            logger.info("Index trigger on API (RUN_INDEXER=false): synced %d file(s)", seen)
        except Exception:  # noqa: BLE001
            logger.exception("API-only Drive sync failed")
        return
    await worker.run_cycle()


async def _backfill_image_embeddings(worker: IndexingWorker) -> None:
    from app.workers.maintenance import _recover_status_from_qdrant

    await _recover_status_from_qdrant(force=True)
    await run_embedding_backfill(worker)


@router.post("/index", response_model=IndexStatus)
async def trigger_index(
    background_tasks: BackgroundTasks,
    worker: IndexingWorker = Depends(get_indexing_worker),
    session: AsyncSession = Depends(get_db),
) -> IndexStatus:
    """Triggers Drive sync (+ index cycle when RUN_INDEXER=true) in the background."""
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
    """Backfill missing index artifacts only — never wipe PROCESSED media/faces.

    Queues ERROR rows and PROCESSED/ERROR images that have no Media row.
    Repairs duplicate-only groups by requeuing one copy as the canonical owner.
    Does not bulk-demote PROCESSED → PENDING (permanent library).
    """
    if worker.is_running:
        raise HTTPException(status_code=409, detail="An indexing run is already in progress")

    from app.db.models import Media
    from sqlalchemy import exists

    # Re-queue hard errors for another attempt.
    await session.execute(
        DriveFile.__table__.update()
        .where(DriveFile.status == DriveFileStatus.ERROR)
        .values(status=DriveFileStatus.PENDING, error_message=None)
    )
    # PROCESSED without media = incomplete first index; allow face/embed add-if-missing.
    no_media = ~exists(select(Media.id).where(Media.drive_file_id == DriveFile.id))
    await session.execute(
        DriveFile.__table__.update()
        .where(
            DriveFile.status == DriveFileStatus.PROCESSED,
            DriveFile.mime_type.like("image/%"),
            no_media,
        )
        .values(status=DriveFileStatus.PENDING, error_message=None)
    )
    from app.drive.conflicts import requeue_circular_duplicate_canonicals

    repaired_duplicate_ids = await requeue_circular_duplicate_canonicals(session)
    if repaired_duplicate_ids:
        logger.info(
            "Reindex backfill queued %d duplicate-only canonical image(s)",
            len(repaired_duplicate_ids),
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


@router.get("/admin/cache-cleanup")
async def cache_cleanup_dry_run() -> dict[str, object]:
    """Dry-run: count Drive media_cache/videos files safe to delete (PROCESSED + Media)."""
    from app.drive.cache_cleanup import run_cache_cleanup

    return await run_cache_cleanup(apply=False)


@router.post("/admin/cache-cleanup")
async def cache_cleanup_apply(apply: bool = Query(False)) -> dict[str, object]:
    """Apply safe Drive cache cleanup. Pass apply=true; default is dry-run."""
    from app.drive.cache_cleanup import run_cache_cleanup

    return await run_cache_cleanup(apply=apply)


@router.post("/backfill/image-embeddings")
async def backfill_image_embeddings(
    background_tasks: BackgroundTasks,
    worker: IndexingWorker = Depends(get_indexing_worker),
) -> dict[str, bool | str]:
    """Backfill Qdrant image embeddings for already-processed images (non-destructive)."""
    background_tasks.add_task(_backfill_image_embeddings, worker)
    return {"ok": True, "scheduled": True}


async def _backfill_image_captions(worker: IndexingWorker) -> None:
    from app.workers.maintenance import _recover_status_from_qdrant

    await _recover_status_from_qdrant(force=True)
    await run_caption_backfill(worker)


@router.post("/backfill/image-captions")
async def backfill_image_captions(
    background_tasks: BackgroundTasks,
    worker: IndexingWorker = Depends(get_indexing_worker),
) -> dict[str, bool | str]:
    """Batch-caption processed images (Gemini Flash, N per call) for fast search-time precision."""
    background_tasks.add_task(_backfill_image_captions, worker)
    return {"ok": True, "scheduled": True}


@router.get("/objects/status")
async def object_status(
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    from app.workers.object_queue import object_queue_status, produce_object_backfill

    status = await object_queue_status(session)
    estimate = await produce_object_backfill(session, limit=5000, dry_run=True)
    return {"ok": True, **status, "backfill_estimate": estimate}


@router.post("/objects/backfill")
async def object_backfill(
    dry_run: bool = Query(True),
    limit: int = Query(500, ge=1, le=5000),
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    from app.workers.object_queue import produce_object_backfill

    runtime = await refresh_runtime_settings_from_db(session)
    if not dry_run and not runtime.object_backfill_enabled:
        raise HTTPException(
            status_code=409,
            detail="Enable object_backfill_enabled before enqueueing historical media",
        )
    result = await produce_object_backfill(session, limit=limit, dry_run=dry_run)
    return {"ok": True, "dry_run": dry_run, **result}


@router.post("/objects/requeue")
async def object_requeue(
    include_done: bool = Query(False),
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    from app.workers.object_queue import requeue_object_jobs

    count = await requeue_object_jobs(session, include_done=include_done)
    return {"ok": True, "requeued": count, "include_done": include_done}


@router.post("/index/recover-from-qdrant")
async def recover_from_qdrant_endpoint(
    session: AsyncSession = Depends(get_db),
    dry_run: bool = True,
    create_orphaned_stubs: bool = False,
) -> dict[str, object]:
    """Append-only: mark drive_files PROCESSED + ensure media when Qdrant already has vectors.

    Never deletes Qdrant points or Postgres rows. Never calls Gemini.
    Captions cannot be recovered when dfi_image_captions is empty.
    Orphaned points (no PG row) are reported; stubs need create_orphaned_stubs=true
    (unsafe — payloads lack name/path/mime).
    """
    from app.qdrant.recover import recover_from_qdrant

    result = await recover_from_qdrant(
        session,
        dry_run=dry_run,
        create_orphaned_stubs=create_orphaned_stubs,
    )
    return {"ok": True, **result.to_dict()}


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


@router.get("/index/skip-stats")
async def skip_stats(session: AsyncSession = Depends(get_db)) -> dict[str, object]:
    """Aggregate skipped *media* by reason — folder markers and Apple junk excluded."""
    from app.drive.library_filters import sql_exclude_blocked_drive_files
    from app.workers.requeue_failed import normalize_skip_reason

    rows = (
        await session.execute(
            select(DriveFile.error_message, func.count())
            .where(
                DriveFile.status == DriveFileStatus.SKIPPED,
                sql_exclude_blocked_drive_files(DriveFile),
            )
            .group_by(DriveFile.error_message)
        )
    ).all()
    by_reason: dict[str, int] = {}
    total = 0
    for msg, count in rows:
        n = int(count)
        key = normalize_skip_reason(msg)
        # Never surface apple/folder markers or non-media mime skips (XML sidecars,
        # wav, docs) — those are not actionable; only real media skips (name_conflict,
        # decode, …) belong in Admin.
        if key in {"appledouble_junk", "folder_marker", "unsupported_mime"}:
            continue
        total += n
        by_reason[key] = by_reason.get(key, 0) + n
    ranked = sorted(
        [{"reason": k, "count": v} for k, v in by_reason.items()],
        key=lambda x: -x["count"],
    )
    return {"total_skipped": total, "by_reason": ranked}


@router.get("/index/tat-stats")
async def tat_stats(session: AsyncSession = Depends(get_db)) -> dict[str, object]:
    """Index turnaround (claim → done: captioned image / indexed video / ERROR)."""
    from app.workers.index_tat import build_index_tat_stats, empty_kind_bucket

    try:
        return await build_index_tat_stats(session)
    except Exception:  # noqa: BLE001 — never break Admin over empty/old schema
        logger.exception("index_tat_stats_failed")
        return {
            "image": empty_kind_bucket(),
            "video": empty_kind_bucket(),
            "sample_window": "unavailable",
            "metric": "claim_to_done",
            "done_means": "unavailable",
        }


@router.post("/index/tat-stats/reset")
async def reset_tat_stats(session: AsyncSession = Depends(get_db)) -> dict[str, object]:
    """Clear accumulated claim→done TAT samples (min/avg/max restart empty).

    Only NULLs processing_started_at / completed_at on finished rows. Status and
    search artifacts are untouched; new index completions refill the card.
    """
    from app.workers.index_tat import (
        build_index_tat_stats,
        empty_kind_bucket,
        reset_index_tat_samples,
    )

    try:
        result = await reset_index_tat_samples(session)
        await session.commit()
        stats = await build_index_tat_stats(session)
        return {**result, "stats": stats}
    except Exception:  # noqa: BLE001
        logger.exception("index_tat_stats_reset_failed")
        await session.rollback()
        return {
            "cleared": 0,
            "stats": {
                "image": empty_kind_bucket(),
                "video": empty_kind_bucket(),
                "sample_window": "unavailable",
                "metric": "claim_to_done",
                "done_means": "unavailable",
            },
        }


@router.get("/index/drive-stats")
async def index_drive_stats(session: AsyncSession = Depends(get_db)) -> dict[str, object]:
    """Drive indexing outcomes: totals + per-folder processed/error/skip with top reasons.

    Use this after (or during) a Drive index to see how many files are search-ready
    versus errored/skipped, broken down by root and parent folder.
    """
    from app.drive.index_stats import build_drive_index_stats

    return await build_drive_index_stats(session)


@router.get("/index/folders")
async def indexed_folders(session: AsyncSession = Depends(get_db)) -> dict[str, object]:
    """All previously indexed Drive folders with persistent Drive URLs."""
    from app.drive.indexed_folders import (
        ensure_indexed_folder_history,
        list_indexed_folders,
    )
    from app.schemas import IndexedFolderOut

    # Additive repair: promote selected folder + any drive_files.root_folder_id
    # into history. Never deletes completed index data.
    await ensure_indexed_folder_history(session)
    await session.commit()
    rows = await list_indexed_folders(session)
    return {
        "folders": [IndexedFolderOut.model_validate(r).model_dump(mode="json") for r in rows],
        "total": len(rows),
    }


@router.post("/index/backup/run")
async def run_backup_now() -> dict[str, object]:
    """Trigger a durable Postgres + Qdrant + carousel forever backup (leader-safe)."""
    from app.workers.backup import run_daily_backup

    return await run_daily_backup()


@router.get("/index/backup/restore-dry-run")
async def backup_restore_dry_run(day: str | None = None) -> dict[str, object]:
    """Verify latest (or named) backup exists without applying a restore."""
    from app.workers.backup import restore_dry_run

    return await restore_dry_run(day)


@router.get("/index/conflicts")
async def index_conflicts(
    status: str | None = "pending",
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Same-name / same-content conflicts awaiting (or past) user resolution."""
    from app.drive.conflicts import (
        KIND_SAME_CONTENT_DIFF_NAME,
        KIND_SAME_NAME_DIFF_CONTENT,
        STATUS_PENDING,
        list_conflicts,
    )
    from app.schemas import FileIndexConflictOut

    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    rows, total = await list_conflicts(session, status=status, limit=limit, offset=offset)

    items = []
    for r in rows:
        out = FileIndexConflictOut.model_validate(r)
        out.can_replace = (
            r.conflict_kind == KIND_SAME_NAME_DIFF_CONTENT and r.status == STATUS_PENDING
        )
        out.can_merge = r.conflict_kind in (
            KIND_SAME_CONTENT_DIFF_NAME,
            "same_content",
        ) and r.status == STATUS_PENDING
        out.can_skip = r.status == STATUS_PENDING
        items.append(out.model_dump(mode="json"))
    return {"total": total, "offset": offset, "limit": limit, "items": items}


@router.post("/index/conflicts/{conflict_id}/resolve")
async def resolve_index_conflict(
    conflict_id: int,
    body: ResolveConflictIn,
    background_tasks: BackgroundTasks,
    worker: IndexingWorker = Depends(get_indexing_worker),
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Resolve a conflict with skip | replace | merge."""
    from app.drive.conflicts import resolve_conflict
    from app.schemas import FileIndexConflictOut

    action = (body.action or "").strip().lower()
    if action not in ("skip", "replace", "merge"):
        raise HTTPException(status_code=400, detail="action must be skip, replace, or merge")
    try:
        row = await resolve_conflict(session, conflict_id, action=action)
        await session.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if action == "replace" and not worker.is_running:
        background_tasks.add_task(_run_cycle, worker)

    return {"ok": True, "conflict": FileIndexConflictOut.model_validate(row).model_dump(mode="json")}


@router.post("/index/skipped/retry")
async def retry_skipped_by_reason(
    body: RetrySkippedByReasonIn,
    background_tasks: BackgroundTasks,
    worker: IndexingWorker = Depends(get_indexing_worker),
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Re-queue (or resume) all media skipped for a given skip-stats reason key.

    ``indexing_paused`` resumes paused folders (Library resume path).
    ``unsupported_mime`` returns without requeueing.
    Other reasons clear the skip and set files to PENDING.
    """
    from app.workers.requeue_failed import requeue_skipped_by_reason as _retry

    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="reason is required")

    result = await _retry(session, reason)
    await session.commit()

    requeued = int(result.get("requeued") or 0)
    if requeued > 0 and not worker.is_running:
        background_tasks.add_task(_run_cycle, worker)

    return {"ok": True, **result}


@router.get("/index/errors")
async def index_errors(
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Paginated indexing failures (ERROR status) for admin retry UI."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    total = int(
        (
            await session.execute(
                select(func.count()).select_from(DriveFile).where(DriveFile.status == DriveFileStatus.ERROR)
            )
        ).scalar_one()
    )
    items = list(
        (
            await session.execute(
                select(DriveFile)
                .where(DriveFile.status == DriveFileStatus.ERROR)
                .order_by(DriveFile.last_synced_at.desc().nulls_last(), DriveFile.name)
                .offset(offset)
                .limit(limit)
            )
        ).scalars().all()
    )
    from app.schemas import DriveFileOut

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [DriveFileOut.model_validate(i) for i in items],
    }


@router.get("/index/error-stats")
async def index_error_stats(session: AsyncSession = Depends(get_db)) -> dict[str, object]:
    """Aggregate ERROR rows by normalize_error_bucket for selective retry UI."""
    from app.workers.requeue_failed import normalize_error_bucket

    rows = list(
        (
            await session.execute(
                select(DriveFile.error_message).where(DriveFile.status == DriveFileStatus.ERROR)
            )
        ).scalars().all()
    )
    by_bucket: dict[str, int] = {}
    for msg in rows:
        key = normalize_error_bucket(msg)
        by_bucket[key] = by_bucket.get(key, 0) + 1
    ranked = sorted(
        [{"bucket": k, "count": v} for k, v in by_bucket.items()],
        key=lambda item: (-int(item["count"]), str(item["bucket"])),
    )
    return {"total_errors": len(rows), "by_bucket": ranked}


@router.post("/index/errors/retry")
async def retry_errored_by_bucket(
    body: RetryErroredByBucketIn,
    background_tasks: BackgroundTasks,
    worker: IndexingWorker = Depends(get_indexing_worker),
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Re-queue ERROR rows for one bucket (never junk/dupe/structural buckets).

    For ``enospc``, confirm disk headroom first — requeueing without free space
    recreates the volume fill.
    """
    from app.workers.requeue_failed import requeue_errored_by_bucket as _retry

    bucket = (body.bucket or "").strip()
    if not bucket:
        raise HTTPException(status_code=400, detail="bucket is required")

    result = await _retry(session, bucket)
    await session.commit()

    requeued = int(result.get("requeued") or 0)
    if requeued > 0 and not worker.is_running:
        background_tasks.add_task(_run_cycle, worker)

    return {"ok": True, **result}


@router.get("/index", response_model=IndexStatus)
async def index_status(
    worker: IndexingWorker = Depends(get_indexing_worker),
    session: AsyncSession = Depends(get_db),
) -> IndexStatus:
    return await _build_index_status(worker, session)


@router.get("/index/events")
async def index_events(request: Request) -> StreamingResponse:
    """SSE: push when the cheap index revision changes (no client poll needed)."""

    async def event_gen():
        from app.index_status_events import cheap_index_revision

        yield "retry: 2000\n\n"
        last: str | None = None
        idle_ticks = 0
        factory = get_session_factory()
        while True:
            if await request.is_disconnected():
                break
            try:
                async with factory() as session:
                    rev, busy = await cheap_index_revision(session)
            except Exception:
                logger.exception("index/events revision check failed")
                yield ": error\n\n"
                await asyncio.sleep(5.0)
                continue
            if rev != last:
                last = rev
                idle_ticks = 0
                yield f"event: revision\ndata: {json.dumps({'revision': rev})}\n\n"
            else:
                idle_ticks += 1
                if idle_ticks % 12 == 0:
                    yield ": ping\n\n"
            # Busy → ~1s push latency; idle → 5s (still far below former 12s client poll).
            await asyncio.sleep(1.0 if busy else 5.0)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _build_index_status(worker: IndexingWorker, session: AsyncSession) -> IndexStatus:
    from app.drive.library_filters import sql_exclude_blocked_drive_files

    # Counts exclude folder markers + AppleDouble / .DS_Store junk entirely.
    stmt = (
        select(DriveFile.status, func.count())
        .where(sql_exclude_blocked_drive_files(DriveFile))
        .group_by(DriveFile.status)
    )
    rows = (await session.execute(stmt)).all()
    counts = {status.value: count for status, count in rows}

    processing_rows = list(
        (
            await session.execute(
                select(DriveFile.name, DriveFile.mime_type)
                .where(DriveFile.status == DriveFileStatus.PROCESSING)
                .order_by(DriveFile.last_synced_at.desc().nulls_last())
                .limit(24)
            )
        ).all()
    )

    from app.pipelines.common import is_image_mime, is_video_mime

    current_image_files = [
        name for name, mime in processing_rows if is_image_mime(mime or "", name or "")
    ][:12]
    current_video_files = [
        name for name, mime in processing_rows if is_video_mime(mime or "")
    ][:12]
    # Backward-compatible mixed list: images first, then videos.
    processing_names = current_image_files + current_video_files

    pending_count = (
        await session.execute(
            select(func.count())
            .select_from(DriveFile)
            .where(
                DriveFile.status == DriveFileStatus.PENDING,
                sql_exclude_blocked_drive_files(DriveFile),
            )
        )
    ).scalar_one()

    last_run = IndexRunResult(**worker.last_run_summary) if worker.last_run_summary else None
    # DB is authoritative across Gunicorn workers (PUT /settings hits one process only).
    runtime = await refresh_runtime_settings_from_db(session)
    from app.config import get_settings
    from app.workers.go_indexer_state import get_go_indexer_state, go_is_alive

    settings = get_settings()
    go_alive = go_is_alive(max_age_seconds=settings.go_indexer_heartbeat_seconds)
    go_stats = get_go_indexer_state().last_stats

    # Process-local task counts are accurate on the background leader; API-only
    # workers always see 0. Prefer DB PROCESSING so Active/is_running stay stable.
    db_processing = int(counts.get("processing", 0) or 0)
    image_active = worker.active_image_count
    video_active = worker.active_video_count
    if image_active + video_active == 0 and db_processing > 0:
        image_active = len(current_image_files)
        video_active = len(current_video_files)
        listed = image_active + video_active
        if listed == 0:
            image_active = db_processing
        elif listed < db_processing:
            image_active += db_processing - listed

    is_running = (
        worker.is_running
        or image_active > 0
        or video_active > 0
        or db_processing > 0
    )
    counts_part = ",".join(f"{k}:{counts[k]}" for k in sorted(counts.keys()))
    last_run_token = worker.last_run_at.isoformat() if worker.last_run_at else ""
    revision = f"{last_run_token}|{int(is_running)}|{pending_count}|{counts_part}"

    return IndexStatus(
        is_running=is_running,
        counts_by_status=counts,
        last_run=last_run,
        last_run_at=worker.last_run_at,
        current_file=processing_names[0] if processing_names else None,
        current_files=processing_names,
        current_image_files=current_image_files,
        current_video_files=current_video_files,
        image_slots=IndexLaneSlots(active=image_active, max=settings.image_index_max_parallel),
        video_slots=IndexLaneSlots(active=video_active, max=settings.video_index_max_parallel),
        active_image_jobs=image_active,
        active_video_jobs=video_active,
        auto_index_enabled=runtime.auto_index_enabled,
        auto_index_interval_seconds=runtime.auto_index_interval_seconds,
        pending_count=pending_count,
        go_indexer_enabled=runtime.go_indexer_enabled,
        go_indexer_alive=go_alive,
        go_files_per_sec=go_stats.files_per_sec if go_stats.reported_at else None,
        revision=revision,
    )


def _go_status_out() -> GoIndexerStatusOut:
    from app.config import get_settings

    settings = get_settings()
    runtime = get_runtime_settings()
    state = get_go_indexer_state()
    stats = state.last_stats
    return GoIndexerStatusOut(
        enabled=runtime.go_indexer_enabled,
        alive=go_is_alive(max_age_seconds=settings.go_indexer_heartbeat_seconds),
        last_heartbeat_at=state.last_heartbeat_at,
        max_parallel=settings.go_indexer_max_parallel,
        canary_limit=settings.go_indexer_canary_limit,
        claimed_open=len(go_claimed_ids()),
        last_files_ok=stats.files_ok,
        last_files_err=stats.files_err,
        last_elapsed_ms=stats.elapsed_ms,
        last_files_per_sec=stats.files_per_sec,
        last_download_bytes=stats.download_bytes,
        last_reported_at=stats.reported_at,
    )


@router.get("/index/go/status", response_model=GoIndexerStatusOut)
async def go_indexer_status() -> GoIndexerStatusOut:
    """Health / throughput check for the Go canary sidecar."""
    return _go_status_out()


@router.post("/index/go/heartbeat", response_model=GoIndexerStatusOut)
async def go_indexer_heartbeat() -> GoIndexerStatusOut:
    runtime = get_runtime_settings()
    if not runtime.go_indexer_enabled:
        raise HTTPException(status_code=403, detail="Go indexer toggle is off")
    go_heartbeat()
    return _go_status_out()


@router.post("/index/go/report", response_model=GoIndexerStatusOut)
async def go_indexer_report(body: GoIndexerReportIn) -> GoIndexerStatusOut:
    runtime = get_runtime_settings()
    if not runtime.go_indexer_enabled:
        raise HTTPException(status_code=403, detail="Go indexer toggle is off")
    go_report_stats(
        files_ok=body.files_ok,
        files_err=body.files_err,
        elapsed_ms=body.elapsed_ms,
        download_bytes=body.download_bytes,
    )
    return _go_status_out()


@router.post("/index/go/claim", response_model=GoIndexerClaimResponse)
async def go_indexer_claim(
    limit: int = 2,
    session: AsyncSession = Depends(get_db),
) -> GoIndexerClaimResponse:
    """Claim pending images for the Go canary (Python reserves parallel slots when Go is alive)."""
    from app.config import get_settings
    from app.drive.indexing_pause import is_file_indexing_paused, load_paused_folder_paths
    from app.pipelines.common import is_image_mime
    from app.pipelines.decode_recovery import decode_max_attempts
    from app.workers.claim_order import claim_window, pending_order_by

    settings = get_settings()
    runtime = get_runtime_settings()
    if not runtime.go_indexer_enabled:
        return GoIndexerClaimResponse(
            enabled=False,
            items=[],
            max_parallel=settings.go_indexer_max_parallel,
            canary_limit=settings.go_indexer_canary_limit,
        )

    go_heartbeat()
    await _release_stale_go_claims(session, settings.go_indexer_claim_stall_seconds)

    open_claims = len(go_claimed_ids())
    slots = max(0, settings.go_indexer_max_parallel - open_claims)
    want = max(0, min(limit, settings.go_indexer_canary_limit, slots))
    if want <= 0:
        return GoIndexerClaimResponse(
            enabled=True,
            items=[],
            max_parallel=settings.go_indexer_max_parallel,
            canary_limit=settings.go_indexer_canary_limit,
        )

    from app.pipelines.common import INDEXABLE_IMAGE_TYPES

    paused_paths = await load_paused_folder_paths(session)
    pending = list(
        (
            await session.execute(
                select(DriveFile)
                .where(
                    DriveFile.status == DriveFileStatus.PENDING,
                    or_(
                        DriveFile.mime_type.in_(tuple(INDEXABLE_IMAGE_TYPES)),
                        DriveFile.mime_type.like("image/%"),
                    ),
                )
                .order_by(*pending_order_by(settings))
                .limit(claim_window(settings, want))
            )
        ).scalars().all()
    )

    items: list[GoIndexerClaimItem] = []
    claimed_ids: list[str] = []
    for drive_file in pending:
        if len(claimed_ids) >= want:
            break
        if not is_image_mime(drive_file.mime_type, drive_file.name):
            continue
        if (drive_file.decode_attempts or 0) >= decode_max_attempts():
            continue
        if is_file_indexing_paused(drive_file.path, paused_paths):
            continue
        drive_file.status = DriveFileStatus.PROCESSING
        now = datetime.now(timezone.utc)
        drive_file.last_synced_at = now
        drive_file.processing_started_at = now
        drive_file.completed_at = None
        claimed_ids.append(drive_file.id)
        items.append(
            GoIndexerClaimItem(
                id=drive_file.id,
                name=drive_file.name,
                mime_type=drive_file.mime_type,
                size=drive_file.size,
                path=drive_file.path,
            )
        )

    if claimed_ids:
        await session.commit()
        go_track_claim(claimed_ids)
        logger.info("Go indexer claimed %d image(s)", len(claimed_ids))

    return GoIndexerClaimResponse(
        enabled=True,
        items=items,
        max_parallel=settings.go_indexer_max_parallel,
        canary_limit=settings.go_indexer_canary_limit,
    )


@router.post("/index/go/complete/{file_id}")
async def go_indexer_complete(
    file_id: str,
    worker: IndexingWorker = Depends(get_indexing_worker),
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """After Go downloads (optional), run the normal Python image index pipeline."""
    runtime = get_runtime_settings()
    if not runtime.go_indexer_enabled:
        raise HTTPException(status_code=403, detail="Go indexer toggle is off")

    drive_file = await session.get(DriveFile, file_id)
    if drive_file is None:
        raise HTTPException(status_code=404, detail="File not found")
    if drive_file.status not in (DriveFileStatus.PROCESSING, DriveFileStatus.PENDING):
        raise HTTPException(
            status_code=409,
            detail=f"File status is {drive_file.status.value}, expected processing",
        )
    if drive_file.status != DriveFileStatus.PROCESSING:
        drive_file.status = DriveFileStatus.PROCESSING
        await session.commit()

    go_heartbeat()
    try:
        await worker.index_claimed_image(file_id)
    finally:
        go_untrack(file_id)

    refreshed = await session.get(DriveFile, file_id)
    return {
        "ok": True,
        "file_id": file_id,
        "status": refreshed.status.value if refreshed else None,
    }


@router.post("/index/go/fail/{file_id}")
async def go_indexer_fail(
    file_id: str,
    session: AsyncSession = Depends(get_db),
    detail: str = "go_indexer_failed",
) -> dict[str, object]:
    runtime = get_runtime_settings()
    if not runtime.go_indexer_enabled:
        raise HTTPException(status_code=403, detail="Go indexer toggle is off")
    drive_file = await session.get(DriveFile, file_id)
    if drive_file is None:
        raise HTTPException(status_code=404, detail="File not found")
    drive_file.status = DriveFileStatus.ERROR
    drive_file.error_message = detail[:2000]
    await session.commit()
    go_untrack(file_id)
    return {"ok": True, "file_id": file_id, "status": "error"}


async def _release_stale_go_claims(session: AsyncSession, stall_seconds: float) -> int:
    stale = go_stale_claims(max_age_seconds=stall_seconds)
    if not stale:
        return 0
    released = 0
    for file_id in stale:
        drive_file = await session.get(DriveFile, file_id)
        if drive_file is not None and drive_file.status == DriveFileStatus.PROCESSING:
            drive_file.status = DriveFileStatus.PENDING
            drive_file.error_message = None
            released += 1
        go_untrack(file_id)
    if released:
        await session.commit()
        logger.warning("Released %d stale Go indexer claim(s)", released)
    return released
