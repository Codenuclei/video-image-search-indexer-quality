from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.dependencies import get_indexing_worker
from app.drive.google_client import DriveDirectError
from app.runtime_settings import get_runtime_settings

logger = logging.getLogger(__name__)

_last_webhook_at: datetime | None = None
WEBHOOK_DEBOUNCE_SECONDS = 5


async def trigger_index_cycle(*, reason: str) -> bool:
    """Sync Drive file list; process pending files when auto-index is enabled."""
    global _last_webhook_at

    worker = get_indexing_worker()
    if worker.is_running:
        logger.info("Index trigger skipped — cycle already running (%s)", reason)
        return False

    if reason.startswith("webhook:"):
        now = datetime.now(timezone.utc)
        if _last_webhook_at and (now - _last_webhook_at).total_seconds() < WEBHOOK_DEBOUNCE_SECONDS:
            logger.debug("Webhook index trigger debounced (%s)", reason)
            return False
        _last_webhook_at = now

    runtime = get_runtime_settings()
    try:
        seen = await worker.sync_file_list()
        logger.info("Drive sync finished (%s): %d file(s)", reason, seen)
        if runtime.auto_index_enabled:
            summary = await worker.process_pending()
            logger.info("Index processing finished (%s): %s", reason, summary)
    except DriveDirectError as exc:
        logger.info("Drive sync skipped (%s): %s", reason, exc)
        return False
    except Exception:  # noqa: BLE001
        logger.exception("Index cycle failed (%s)", reason)
        return False
    return True
