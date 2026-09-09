"""Idle OCR enrich lane for face-workers. Default off; never preempts face jobs."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db.models import (
    DriveFile,
    Face,
    Media,
    MediaOcrSpan,
    OcrJob,
    OcrJobStatus,
)
from app.db.session import get_session_factory
from app.drive.media_cache import (
    media_cache_dir,
    read_cached_bytes,
)
from app.ocr.rapidocr_engine import (
    OCR_MODEL_VERSION,
    BBox,
    classify_ocr_region,
    normalize_ocr_text,
    run_rapidocr_bgr,
    torso_from_face,
)
from app.pipelines.common import decode_image_bgr
from app.workers.object_queue import face_work_pending

logger = logging.getLogger(__name__)
_MAX_ATTEMPTS = 3

_METRICS: dict[str, float | int | str | None] = {
    "completed": 0,
    "errors": 0,
    "last_completed_at": None,
    "last_starved_at": None,
}


async def enqueue_ocr_job(
    session: AsyncSession,
    drive_file_id: str,
    *,
    model_version: str = OCR_MODEL_VERSION,
    force: bool = False,
) -> OcrJob | None:
    if not drive_file_id:
        return None
    existing = await session.scalar(
        select(OcrJob).where(
            OcrJob.drive_file_id == drive_file_id,
            OcrJob.model_version == model_version,
        )
    )
    if existing is not None:
        if force and existing.status in (OcrJobStatus.DONE, OcrJobStatus.ERROR):
            existing.status = OcrJobStatus.PENDING
            existing.attempts = 0
            existing.error_message = None
            existing.lock_token = None
            existing.locked_at = None
            existing.scan_completed_at = None
            existing.span_count = None
        return existing
    job = OcrJob(
        drive_file_id=drive_file_id,
        model_version=model_version,
        status=OcrJobStatus.PENDING,
    )
    session.add(job)
    await session.flush()
    return job


async def claim_ocr_jobs(
    session: AsyncSession,
    *,
    limit: int,
    lease_seconds: int = 600,
    worker_token: str | None = None,
) -> list[int]:
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(seconds=max(60, lease_seconds))
    await session.execute(
        update(OcrJob)
        .where(
            OcrJob.status == OcrJobStatus.PROCESSING,
            OcrJob.model_version == OCR_MODEL_VERSION,
            OcrJob.locked_at < stale_before,
            OcrJob.attempts >= _MAX_ATTEMPTS,
        )
        .values(
            status=OcrJobStatus.ERROR,
            lock_token=None,
            locked_at=None,
            error_message="lease_expired",
        )
    )
    await session.execute(
        update(OcrJob)
        .where(
            OcrJob.status == OcrJobStatus.PROCESSING,
            OcrJob.model_version == OCR_MODEL_VERSION,
            OcrJob.locked_at < stale_before,
            OcrJob.attempts < _MAX_ATTEMPTS,
        )
        .values(status=OcrJobStatus.PENDING, lock_token=None, locked_at=None)
    )
    result = await session.execute(
        text(
            """
            UPDATE ocr_jobs
            SET status = 'PROCESSING', lock_token = :token, locked_at = :now,
                attempts = attempts + 1, updated_at = :now
            WHERE id IN (
                SELECT id FROM ocr_jobs
                WHERE status = 'PENDING' AND model_version = :model_version
                ORDER BY created_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT :limit
            )
            RETURNING id
            """
        ),
        {
            "token": worker_token or uuid.uuid4().hex,
            "now": now,
            "limit": max(1, min(8, int(limit))),
            "model_version": OCR_MODEL_VERSION,
        },
    )
    return [int(row[0]) for row in result.fetchall()]


async def persist_ocr_spans_for_media(
    session: AsyncSession,
    *,
    media_id: int,
    image_bgr,
    faces: list[Face],
    confidence_floor: float,
) -> int:
    """Replace spans for this model_version. Safe to call from worker or testv1."""
    h, w = int(image_bgr.shape[0]), int(image_bgr.shape[1])
    torsos = [
        torso_from_face(
            BBox(x=f.bbox_x, y=f.bbox_y, w=f.bbox_width, h=f.bbox_height),
            image_w=float(w),
            image_h=float(h),
        )
        for f in faces
    ]
    boxes = run_rapidocr_bgr(image_bgr)
    await session.execute(
        delete(MediaOcrSpan).where(
            MediaOcrSpan.media_id == media_id,
            MediaOcrSpan.model_version == OCR_MODEL_VERSION,
        )
    )
    kept = 0
    for box in boxes:
        if box.confidence < confidence_floor:
            continue
        region = classify_ocr_region(
            BBox(x=box.x, y=box.y, w=box.w, h=box.h),
            torsos=torsos,
            image_h=float(h),
            image_w=float(w),
        )
        near_face_id = None
        if faces and region in {"torso", "upper"}:
            # Nearest face by vertical distance to span center.
            cy = box.y + box.h * 0.5
            best = min(
                faces,
                key=lambda f: abs((f.bbox_y + f.bbox_height) - cy),
            )
            near_face_id = best.id
        session.add(
            MediaOcrSpan(
                media_id=media_id,
                text=box.text[:512],
                normalized_text=normalize_ocr_text(box.text)[:512],
                confidence=box.confidence,
                bbox_x=box.x,
                bbox_y=box.y,
                bbox_width=box.w,
                bbox_height=box.h,
                region=region,
                near_face_id=near_face_id,
                model_version=OCR_MODEL_VERSION,
            )
        )
        kept += 1
    return kept


async def process_ocr_jobs(session: AsyncSession, job_ids: list[int]) -> None:
    if not job_ids:
        return
    settings = get_settings()
    runtime_floor = 0.45
    try:
        from app.runtime_settings import get_runtime_settings

        runtime_floor = float(get_runtime_settings().ocr_confidence_floor)
    except Exception:  # noqa: BLE001
        pass

    client = None
    try:
        from app.dependencies import get_drive_client

        client = get_drive_client()
    except Exception:  # noqa: BLE001
        client = None

    jobs = list(
        (
            await session.execute(select(OcrJob).where(OcrJob.id.in_(job_ids)))
        ).scalars().all()
    )
    for job in jobs:
        try:
            drive_file = await session.get(DriveFile, job.drive_file_id)
            if drive_file is None or not (drive_file.mime_type or "").startswith("image/"):
                job.status = OcrJobStatus.DONE
                job.span_count = 0
                job.scan_completed_at = datetime.now(timezone.utc)
                continue
            media = await session.scalar(
                select(Media).where(Media.drive_file_id == drive_file.id)
            )
            if media is None:
                raise RuntimeError("ocr_missing_media")

            cache_path: Path | None = None
            if drive_file.cache_rel_path:
                root = media_cache_dir(settings)
                candidate = root / drive_file.cache_rel_path
                if candidate.is_file():
                    cache_path = candidate
            if cache_path is None and client is not None:
                from app.drive.media_cache import ensure_media_cached

                # Idle face-worker only — bounded redownload for OCR enrich.
                cache_path = await ensure_media_cached(
                    client,
                    drive_file,
                    settings,
                    allow_redownload=True,
                )
            if cache_path is None or not Path(cache_path).is_file():
                job.status = OcrJobStatus.ERROR
                job.error_message = "cache_miss"
                job.lock_token = None
                job.locked_at = None
                _METRICS["errors"] = int(_METRICS.get("errors") or 0) + 1
                continue

            raw = read_cached_bytes(Path(cache_path))
            image_bgr = decode_image_bgr(raw, file_name=drive_file.name or "")
            faces = list(
                (
                    await session.execute(select(Face).where(Face.media_id == media.id))
                ).scalars().all()
            )
            count = await persist_ocr_spans_for_media(
                session,
                media_id=media.id,
                image_bgr=image_bgr,
                faces=faces,
                confidence_floor=runtime_floor,
            )
            job.status = OcrJobStatus.DONE
            job.span_count = count
            job.scan_completed_at = datetime.now(timezone.utc)
            job.error_message = None
            job.lock_token = None
            job.locked_at = None
            _METRICS["completed"] = int(_METRICS.get("completed") or 0) + 1
            _METRICS["last_completed_at"] = datetime.now(timezone.utc).isoformat()
        except Exception as exc:  # noqa: BLE001
            logger.warning("ocr_job_failed id=%s err=%s", job.id, exc)
            if job.attempts >= _MAX_ATTEMPTS:
                job.status = OcrJobStatus.ERROR
                job.error_message = str(exc)[:500]
            else:
                job.status = OcrJobStatus.PENDING
                job.error_message = str(exc)[:500]
            job.lock_token = None
            job.locked_at = None
            _METRICS["errors"] = int(_METRICS.get("errors") or 0) + 1


async def produce_ocr_backfill(session: AsyncSession, *, limit: int = 40) -> dict:
    """Enqueue image media that lack OCR spans for the current model version."""
    rows = (
        await session.execute(
            text(
                """
                SELECT m.drive_file_id
                FROM media m
                JOIN drive_files d ON d.id = m.drive_file_id
                WHERE d.mime_type LIKE 'image/%%'
                  AND d.cache_rel_path IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM media_ocr_spans s
                    WHERE s.media_id = m.id AND s.model_version = :model_version
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM ocr_jobs j
                    WHERE j.drive_file_id = m.drive_file_id
                      AND j.model_version = :model_version
                      AND j.status IN ('PENDING', 'PROCESSING', 'DONE')
                  )
                ORDER BY m.id DESC
                LIMIT :limit
                """
            ),
            {"model_version": OCR_MODEL_VERSION, "limit": max(1, min(200, limit))},
        )
    ).fetchall()
    enqueued = 0
    for (drive_file_id,) in rows:
        job = await enqueue_ocr_job(session, drive_file_id)
        if job is not None and job.status == OcrJobStatus.PENDING:
            enqueued += 1
    await session.commit()
    return {"enqueued": enqueued}


class OcrWorkerLoop:
    """Low-priority OCR side lane. Sleeps unless ocr_lane_enabled; yields to faces."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._session_factory = session_factory or get_session_factory()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._token = uuid.uuid4().hex

    def ensure_started(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="ocr-side-lane")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        from app.db.app_settings_store import refresh_runtime_settings_from_db

        face_priority_checks = 0
        while not self._stop.is_set():
            try:
                async with self._session_factory() as session:
                    runtime = await refresh_runtime_settings_from_db(session)
                    if not runtime.ocr_lane_enabled:
                        face_priority_checks = 0
                        await session.rollback()
                        await asyncio.sleep(8)
                        continue
                    if await face_work_pending(session):
                        _METRICS["last_starved_at"] = datetime.now(timezone.utc).isoformat()
                        face_priority_checks += 1
                        if face_priority_checks < runtime.ocr_face_priority_ratio:
                            await session.rollback()
                            await asyncio.sleep(1)
                            continue
                    face_priority_checks = 0
                    job_ids = await claim_ocr_jobs(
                        session,
                        limit=runtime.ocr_batch_size,
                        worker_token=self._token,
                    )
                    await session.commit()
                if not job_ids:
                    if runtime.ocr_backfill_enabled:
                        async with self._session_factory() as producer_session:
                            await produce_ocr_backfill(
                                producer_session,
                                limit=runtime.ocr_batch_size * 10,
                            )
                    await asyncio.sleep(3)
                    continue
                async with self._session_factory() as work_session:
                    await process_ocr_jobs(work_session, job_ids)
                    await work_session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("ocr side-lane tick failed")
                await asyncio.sleep(5)
