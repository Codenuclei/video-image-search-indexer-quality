from __future__ import annotations

import asyncio
import logging

from app.drive.google_client import DriveDirectError
from app.runtime_settings import get_runtime_settings
from app.workers.indexer import IndexingWorker
from app.workers.maintenance import maintenance_tick
from app.workers.requeue_failed import requeue_failed_files

logger = logging.getLogger(__name__)


async def auto_index_loop(worker: IndexingWorker, stop_event: asyncio.Event) -> None:
    """
    Periodically syncs the Drive folder file list.
    Processes pending files only when auto-indexing is enabled.
    """
    logger.info(
        "Drive sync background loop started (interval=%ss)",
        get_runtime_settings().auto_index_interval_seconds,
    )
    while not stop_event.is_set():
        runtime = get_runtime_settings()
        interval = max(30, runtime.auto_index_interval_seconds)

        if not worker.is_running:
            try:
                seen = await worker.sync_file_list()
                logger.debug("Auto file-list sync: %d file(s)", seen)
            except DriveDirectError as exc:
                logger.warning("Auto file-list sync skipped: %s", exc)
            except Exception:  # noqa: BLE001 — keep the loop alive
                logger.exception("Auto file-list sync failed")

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
