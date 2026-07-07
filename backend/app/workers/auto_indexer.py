from __future__ import annotations

import asyncio
import logging

from app.drive.google_client import DriveDirectError
from app.runtime_settings import get_runtime_settings
from app.workers.indexer import IndexingWorker

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
                    summary = await worker.process_pending()
                    logger.info("Auto-index processing: %s", summary)
                except Exception:  # noqa: BLE001
                    logger.exception("Auto-index processing failed")
        else:
            logger.debug("Auto sync tick skipped — cycle in progress")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    logger.info("Drive sync background loop stopped")
