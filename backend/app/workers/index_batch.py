"""Batched DB writes for the indexer — claim / finalize many files in one commit.

Per-file sessions were exhausting Postgres QueuePool (46 parallel jobs × 1
connection each). Claim and status updates go through bulk UPDATEs instead.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import DriveFile, DriveFileStatus

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 100


@dataclass(slots=True)
class StatusWrite:
    file_id: str
    status: DriveFileStatus
    error_message: str | None = None
    gemini_document_name: str | None = None
    clear_gemini_document: bool = False
    bump_synced_at: bool = False
    unlink_drive_cache: bool = False
    # Wall-clock when the worker finished the file (set at enqueue). Flush must
    # use this for TAT — never one shared stamp for a whole 100-row batch.
    finished_at: datetime | None = None


async def bulk_claim_files(
    session: AsyncSession,
    file_ids: list[str],
    *,
    now: datetime | None = None,
    expected_statuses: Collection[DriveFileStatus] = (DriveFileStatus.PENDING,),
) -> int:
    """Return how many rows were atomically claimed."""
    claimed = await bulk_claim_file_ids(
        session,
        file_ids,
        now=now,
        expected_statuses=expected_statuses,
    )
    return len(claimed)


async def bulk_claim_file_ids(
    session: AsyncSession,
    file_ids: list[str],
    *,
    now: datetime | None = None,
    expected_statuses: Collection[DriveFileStatus] = (DriveFileStatus.PENDING,),
) -> list[str]:
    """Atomically claim and return only rows that actually transitioned."""
    ids = [fid for fid in file_ids if fid]
    statuses = tuple(expected_statuses)
    if not ids or not statuses:
        return []
    stamp = now or datetime.now(timezone.utc)
    result = await session.execute(
        update(DriveFile)
        .where(
            DriveFile.id.in_(ids),
            DriveFile.status.in_(statuses),
        )
        .values(
            status=DriveFileStatus.PROCESSING,
            error_message=None,
            last_synced_at=stamp,
            processing_started_at=stamp,
            completed_at=None,
        )
        .returning(DriveFile.id)
    )
    return [str(file_id) for file_id in result.scalars().all()]


async def bulk_apply_status_writes(
    session: AsyncSession,
    writes: list[StatusWrite],
) -> int:
    """Apply one bounded status batch with one UPDATE ... FROM VALUES statement."""
    if not writes:
        return 0

    fallback_stamp = datetime.now(timezone.utc)
    rows_sql: list[str] = []
    params: dict[str, object] = {}
    for index, write in enumerate(writes):
        rows_sql.append(
            f"(CAST(:id_{index} AS varchar), CAST(:status_{index} AS varchar), "
            f"CAST(:error_{index} AS text), CAST(:gemini_{index} AS varchar), "
            f"CAST(:clear_{index} AS boolean), CAST(:bump_{index} AS boolean), "
            f"CAST(:finished_{index} AS timestamptz))"
        )
        params[f"id_{index}"] = write.file_id
        params[f"status_{index}"] = write.status.name
        params[f"error_{index}"] = write.error_message
        params[f"gemini_{index}"] = write.gemini_document_name
        params[f"clear_{index}"] = write.clear_gemini_document
        params[f"bump_{index}"] = write.bump_synced_at
        params[f"finished_{index}"] = write.finished_at or fallback_stamp

    result = await session.execute(
        text(
            f"""
            UPDATE drive_files AS target
            SET status = batch.status::drive_file_status,
                error_message = batch.error_message,
                gemini_document_name = CASE
                    WHEN batch.clear_gemini THEN NULL
                    WHEN batch.gemini_document_name IS NOT NULL
                        THEN batch.gemini_document_name
                    ELSE target.gemini_document_name
                END,
                last_synced_at = CASE
                    WHEN batch.bump_synced
                      OR batch.status IN ('PROCESSED', 'ERROR')
                        THEN batch.finished_at
                    ELSE target.last_synced_at
                END,
                completed_at = CASE
                    WHEN batch.status IN ('PROCESSED', 'ERROR')
                     AND target.processing_started_at IS NOT NULL
                     AND target.completed_at IS NULL
                        THEN batch.finished_at
                    ELSE target.completed_at
                END
            FROM (
                VALUES {", ".join(rows_sql)}
            ) AS batch(
                file_id,
                status,
                error_message,
                gemini_document_name,
                clear_gemini,
                bump_synced,
                finished_at
            )
            WHERE target.id = batch.file_id
            """
        ),
        params,
    )
    return int(result.rowcount or 0)


class IndexStatusBatcher:
    """Queue status finals and flush every ``batch_size`` rows (or on demand)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._session_factory = session_factory
        self._batch_size = max(1, batch_size)
        self._lock = asyncio.Lock()
        self._queue: list[StatusWrite] = []
        self._flushed_total = 0

    @property
    def pending_count(self) -> int:
        return len(self._queue)

    @property
    def pending_ids(self) -> set[str]:
        """File ids waiting for the next 100-row status flush."""
        return {w.file_id for w in self._queue}

    async def enqueue(self, write: StatusWrite, *, flush_if_full: bool = True) -> None:
        if write.finished_at is None:
            write.finished_at = datetime.now(timezone.utc)
        async with self._lock:
            # Latest write for a file wins (collapse duplicates in the buffer).
            self._queue = [w for w in self._queue if w.file_id != write.file_id]
            self._queue.append(write)
            if flush_if_full and len(self._queue) >= self._batch_size:
                await self._flush_unlocked()
        # Scratch unlink is best-effort; durable policy cleanup waits until the
        # row is PROCESSED in Postgres (and captioned+embedded for images).
        if write.unlink_drive_cache or write.status in (
            DriveFileStatus.PROCESSED,
            DriveFileStatus.ERROR,
        ):
            try:
                from app.drive.media_cache import unlink_drive_cache_now

                await unlink_drive_cache_now(write.file_id)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "status_enqueue_immediate_unlink_failed file_id=%s",
                    write.file_id[:12],
                )

    async def flush(self) -> int:
        async with self._lock:
            return await self._flush_unlocked()

    async def _flush_unlocked(self) -> int:
        if not self._queue:
            return 0
        batch = self._queue
        self._queue = []
        try:
            async with self._session_factory() as session:
                n = await bulk_apply_status_writes(session, batch)
                # Phase 0: drop Drive download leftovers after PROCESSED/ERROR.
                unlink_ids = [
                    w.file_id
                    for w in batch
                    if w.unlink_drive_cache
                    or w.status
                    in (DriveFileStatus.PROCESSED, DriveFileStatus.ERROR)
                ]
                if unlink_ids:
                    from app.drive.media_cache import unlink_drive_caches_for_ids

                    try:
                        removed = await unlink_drive_caches_for_ids(session, unlink_ids)
                        if removed:
                            logger.info(
                                "index_status_batch_unlink caches=%d of %d",
                                removed,
                                len(unlink_ids),
                            )
                    except Exception:  # noqa: BLE001
                        # Never block status flush on cache cleanup.
                        logger.exception(
                            "index_status_batch_unlink_failed count=%d",
                            len(unlink_ids),
                        )
                await session.commit()
            self._flushed_total += n
            logger.info(
                "index_status_batch_flush wrote=%d queued_was=%d lifetime=%d",
                n,
                len(batch),
                self._flushed_total,
            )
            return n
        except Exception:
            # Put writes back so a later flush can retry.
            self._queue = batch + self._queue
            logger.exception(
                "index_status_batch_flush_failed count=%d — re-queued",
                len(batch),
            )
            raise
