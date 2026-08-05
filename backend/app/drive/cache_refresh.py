"""Refresh the in-memory Drive file-list cache and optionally sync DB rows."""
from __future__ import annotations

import logging

from app.db.advisory_locks import LOCK_DRIVE_CACHE_REFRESH, advisory_lock
from app.dependencies import get_drive_client, get_indexing_worker
from app.drive.file_list_cache import get_file_list_cache
from app.drive.google_client import DriveDirectError
from app.drive.push_channels import advance_page_token
from app.runtime_settings import get_runtime_settings

logger = logging.getLogger(__name__)


async def refresh_drive_file_list_cache(
    *,
    source: str,
    sync_db: bool = True,
    process_pending: bool | None = None,
) -> dict[str, object]:
    """
    Pull a fresh Drive listing into memory, then upsert into Postgres when asked.

    Designed to run as a background task after Google push notifications so the
    webhook handler can return quickly.

    Cross-worker: a Postgres advisory lock prevents 24 Gunicorn workers from
    walking Drive / syncing DB at once when a push is load-balanced.
    """
    async with advisory_lock(
        LOCK_DRIVE_CACHE_REFRESH, name="drive_cache_refresh", blocking=False
    ) as got_global:
        if not got_global:
            logger.info(
                "Drive cache refresh skipped — another worker holds refresh lock (%s)",
                source,
            )
            return {"ok": False, "skipped": True, "reason": "global_in_flight"}

        cache = get_file_list_cache()
        if not await cache.mark_refresh_start():
            logger.info("Drive cache refresh skipped — already in flight (%s)", source)
            return {"ok": False, "skipped": True, "reason": "in_flight"}

        worker = get_indexing_worker()
        try:
            if sync_db:
                if worker.is_running:
                    # Still refresh memory via a direct list so polls stay current.
                    client = get_drive_client()
                    listing = await client.list_folder_files()
                    await cache.replace(listing, source=source)
                    await advance_page_token(client)
                    logger.info(
                        "Drive DB sync deferred — indexer already running (%s)", source
                    )
                    return {
                        "ok": True,
                        "source": source,
                        "files": sum(1 for f in listing.files if not f.is_folder),
                        "truncated": listing.truncated,
                        "db_synced": False,
                        "db_deferred": True,
                    }

                seen = await worker.sync_file_list(cache_source=source)
                client = get_drive_client()
                await advance_page_token(client)
                result: dict[str, object] = {
                    "ok": True,
                    "source": source,
                    "files": seen,
                    "db_synced": True,
                }
                do_process = (
                    process_pending
                    if process_pending is not None
                    else get_runtime_settings().auto_index_enabled
                )
                if do_process:
                    summary = await worker.process_pending()
                    result["processed"] = summary
                return result

            client = get_drive_client()
            listing = await client.list_folder_files()
            await cache.replace(listing, source=source)
            await advance_page_token(client)
            return {
                "ok": True,
                "source": source,
                "files": sum(1 for f in listing.files if not f.is_folder),
                "truncated": listing.truncated,
                "db_synced": False,
            }
        except DriveDirectError as exc:
            await cache.mark_refresh_done(error=str(exc))
            logger.warning("Drive cache refresh skipped (%s): %s", source, exc)
            return {"ok": False, "source": source, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            await cache.mark_refresh_done(error=str(exc))
            logger.exception("Drive cache refresh failed (%s)", source)
            return {"ok": False, "source": source, "error": str(exc)[:240]}
