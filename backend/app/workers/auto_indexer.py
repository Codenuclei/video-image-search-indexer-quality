from __future__ import annotations

import asyncio
import logging
import time

from app.drive.google_client import DriveDirectError
from app.runtime_settings import get_runtime_settings
from app.workers.indexer import IndexingWorker
from app.workers.maintenance import maintenance_tick
from app.workers.requeue_failed import requeue_failed_files

logger = logging.getLogger(__name__)

# Full Drive tree walks are expensive (thousands of sequential folder GETs) and
# used to run every auto-index tick (often 30s), starving carousel /health and
# list APIs on the single uvicorn worker. Keep pending-file processing on the
# short interval; run full sync much less often.
_FULL_DRIVE_SYNC_MIN_SEC = 900.0
_drive_sync_lock = asyncio.Lock()
_last_full_drive_sync_mono = 0.0


async def auto_index_loop(worker: IndexingWorker, stop_event: asyncio.Event) -> None:
    """
    Periodically processes pending files and syncs Drive when auto-index is on.
    When auto-index is off, the loop only sleeps — full Drive tree walks are skipped
    so local carousel studio work is not starved by sync traffic.
    """
    logger.info(
        "Drive sync background loop started (interval=%ss, full_sync_min=%ss)",
        get_runtime_settings().auto_index_interval_seconds,
        int(_FULL_DRIVE_SYNC_MIN_SEC),
    )
    while not stop_event.is_set():
        runtime = get_runtime_settings()
        interval = max(30, runtime.auto_index_interval_seconds)

        if not worker.is_running:
            # Process the existing queue first. Full Drive sync can take many minutes on
            # large trees and used to starve pending claims every tick.
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

            # Full Drive tree walks can take many minutes and saturate the single
            # uvicorn worker's sockets/threads. When auto-index is off (typical
            # local carousel studio), skip sync so extract/generate stay responsive.
            # When on, throttle to at most once per _FULL_DRIVE_SYNC_MIN_SEC and
            # never overlap two walks.
            if runtime.auto_index_enabled:
                global _last_full_drive_sync_mono
                now = time.monotonic()
                due = (now - _last_full_drive_sync_mono) >= _FULL_DRIVE_SYNC_MIN_SEC
                if due and not _drive_sync_lock.locked():
                    async with _drive_sync_lock:
                        # Re-check under lock in case another tick started.
                        now = time.monotonic()
                        if (now - _last_full_drive_sync_mono) >= _FULL_DRIVE_SYNC_MIN_SEC:
                            try:
                                seen = await worker.sync_file_list()
                                _last_full_drive_sync_mono = time.monotonic()
                                logger.info(
                                    "Auto file-list sync finished: %d file(s); next full sync in ≥%ss",
                                    seen,
                                    int(_FULL_DRIVE_SYNC_MIN_SEC),
                                )
                            except DriveDirectError as exc:
                                logger.warning("Auto file-list sync skipped: %s", exc)
                                _last_full_drive_sync_mono = time.monotonic()
                            except Exception:  # noqa: BLE001 — keep the loop alive
                                logger.exception("Auto file-list sync failed")
                                _last_full_drive_sync_mono = time.monotonic()

                            try:
                                await worker.ensure_parallel_video_indexing()
                                await worker.ensure_parallel_image_indexing()
                            except Exception:  # noqa: BLE001
                                logger.exception("Post-sync parallel slot fill failed")

                            try:
                                await maintenance_tick(worker)
                            except Exception:  # noqa: BLE001
                                logger.exception("Auto maintenance tick failed")
        else:
            try:
                await worker.ensure_parallel_video_indexing()
                await worker.ensure_parallel_image_indexing()
            except Exception:  # noqa: BLE001
                logger.exception("Parallel video slot fill failed")
            logger.debug("Auto sync tick skipped — cycle in progress")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    logger.info("Drive sync background loop stopped")
