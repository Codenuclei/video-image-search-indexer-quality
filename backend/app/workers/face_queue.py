"""Durable InsightFace job queue — Postgres FOR UPDATE SKIP LOCKED.

Indexer (or prepare path) enqueues PENDING rows. Volume-less dfi-face-worker
replicas claim one job at a time (sequential InsightFace lock per process).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db.models import DriveFile, DriveFileStatus, FaceJob, FaceJobStatus, Media
from app.db.session import get_session_factory
from app.drive.media_cache import unlink_drive_source_cache
from app.workers.index_batch import IndexStatusBatcher, StatusWrite
from app.workers.index_errors import friendly_index_error_message

logger = logging.getLogger(__name__)


async def enqueue_face_job(session: AsyncSession, drive_file_id: str) -> FaceJob | None:
    """Insert a PENDING face job unless one is already pending/processing."""
    if not drive_file_id:
        return None
    existing = await session.scalar(
        select(FaceJob)
        .where(
            FaceJob.drive_file_id == drive_file_id,
            FaceJob.status.in_((FaceJobStatus.PENDING, FaceJobStatus.PROCESSING)),
        )
        .limit(1)
    )
    if existing is not None:
        return existing
    job = FaceJob(drive_file_id=drive_file_id, status=FaceJobStatus.PENDING, attempts=0)
    session.add(job)
    await session.flush()
    logger.info("face_job_enqueued file_id=%s job_id=%s", drive_file_id[:12], job.id)
    return job


async def claim_face_jobs(
    session: AsyncSession,
    *,
    limit: int = 1,
    lease_seconds: int = 900,
    worker_token: str | None = None,
) -> list[FaceJob]:
    """Atomically claim up to ``limit`` PENDING (or expired PROCESSING) jobs."""
    limit = max(1, min(int(limit), 8))
    token = worker_token or uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(seconds=max(60, int(lease_seconds)))

    # Reclaim expired leases first.
    await session.execute(
        update(FaceJob)
        .where(
            FaceJob.status == FaceJobStatus.PROCESSING,
            FaceJob.locked_at.is_not(None),
            FaceJob.locked_at < stale_before,
        )
        .values(status=FaceJobStatus.PENDING, lock_token=None, locked_at=None)
    )

    result = await session.execute(
        text(
            """
            UPDATE face_jobs
            SET status = 'PROCESSING',
                lock_token = :token,
                locked_at = :now,
                attempts = attempts + 1,
                updated_at = :now
            WHERE id IN (
                SELECT id FROM face_jobs
                WHERE status = 'PENDING'
                ORDER BY created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT :lim
            )
            RETURNING id
            """
        ),
        {"token": token, "now": now, "lim": limit},
    )
    ids = [int(row[0]) for row in result.fetchall()]
    if not ids:
        return []
    jobs = list(
        (await session.execute(select(FaceJob).where(FaceJob.id.in_(ids)))).scalars().all()
    )
    return jobs


async def process_face_job(
    session: AsyncSession,
    job: FaceJob,
    *,
    settings: Settings | None = None,
    status_batcher: IndexStatusBatcher | None = None,
    embed_queue=None,
) -> None:
    """Run InsightFace for one claimed job, then embed/status or ERROR."""
    from app.dependencies import get_drive_client
    from app.faces.engine import get_face_engine
    from app.pipelines.image import apply_faces_to_prepared_image

    settings = settings or get_settings()
    drive_file = await session.get(DriveFile, job.drive_file_id)
    if drive_file is None:
        job.status = FaceJobStatus.DONE
        job.error_message = "drive_file_missing"
        return

    try:
        media = await session.scalar(
            select(Media).where(Media.drive_file_id == drive_file.id).limit(1)
        )
        if media is None:
            raise RuntimeError("face_job_missing_media")

        # Already search-ready (e.g. indexed before face fleet, or cache cleaned after
        # PROCESSED). Never demote PROCESSED — close the job and move on.
        if drive_file.status == DriveFileStatus.PROCESSED:
            job.status = FaceJobStatus.DONE
            job.error_message = None
            job.lock_token = None
            job.locked_at = None
            await session.commit()
            logger.info(
                "face_job_skip_already_processed job_id=%s file_id=%s",
                job.id,
                drive_file.id[:12],
            )
            return

        await apply_faces_to_prepared_image(
            session,
            drive_file,
            media,
            client=get_drive_client(),
            settings=settings,
            engine=get_face_engine(),
        )
        job.status = FaceJobStatus.DONE
        job.error_message = None
        job.lock_token = None
        job.locked_at = None
        await session.commit()

        if embed_queue is not None and settings.gemini_api_key:
            await embed_queue.push(drive_file.id)
        elif status_batcher is not None:
            await status_batcher.enqueue(
                StatusWrite(
                    file_id=drive_file.id,
                    status=DriveFileStatus.PROCESSED,
                    error_message=None,
                    clear_gemini_document=True,
                    bump_synced_at=True,
                )
            )
        logger.info(
            "face_job_done job_id=%s file_id=%s attempts=%s",
            job.id,
            drive_file.id[:12],
            job.attempts,
        )
    except Exception as exc:  # noqa: BLE001
        msg = friendly_index_error_message(exc, max_len=500)
        # Cache was cleaned after PROCESSED — treat as success for the Drive row.
        if "media_cache reclaimed for PROCESSED" in msg:
            job.status = FaceJobStatus.DONE
            job.error_message = msg[:500]
            job.lock_token = None
            job.locked_at = None
            if drive_file.status != DriveFileStatus.PROCESSED:
                # Should already be PROCESSED; never force ERROR for this case.
                drive_file.status = DriveFileStatus.PROCESSED
                drive_file.error_message = None
            await session.commit()
            logger.info(
                "face_job_done_cache_gone job_id=%s file_id=%s",
                job.id,
                drive_file.id[:12],
            )
            return
        max_attempts = max(1, settings.face_job_max_attempts)
        if job.attempts >= max_attempts:
            job.status = FaceJobStatus.ERROR
            job.error_message = msg
            # Only demote incomplete rows — never overwrite PROCESSED.
            if drive_file.status != DriveFileStatus.PROCESSED:
                drive_file.status = DriveFileStatus.ERROR
                drive_file.error_message = msg
                unlink_drive_source_cache(drive_file, settings)
            await session.commit()
            if (
                status_batcher is not None
                and drive_file.status == DriveFileStatus.ERROR
            ):
                await status_batcher.enqueue(
                    StatusWrite(
                        file_id=drive_file.id,
                        status=DriveFileStatus.ERROR,
                        error_message=msg,
                        clear_gemini_document=True,
                        bump_synced_at=True,
                    )
                )
            logger.warning(
                "face_job_error_final job_id=%s file_id=%s err=%s",
                job.id,
                drive_file.id[:12],
                msg[:160],
            )
        else:
            job.status = FaceJobStatus.PENDING
            job.lock_token = None
            job.locked_at = None
            job.error_message = msg
            await session.commit()
            logger.warning(
                "face_job_requeue job_id=%s file_id=%s attempts=%s err=%s",
                job.id,
                drive_file.id[:12],
                job.attempts,
                msg[:160],
            )


class FaceWorkerLoop:
    """Background consumer for volume-less face runner replicas."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        settings: Settings | None = None,
        *,
        status_batcher: IndexStatusBatcher | None = None,
        embed_queue=None,
    ) -> None:
        self._session_factory = session_factory or get_session_factory()
        self._settings = settings or get_settings()
        self._status_batcher = status_batcher
        self._embed_queue = embed_queue
        self._token = uuid.uuid4().hex
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()

    def ensure_started(self) -> None:
        if self._tasks:
            return
        # Own batcher/embed queue when running as a dedicated face service.
        if self._status_batcher is None:
            self._status_batcher = IndexStatusBatcher(
                self._session_factory,
                batch_size=max(1, self._settings.index_status_batch_size),
            )
        if self._embed_queue is None and self._settings.gemini_api_key:
            from app.workers.embed_queue import ImageEmbedQueue

            self._embed_queue = ImageEmbedQueue(
                status_batcher=self._status_batcher,
                settings=self._settings,
            )
            self._embed_queue.ensure_started()

        n = max(1, min(4, int(self._settings.face_worker_concurrency)))
        for i in range(n):
            self._tasks.append(
                asyncio.create_task(self._worker_loop(i), name=f"face-worker-{i}")
            )
        logger.info(
            "FaceWorkerLoop started concurrency=%d lease_s=%d",
            n,
            self._settings.face_job_lease_seconds,
        )

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._status_batcher is not None:
            await self._status_batcher.flush()

    async def _worker_loop(self, slot: int) -> None:
        settings = self._settings
        while not self._stop.is_set():
            try:
                async with self._session_factory() as session:
                    jobs = await claim_face_jobs(
                        session,
                        limit=1,
                        lease_seconds=settings.face_job_lease_seconds,
                        worker_token=f"{self._token}:{slot}",
                    )
                    await session.commit()
                if not jobs:
                    await asyncio.sleep(1.0)
                    continue
                for job in jobs:
                    async with self._session_factory() as session:
                        fresh = await session.get(FaceJob, job.id)
                        if fresh is None:
                            continue
                        await process_face_job(
                            session,
                            fresh,
                            settings=settings,
                            status_batcher=self._status_batcher,
                            embed_queue=self._embed_queue,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("face_worker_loop_error slot=%s", slot)
                await asyncio.sleep(2.0)
