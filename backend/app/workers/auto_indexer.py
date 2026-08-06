from __future__ import annotations

import asyncio
import logging
import time

from app.config import get_settings
from app.dependencies import get_drive_client
from app.drive.cache_refresh import refresh_drive_file_list_cache
from app.drive.google_client import DriveDirectError
from app.drive.push_channels import (
    get_push_channel_state,
    register_or_renew_channel,
    resolve_webhook_address,
)
from app.db.app_settings_store import refresh_runtime_settings_from_db
from app.runtime_settings import get_runtime_settings
from app.workers.indexer import IndexingWorker
from app.workers.maintenance import schedule_maintenance_tick
from app.workers.requeue_failed import requeue_failed_files


logger = logging.getLogger(__name__)

# Without push notifications, keep a 15m safety-net sync (previous behaviour).
# With an active push channel, only fall back on the long DRIVE_CACHE_FALLBACK interval.
_FALLBACK_SYNC_MIN_SEC_NO_PUSH = 900.0
_drive_sync_lock = asyncio.Lock()
_last_full_drive_sync_mono = 0.0


def _fallback_interval_sec() -> float:
    settings = get_settings()
    if get_push_channel_state().status().get("active"):
        return max(3600.0, float(settings.drive_cache_fallback_sync_seconds))
    return _FALLBACK_SYNC_MIN_SEC_NO_PUSH


async def auto_index_loop(worker: IndexingWorker, stop_event: asyncio.Event) -> None:
    """
    Process pending files on the short interval; refresh Drive file list via
    push webhooks (preferred) or a rare fallback sync.
    """
    logger.info(
        "Drive sync background loop started (interval=%ss, webhook=%s)",
        get_runtime_settings().auto_index_interval_seconds,
        resolve_webhook_address(get_settings()) or "(none — startup seed + manual)",
    )
    while not stop_event.is_set():
        # DB is source of truth — other Gunicorn workers may have written PUT /settings.
        try:
            runtime = await refresh_runtime_settings_from_db()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to refresh runtime settings from DB; using in-memory cache")
            runtime = get_runtime_settings()
        interval = max(30, runtime.auto_index_interval_seconds)

        if not worker.is_running:
            if runtime.auto_index_enabled:
                try:
                    if runtime.reindex_errored_files or runtime.reindex_skipped_files:
                        await requeue_failed_files(
                            worker._session_factory,
                            reindex_errored=runtime.reindex_errored_files,
                            reindex_skipped=runtime.reindex_skipped_files,
                        )
                    await worker.ensure_parallel_video_indexing()
                    await worker.ensure_parallel_image_indexing()
                    summary = await worker.process_pending()
                    logger.info("Auto-index processing: %s", summary)
                except Exception:  # noqa: BLE001
                    logger.exception("Auto-index processing failed")

            # Caption/embed backfill every tick (including while Start Index runs).
            # Captions are maintenance-only — never inline in process_image_file.
            schedule_maintenance_tick(worker)

            # Renew push channel near expiry (no Drive tree walk).
            try:
                await register_or_renew_channel(get_drive_client(), force=False)
            except Exception:  # noqa: BLE001
                logger.exception("Drive push channel renew tick failed")

            # Rare fallback sync — webhook-driven updates are the primary path.
            # Continuous short-interval tree walks that starved uvicorn are gone.
            global _last_full_drive_sync_mono
            if runtime.auto_index_enabled:
                fallback_sec = _fallback_interval_sec()
                now = time.monotonic()
                due = (now - _last_full_drive_sync_mono) >= fallback_sec
                if due and not _drive_sync_lock.locked():
                    async with _drive_sync_lock:
                        now = time.monotonic()
                        if (now - _last_full_drive_sync_mono) >= fallback_sec:
                            try:
                                result = await refresh_drive_file_list_cache(
                                    source="fallback",
                                    sync_db=True,
                                )
                                _last_full_drive_sync_mono = time.monotonic()
                                logger.info(
                                    "Fallback Drive cache sync finished: %s; next in ≥%ss",
                                    result,
                                    int(fallback_sec),
                                )
                            except DriveDirectError as exc:
                                logger.warning("Fallback Drive sync skipped: %s", exc)
                                _last_full_drive_sync_mono = time.monotonic()
                            except Exception:  # noqa: BLE001
                                logger.exception("Fallback Drive sync failed")
                                _last_full_drive_sync_mono = time.monotonic()

                            try:
                                await worker.ensure_parallel_video_indexing()
                                await worker.ensure_parallel_image_indexing()
                            except Exception:  # noqa: BLE001
                                logger.exception("Post-sync parallel slot fill failed")
        else:
            try:
                await worker.ensure_parallel_video_indexing()
                await worker.ensure_parallel_image_indexing()
            except Exception:  # noqa: BLE001
                logger.exception("Parallel video slot fill failed")
            # Still caption while a Start Index cycle holds is_running.
            schedule_maintenance_tick(worker)
            logger.debug("Auto sync tick skipped — cycle in progress")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    logger.info("Drive sync background loop stopped")
