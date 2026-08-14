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

from sqlalchemy import update
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
        )
        .returning(DriveFile.id)
    )
    return [str(file_id) for file_id in result.scalars().all()]


async def bulk_apply_status_writes(
    session: AsyncSession,
    writes: list[StatusWrite],
) -> int:
    """Apply many status finals in one transaction (grouped by identical payload)."""
    if not writes:
        return 0

    # Group identical status+error payloads so each group is one UPDATE.
    groups: dict[tuple, list[str]] = {}
    for w in writes:
        key = (
            w.status,
            w.error_message,
            w.gemini_document_name,
            w.clear_gemini_document,
            w.bump_synced_at,
        )
        groups.setdefault(key, []).append(w.file_id)

    total = 0
    stamp = datetime.now(timezone.utc)
    for (status, error_message, gemini_doc, clear_gemini, bump_synced), ids in groups.items():
        values: dict = {
            "status": status,
            "error_message": error_message,
        }
        if clear_gemini:
            values["gemini_document_name"] = None
        elif gemini_doc is not None:
            values["gemini_document_name"] = gemini_doc
        if bump_synced or status == DriveFileStatus.PROCESSED:
            values["last_synced_at"] = stamp
        result = await session.execute(
            update(DriveFile).where(DriveFile.id.in_(ids)).values(**values)
        )
        total += int(result.rowcount or 0)
    return total


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
        async with self._lock:
            # Latest write for a file wins (collapse duplicates in the buffer).
            self._queue = [w for w in self._queue if w.file_id != write.file_id]
            self._queue.append(write)
            if flush_if_full and len(self._queue) >= self._batch_size:
                await self._flush_unlocked()

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
