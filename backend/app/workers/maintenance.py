"""Background maintenance: caption/embedding backfill without manual triggers."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import cv2
from sqlalchemy import select

from app.config import get_settings
from app.concurrency.pools import effective_cpu_workers
from app.db.models import DriveFile, DriveFileStatus
from app.db.session import get_session_factory
from app.pipelines.async_cpu import run_cpu_bound
from app.pipelines.common import decode_image_bgr, download_to_memory
from app.drive.indexing_pause import (
    CORRUPT_SKIPPED_PREFIX,
    is_file_indexing_paused,
    load_paused_folder_paths,
)
from app.pipelines.decode_recovery import apply_decode_failure
from app.qdrant.image_captions import (
    delete_caption_sync,
    existing_caption_ids_sync,
    valid_caption_ids_sync,
)
from app.qdrant.images import existing_image_ids_sync
from app.search.images import index_image_captions_batch, index_image_embedding
from app.workers.indexer import IndexingWorker

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()
_caption_running = False
_embed_running = False
_last_caption_run_at: datetime | None = None
_last_embed_run_at: datetime | None = None
_last_caption_done = 0
_last_embed_done = 0
_last_invalid_captions_removed = 0


async def _processed_image_ids(*, exclude_paused: bool = True) -> list[str]:
    session_factory = get_session_factory()
    async with session_factory() as session:
        paused = await load_paused_folder_paths(session) if exclude_paused else []
        rows = (
            await session.execute(
                select(DriveFile.id, DriveFile.path).where(
                    DriveFile.status == DriveFileStatus.PROCESSED,
                    DriveFile.mime_type.like("image/%"),
                )
            )
        ).all()
    if not paused:
        return [r[0] for r in rows]
    return [fid for fid, path in rows if not is_file_indexing_paused(path, paused)]


async def caption_recaption_ids() -> tuple[list[str], list[str]]:
    """Return (missing_ids, invalid_ids) that need caption generation."""
    all_ids = await _processed_image_ids()
    if not all_ids:
        return [], []
    existing = await asyncio.to_thread(existing_caption_ids_sync, all_ids)
    valid = await asyncio.to_thread(valid_caption_ids_sync, list(existing))
    invalid = sorted(existing - valid)
    missing = [fid for fid in all_ids if fid not in existing]
    return missing, invalid


def maintenance_status() -> dict[str, object]:
    return {
        "caption_backfill_running": _caption_running,
        "embed_backfill_running": _embed_running,
        "last_caption_run_at": _last_caption_run_at.isoformat() if _last_caption_run_at else None,
        "last_embed_run_at": _last_embed_run_at.isoformat() if _last_embed_run_at else None,
        "last_caption_indexed": _last_caption_done,
        "last_embed_indexed": _last_embed_done,
        "last_invalid_captions_removed": _last_invalid_captions_removed,
    }


async def count_missing_captions() -> int:
    settings = get_settings()
    if not settings.image_caption_enabled or not settings.gemini_api_key:
        return 0
    missing, invalid = await caption_recaption_ids()
    return len(missing) + len(invalid)


async def count_missing_embeddings() -> int:
    settings = get_settings()
    if not settings.gemini_api_key:
        return 0
    all_ids = await _processed_image_ids()
    if not all_ids:
        return 0
    already = await asyncio.to_thread(existing_image_ids_sync, all_ids)
    return len(all_ids) - len(already)


async def run_caption_backfill(worker: IndexingWorker, *, max_batches: int | None = None) -> int:
    """Caption processed images missing captions or holding stub/invalid caption text."""
    global _caption_running, _last_caption_run_at, _last_caption_done, _last_invalid_captions_removed

    settings = get_settings()
    if not settings.image_caption_enabled or not settings.gemini_api_key:
        return 0

    if _caption_running:
        return 0

    async with _lock:
        if _caption_running:
            return 0
        _caption_running = True

    done = 0
    removed = 0
    try:
        missing, invalid = await caption_recaption_ids()
        todo = missing + invalid
        if not todo:
            return 0

        for fid in invalid:
            await asyncio.to_thread(delete_caption_sync, fid)
            removed += 1
        _last_invalid_captions_removed = removed
        if removed:
            logger.info("Caption backfill: removed %d invalid/stub caption(s)", removed)

        batch_size = settings.image_caption_batch_size
        batch_parallel = max(1, settings.image_caption_batch_parallel)
        batches_limit = max_batches if max_batches is not None else batch_parallel
        max_items = batches_limit * batch_size
        todo = todo[:max_items]

        logger.info(
            "Caption backfill: %d image(s) this tick (%d missing, %d invalid) — "
            "%d per batch × up to %d parallel",
            len(todo),
            len(missing),
            len(invalid),
            batch_size,
            batch_parallel,
        )
        dl_workers = effective_cpu_workers(settings.cpu_thread_pool_size)
        dl_sem = asyncio.Semaphore(max(4, dl_workers))

        session_factory = get_session_factory()

        async def _file_name_for_id(fid: str) -> str:
            async with session_factory() as session:
                row = await session.get(DriveFile, fid)
                return row.name if row else ""

        async def _mark_corrupt_skipped(fid: str, error: str) -> None:
            async with session_factory() as session:
                row = await session.get(DriveFile, fid)
                if row is None:
                    return
                apply_decode_failure(row, error)
                if row.status != DriveFileStatus.SKIPPED:
                    row.status = DriveFileStatus.SKIPPED
                    row.error_message = f"{CORRUPT_SKIPPED_PREFIX} {error[:500]}"
                await session.commit()

        async def _prepare_item(fid: str) -> tuple[str, bytes] | None:
            async with dl_sem:
                try:
                    raw = await download_to_memory(worker._client, fid)  # noqa: SLF001
                    file_name = await _file_name_for_id(fid)
                    image_bgr = await run_cpu_bound(decode_image_bgr, raw, file_name=file_name)
                    ok, buf = cv2.imencode(".jpg", image_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                    if ok:
                        return fid, buf.tobytes()
                except Exception as exc:  # noqa: BLE001
                    await _mark_corrupt_skipped(fid, str(exc))
                    logger.warning("Caption backfill skipped corrupt file %s", fid)
            return None

        batch_ids = [todo[i : i + batch_size] for i in range(0, len(todo), batch_size)]
        caption_sem = asyncio.Semaphore(batch_parallel)

        async def _caption_batch(ids: list[str]) -> int:
            async with caption_sem:
                prepared = await asyncio.gather(*[_prepare_item(fid) for fid in ids])
                items = [item for item in prepared if item]
                if not items:
                    return 0
                for attempt in range(4):
                    try:
                        n = await index_image_captions_batch(items)
                        logger.info("Caption backfill batch: +%d", n)
                        return n
                    except Exception:  # noqa: BLE001
                        if attempt == 3:
                            logger.exception("Caption backfill batch failed after retries")
                            return 0
                        await asyncio.sleep(5 * (attempt + 1))
                return 0

        results = await asyncio.gather(*[_caption_batch(chunk) for chunk in batch_ids])
        done = sum(results)

        _last_caption_run_at = datetime.now(tz=timezone.utc)
        _last_caption_done = done
        logger.info("Caption backfill run complete: %d caption(s)", done)
        return done
    finally:
        _caption_running = False


async def run_embedding_backfill(worker: IndexingWorker, *, max_items: int | None = None) -> int:
    """Embed processed images missing Qdrant visual vectors."""
    global _embed_running, _last_embed_run_at, _last_embed_done

    settings = get_settings()
    if not settings.gemini_api_key:
        return 0

    if _embed_running:
        return 0

    async with _lock:
        if _embed_running:
            return 0
        _embed_running = True

    done = 0
    try:
        all_ids = await _processed_image_ids()
        if not all_ids:
            return 0

        already = await asyncio.to_thread(existing_image_ids_sync, all_ids)
        todo = [fid for fid in all_ids if fid not in already]
        if not todo:
            return 0

        if max_items is not None:
            todo = todo[:max_items]

        parallel = max(1, settings.image_embed_backfill_parallel)
        session_factory = get_session_factory()
        logger.info(
            "Embedding backfill: %d image(s) this tick (up to %d parallel)",
            len(todo),
            parallel,
        )
        embed_sem = asyncio.Semaphore(parallel)

        async def _mark_corrupt_skipped(fid: str, error: str) -> None:
            async with session_factory() as session:
                row = await session.get(DriveFile, fid)
                if row is not None:
                    apply_decode_failure(row, str(error))
                    if row.status != DriveFileStatus.SKIPPED:
                        row.status = DriveFileStatus.SKIPPED
                        row.error_message = f"{CORRUPT_SKIPPED_PREFIX} {str(error)[:500]}"
                    await session.commit()

        async def _embed_one(fid: str) -> int:
            async with embed_sem:
                try:
                    async with session_factory() as session:
                        row = await session.get(DriveFile, fid)
                        file_name = row.name if row else ""
                    raw = await download_to_memory(worker._client, fid)  # noqa: SLF001
                    image_bgr = await run_cpu_bound(decode_image_bgr, raw, file_name=file_name)
                    ok, buf = cv2.imencode(".jpg", image_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                    if not ok:
                        return 0
                    await index_image_embedding(buf.tobytes(), fid)
                    return 1
                except Exception as exc:  # noqa: BLE001
                    await _mark_corrupt_skipped(fid, str(exc))
                    logger.warning("Embedding backfill skipped corrupt file %s", fid)
                    return 0

        results = await asyncio.gather(*[_embed_one(fid) for fid in todo])
        done = sum(results)

        _last_embed_run_at = datetime.now(tz=timezone.utc)
        _last_embed_done = done
        logger.info("Embedding backfill run complete: %d image(s)", done)
        return done
    finally:
        _embed_running = False


async def maintenance_tick(worker: IndexingWorker) -> None:
    """Advance caption/embed backfill in bounded parallel chunks each auto-index tick."""
    if worker.is_running:
        return

    settings = get_settings()
    batches_per_tick = max(1, settings.maintenance_batches_per_tick)

    missing_caps = await count_missing_captions()
    if missing_caps > 0 and not _caption_running:
        logger.info("Maintenance: %d image(s) need captions — starting backfill", missing_caps)
        await run_caption_backfill(worker, max_batches=batches_per_tick)

    missing_embed = await count_missing_embeddings()
    if missing_embed > 0 and not _embed_running:
        embed_limit = settings.image_embed_backfill_parallel * batches_per_tick
        logger.info("Maintenance: %d image(s) need embeddings — starting backfill", missing_embed)
        await run_embedding_backfill(worker, max_items=embed_limit)


async def startup_maintenance(worker: IndexingWorker) -> None:
    """Deferred kick after boot so Railway healthcheck passes first."""
    await asyncio.sleep(20)
    try:
        await maintenance_tick(worker)
    except Exception:  # noqa: BLE001
        logger.exception("Startup maintenance failed")
