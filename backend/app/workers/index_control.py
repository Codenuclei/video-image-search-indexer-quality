"""Cross-process control watcher for immediate, non-destructive indexing pause."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.db.models import IndexControlState
from app.drive.indexing_pause import global_indexing_is_paused
from app.workers.indexer import IndexingWorker

logger = logging.getLogger(__name__)


async def index_control_watch_loop(
    worker: IndexingWorker,
    stop_event: asyncio.Event,
    *,
    interval_seconds: float = 2.0,
) -> None:
    """Poll the shared pause flag and publish the leader's active-job heartbeat."""
    total_cancelled = 0
    while not stop_event.is_set():
        try:
            async with worker._session_factory() as session:
                paused = await global_indexing_is_paused(session)
                if paused:
                    total_cancelled += await worker.cancel_all_indexing_tasks()
                    current = asyncio.current_task()
                    for task in asyncio.all_tasks():
                        if task is current or task.done():
                            continue
                        if task.get_name() in {"maintenance-tick", "startup-maintenance"}:
                            task.cancel()
                state = await session.get(IndexControlState, 1)
                if state is None:
                    state = IndexControlState(id=1)
                    session.add(state)
                state.active_image_jobs = worker.active_image_count
                state.active_video_jobs = worker.active_video_count
                state.cancelled_jobs = total_cancelled
                state.watcher_heartbeat_at = datetime.now(timezone.utc)
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Index control watcher tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(0.5, interval_seconds))
        except asyncio.TimeoutError:
            pass
