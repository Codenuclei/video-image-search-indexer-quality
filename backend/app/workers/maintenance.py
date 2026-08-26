"""Background maintenance: caption/embedding backfill without manual triggers."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import cv2
from sqlalchemy import select

from app.config import get_settings
from app.concurrency.pools import effective_cpu_workers
from app.db.models import DriveFile, DriveFileStatus
from app.db.session import get_session_factory
from app.pipelines.async_cpu import run_cpu_bound
from app.pipelines.common import (
    decode_image_bgr,
    download_to_memory,
    looks_like_svg,
    svg_bytes_complete,
)
from app.drive.indexing_pause import (
    CORRUPT_SKIPPED_PREFIX,
    is_file_indexing_paused,
    load_paused_folder_paths,
)
from app.pipelines.decode_recovery import apply_decode_failure
from app.qdrant.image_captions import (
    existing_caption_ids_sync,
    valid_caption_ids_sync,
)
from app.qdrant.images import existing_image_ids_sync
from app.search.images import index_image_captions_batch, index_image_embeddings_batch
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
# Persist across maintenance ticks so one undecodable cache file cannot spin forever.
_embed_skip_ids: set[str] = set()


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
    try:
        missing, invalid = await caption_recaption_ids()
        todo = missing + invalid
        if not todo:
            return 0

        # Upsert-by-id overwrites invalid captions; avoid delete-then-gap.
        _last_invalid_captions_removed = 0
        if invalid:
            logger.info(
                "Caption backfill: will upsert over %d invalid/stub caption(s)",
                len(invalid),
            )

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
        # Stay under SQLAlchemy pool while Gemini caption batches run in parallel.
        prepare_slots = max(4, min(dl_workers * 2 if dl_workers else 16, batch_parallel, 16))
        dl_sem = asyncio.Semaphore(prepare_slots)

        session_factory = get_session_factory()

        async def _mark_corrupt_skipped(fid: str, error: str) -> None:
            # Permanent library: never demote PROCESSED → SKIPPED on backfill decode failure.
            async with session_factory() as session:
                row = await session.get(DriveFile, fid)
                if row is None:
                    return
                if row.status == DriveFileStatus.PROCESSED:
                    logger.warning(
                        "Caption backfill decode failed for PROCESSED %s — leaving status: %s",
                        fid,
                        error[:200],
                    )
                    return
                apply_decode_failure(row, error)
                if row.status != DriveFileStatus.SKIPPED:
                    row.status = DriveFileStatus.SKIPPED
                    row.error_message = f"{CORRUPT_SKIPPED_PREFIX} {error[:500]}"
                await session.commit()

        async def _prepare_item(fid: str) -> tuple[str, bytes] | None:
            async with dl_sem:
                try:
                    from app.drive.media_cache import ensure_media_cached, read_cached_bytes

                    file_name = ""
                    raw: bytes | None = None
                    async with session_factory() as session:
                        row = await session.get(DriveFile, fid)
                        if row is None:
                            return None
                        file_name = row.name or ""
                        path = await ensure_media_cached(
                            worker._client,  # noqa: SLF001
                            row,
                            settings,
                            allow_redownload=True,
                        )
                        await session.commit()
                    raw = await run_cpu_bound(read_cached_bytes, path)
                    if raw is None or (
                        looks_like_svg(raw, file_name=file_name) and not svg_bytes_complete(raw)
                    ):
                        logger.warning(
                            "Incomplete SVG after cache for %s (%s bytes) — re-downloading",
                            fid,
                            len(raw or b""),
                        )
                        raw = await download_to_memory(worker._client, fid)  # noqa: SLF001
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


async def _drive_connected() -> bool:
    """True when a DriveUser row exists (OAuth connected)."""
    from app.db.models import DriveUser

    session_factory = get_session_factory()
    async with session_factory() as session:
        row = (
            await session.execute(select(DriveUser.id).limit(1))
        ).scalar_one_or_none()
        return row is not None


async def _order_ids_cached_first(ids: list[str]) -> tuple[list[str], list[str]]:
    """Split ids into (on_disk_cache, needs_download) preserving relative order."""
    if not ids:
        return [], []
    from app.drive.media_cache import resolve_cache_path

    settings = get_settings()
    session_factory = get_session_factory()
    cached: list[str] = []
    uncached: list[str] = []
    # Chunk to keep select payloads bounded.
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        async with session_factory() as session:
            rows = (
                await session.execute(select(DriveFile).where(DriveFile.id.in_(chunk)))
            ).scalars().all()
        by_id = {r.id: r for r in rows}
        for fid in chunk:
            row = by_id.get(fid)
            if row is not None and resolve_cache_path(settings, row) is not None:
                cached.append(fid)
            else:
                uncached.append(fid)
    return cached, uncached


async def run_embedding_backfill(worker: IndexingWorker, *, max_items: int | None = None) -> int:
    """Embed processed images missing Qdrant visual vectors.

    Uses ``batchEmbedContents`` in waves of ``batch_size × parallel`` concurrent
    calls (default 5 × 20 → ~50 img/s when Gemini latency is ~2s/batch).

    ``max_items=None`` drains the full backlog in successive waves (POST
    ``/backfill/image-embeddings``). Maintenance passes a bounded ``max_items``.

    Prefers on-disk media cache. When Drive OAuth is disconnected, only cached
    files are embedded (download would fail).
    """
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
        batch_size = max(1, settings.image_embed_batch_size)
        parallel = max(1, settings.image_embed_backfill_parallel)
        wave_size = parallel * batch_size
        remaining_cap = max_items  # None = drain all waves
        drive_ok = await _drive_connected()
        if not drive_ok:
            logger.warning(
                "Embedding backfill: Drive OAuth disconnected — "
                "only embedding images already on media_cache (~50/s target still applies)"
            )

        session_factory = get_session_factory()
        embed_sem = asyncio.Semaphore(parallel)
        # Prepare must stay under the SQLAlchemy pool (30+20).
        dl_workers = effective_cpu_workers(settings.cpu_thread_pool_size)
        prepare_slots = max(8, min(parallel * 2, dl_workers * 2 if dl_workers else 24, 24))
        dl_sem = asyncio.Semaphore(prepare_slots)

        async def _mark_corrupt_skipped(fid: str, error: str) -> None:
            async with session_factory() as session:
                row = await session.get(DriveFile, fid)
                if row is None:
                    return
                if row.status == DriveFileStatus.PROCESSED:
                    logger.warning(
                        "Embed backfill decode failed for PROCESSED %s — leaving status: %s",
                        fid,
                        str(error)[:200],
                    )
                    return
                apply_decode_failure(row, str(error))
                if row.status != DriveFileStatus.SKIPPED:
                    row.status = DriveFileStatus.SKIPPED
                    row.error_message = f"{CORRUPT_SKIPPED_PREFIX} {str(error)[:500]}"
                await session.commit()

        async def _prepare(fid: str) -> tuple[str, bytes] | None:
            async with dl_sem:
                try:
                    from app.drive.media_cache import (
                        ensure_media_cached,
                        read_cached_bytes,
                        resolve_cache_path,
                    )

                    async with session_factory() as session:
                        row = await session.get(DriveFile, fid)
                        if row is None:
                            _embed_skip_ids.add(fid)
                            return None
                        file_name = row.name if row else ""
                        if drive_ok:
                            cache_path = await ensure_media_cached(
                                worker._client,  # noqa: SLF001
                                row,
                                settings,
                                allow_redownload=True,
                            )
                            await session.commit()
                        else:
                            cache_path = resolve_cache_path(settings, row)
                            if cache_path is None:
                                return None
                    raw = await run_cpu_bound(read_cached_bytes, cache_path)
                    if looks_like_svg(raw, file_name=file_name) and not svg_bytes_complete(raw):
                        logger.warning(
                            "Incomplete SVG after cache for embed %s (%s bytes) — re-downloading",
                            fid,
                            len(raw or b""),
                        )
                        if not drive_ok:
                            return None
                        raw = await download_to_memory(worker._client, fid)  # noqa: SLF001
                    from app.gemini.video_embeddings import _downscale_jpeg_bytes

                    # Fast path: already JPEG — skip cv2 decode/re-encode.
                    lower = (file_name or cache_path.name).lower()
                    if lower.endswith((".jpg", ".jpeg")) and raw:
                        jpeg = await run_cpu_bound(_downscale_jpeg_bytes, raw)
                        return fid, jpeg
                    image_bgr = await run_cpu_bound(decode_image_bgr, raw, file_name=file_name)
                    ok, buf = cv2.imencode(".jpg", image_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                    if not ok:
                        _embed_skip_ids.add(fid)
                        return None
                    jpeg = await run_cpu_bound(_downscale_jpeg_bytes, buf.tobytes())
                    return fid, jpeg
                except Exception as exc:  # noqa: BLE001
                    _embed_skip_ids.add(fid)
                    msg = str(exc)
                    # Transient pool / Drive auth — do not spam as "corrupt".
                    if "QueuePool" in msg or "No Google Drive account" in msg:
                        logger.debug("Embedding backfill defer %s: %s", fid, msg[:160])
                        return None
                    await _mark_corrupt_skipped(fid, msg)
                    logger.warning("Embedding backfill skipped file %s: %s", fid, msg[:160])
                    return None

        async def _embed_batch(ids: list[str]) -> int:
            async with embed_sem:
                prepared = await asyncio.gather(*[_prepare(fid) for fid in ids])
                items = [item for item in prepared if item]
                if not items:
                    return 0
                return await index_image_embeddings_batch(items)

        wave_n = 0
        empty_waves = 0
        while True:
            if remaining_cap is not None and remaining_cap <= 0:
                break

            all_ids = await _processed_image_ids()
            if not all_ids:
                break
            already = await asyncio.to_thread(existing_image_ids_sync, all_ids)
            candidates = [
                fid for fid in all_ids if fid not in already and fid not in _embed_skip_ids
            ]
            if not candidates:
                break

            cached, uncached = await _order_ids_cached_first(candidates)
            # Cached first keeps Gemini fed without Drive; uncached only if OAuth ok.
            ordered = cached + (uncached if drive_ok else [])
            if not ordered:
                logger.warning(
                    "Embedding backfill: %d missing vector(s) need Drive download "
                    "but OAuth is disconnected — stopping (reconnect Drive to continue)",
                    len(uncached),
                )
                break

            take = wave_size
            if remaining_cap is not None:
                take = min(take, remaining_cap)
            todo = ordered[:take]
            wave_n += 1
            logger.info(
                "Embedding backfill wave %d: %d image(s) "
                "(%d cached-available in backlog) — "
                "%d per batchEmbedContents × %d parallel (~50/s target)",
                wave_n,
                len(todo),
                len(cached),
                batch_size,
                parallel,
            )

            chunks = [todo[i : i + batch_size] for i in range(0, len(todo), batch_size)]
            t0 = datetime.now(tz=timezone.utc)
            results = await asyncio.gather(*[_embed_batch(chunk) for chunk in chunks])
            wave_done = sum(results)
            elapsed = max(0.001, (datetime.now(tz=timezone.utc) - t0).total_seconds())
            rps = wave_done / elapsed
            done += wave_done
            if remaining_cap is not None:
                remaining_cap -= len(todo)
            logger.info(
                "Embedding backfill wave %d complete: %d image(s) in %.1fs (%.1f/s)",
                wave_n,
                wave_done,
                elapsed,
                rps,
            )
            if wave_done == 0:
                empty_waves += 1
                if empty_waves >= 3:
                    break
            else:
                empty_waves = 0
            if max_items is not None and remaining_cap is not None and remaining_cap <= 0:
                break

        _last_embed_run_at = datetime.now(tz=timezone.utc)
        _last_embed_done = done
        logger.info("Embedding backfill run complete: %d image(s) across %d wave(s)", done, wave_n)
        return done
    finally:
        _embed_running = False


# Qdrant status recovery is a repair task, not a per-tick one: it scrolls every
# point of three collections and loads all drive_files/media rows. Running it on
# each maintenance tick hammered Qdrant/Postgres continuously and starved
# interactive API routes (e.g. carousel select-images).
_recover_running = False
_last_recover_at: datetime | None = None
_last_recover_counts: dict[str, int] | None = None
_RECOVER_MIN_INTERVAL_SEC = 6 * 3600.0


async def _recover_status_from_qdrant(*, force: bool = False) -> None:
    """Mark files PROCESSED when Qdrant already has their vectors.

    Append-only repair (scrolls Qdrant, updates Postgres). Overlap-guarded,
    throttled to once per ``_RECOVER_MIN_INTERVAL_SEC``, and skipped entirely
    when collection point counts have not changed since the last completed
    run. ``force=True`` (explicit user-triggered endpoints) bypasses the
    throttle and the change gate but never the overlap guard.
    """
    global _recover_running, _last_recover_at, _last_recover_counts
    from app.qdrant.recover import collection_point_counts, recover_from_qdrant

    if _recover_running:
        return
    now = datetime.now(tz=timezone.utc)
    if (
        not force
        and _last_recover_at is not None
        and (now - _last_recover_at).total_seconds() < _RECOVER_MIN_INTERVAL_SEC
    ):
        return
    _recover_running = True
    try:
        counts = await asyncio.to_thread(collection_point_counts)
        if (
            not force
            and _last_recover_counts is not None
            and counts == _last_recover_counts
        ):
            _last_recover_at = datetime.now(tz=timezone.utc)
            return
        session_factory = get_session_factory()
        async with session_factory() as session:
            result = await recover_from_qdrant(session, dry_run=False)
        if result.status_marked_processed:
            logger.info(
                "Qdrant recovery: marked %d file(s) PROCESSED from existing vectors",
                result.status_marked_processed,
            )
        _last_recover_counts = counts
        _last_recover_at = datetime.now(tz=timezone.utc)
    except Exception:
        logger.exception("Qdrant status recovery failed (continuing)")
    finally:
        _recover_running = False


async def maintenance_tick(worker: IndexingWorker) -> None:
    """Advance caption/embed backfill in bounded parallel chunks.

    Runs even while Start Index / ``worker.is_running`` is true so captions are
    not starved for the whole indexing cycle. Captions are never generated
    inline in ``process_image_file`` — this path is the only producer.

    Work per tick is sized to the async Gemini semaphores (caption/embed
    parallel), not Gunicorn worker count. ``maintenance_batches_per_tick`` is a
    floor; we never run fewer caption batches than ``image_caption_batch_parallel``.
    """
    from app.drive.cleanup import restore_archived_when_index_complete
    from app.drive.indexing_pause import global_indexing_is_paused

    session_factory = get_session_factory()
    async with session_factory() as session:
        if await global_indexing_is_paused(session):
            logger.info("Maintenance skipped: global indexing pause is active")
            return
        saved = await restore_archived_when_index_complete(session)
        if saved:
            await session.commit()
            logger.info(
                "Maintenance: restored %d archived file(s) that already qualify as PROCESSED",
                saved,
            )

    settings = get_settings()
    batches_per_tick = max(1, settings.maintenance_batches_per_tick)
    caption_batches = max(batches_per_tick, max(1, settings.image_caption_batch_parallel))

    await _recover_status_from_qdrant()

    missing_embed = await count_missing_embeddings()
    if missing_embed > 0 and not _embed_running:
        # Prefer draining embeddings; multi-wave so we keep Gemini busy (~50/s target).
        embed_limit = (
            settings.image_embed_backfill_parallel
            * max(1, settings.image_embed_batch_size)
            * max(1, batches_per_tick)
        )
        logger.info("Maintenance: %d image(s) need embeddings — starting backfill", missing_embed)
        await run_embedding_backfill(worker, max_items=embed_limit)

    missing_caps = await count_missing_captions()
    if missing_caps > 0 and not _caption_running and not _embed_running:
        logger.info("Maintenance: %d image(s) need captions — starting backfill", missing_caps)
        await run_caption_backfill(worker, max_batches=caption_batches)

    # After caption/embed progress, reclaim cache for fully done PROCESSED rows.
    schedule_cache_cleanup_tick()


_last_cache_cleanup_mono = 0.0
_cache_cleanup_running = False
_CACHE_CLEANUP_MIN_INTERVAL_SEC = 120.0


def schedule_cache_cleanup_tick() -> None:
    """Fire-and-forget cache cleaner — never blocks indexing.

    Only deletes disk cache for rows already PROCESSED (images need caption+embed).
    Does not change DriveFile status.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_safe_cache_cleanup_tick(), name="cache-cleanup-tick")


async def _safe_cache_cleanup_tick() -> None:
    global _last_cache_cleanup_mono, _cache_cleanup_running
    if _cache_cleanup_running:
        return
    now = time.monotonic()
    if (now - _last_cache_cleanup_mono) < _CACHE_CLEANUP_MIN_INTERVAL_SEC:
        return
    _cache_cleanup_running = True
    _last_cache_cleanup_mono = now
    try:
        from app.drive.cache_cleanup import run_cache_cleanup

        result = await run_cache_cleanup(apply=True)
        deleted = int(result.get("deleted_count") or 0)
        if deleted:
            logger.info(
                "Auto cache cleanup deleted_files=%d deleted_bytes=%s",
                deleted,
                result.get("deleted_bytes"),
            )
    except Exception:  # noqa: BLE001
        logger.exception("Scheduled cache cleanup failed")
    finally:
        _cache_cleanup_running = False


def schedule_maintenance_tick(worker: IndexingWorker) -> None:
    """Fire-and-forget maintenance so indexing ticks are not blocked by VLM batches."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_safe_maintenance_tick(worker), name="maintenance-tick")


async def _safe_maintenance_tick(worker: IndexingWorker) -> None:
    try:
        await maintenance_tick(worker)
    except Exception:  # noqa: BLE001
        logger.exception("Scheduled maintenance tick failed")


async def startup_maintenance(worker: IndexingWorker) -> None:
    """Deferred kick after boot so Railway healthcheck passes first."""
    await asyncio.sleep(20)
    try:
        await maintenance_tick(worker)
    except Exception:  # noqa: BLE001
        logger.exception("Startup maintenance failed")
