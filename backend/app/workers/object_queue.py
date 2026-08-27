"""Durable low-priority object queue using leases and SKIP LOCKED."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    DriveFile,
    FaceJob,
    FaceJobStatus,
    Media,
    MediaObjectLabel,
    ObjectJob,
    ObjectJobStatus,
    OcrPage,
    VideoSegment,
)
from app.db.session import get_session_factory
from app.objects.taxonomy import (
    OBJECT_MODEL_VERSION,
    TAXONOMY_VERSION,
    classify_text,
)

logger = logging.getLogger(__name__)
_MAX_ATTEMPTS = 3

_METRICS: dict[str, float | int | str | None] = {
    "completed": 0,
    "retried": 0,
    "errors": 0,
    "total_latency_ms": 0.0,
    "last_completed_at": None,
    "last_starved_at": None,
}


@dataclass(frozen=True)
class ObjectInput:
    job_id: int
    drive_file_id: str
    media_id: int
    text_samples: tuple[tuple[str, float | None], ...]


async def enqueue_object_job(
    session: AsyncSession,
    drive_file_id: str,
    *,
    model_version: str = OBJECT_MODEL_VERSION,
    force: bool = False,
) -> ObjectJob | None:
    """Idempotently enqueue missing/outdated media, optionally requeueing."""
    if not drive_file_id:
        return None
    existing = await session.scalar(
        select(ObjectJob).where(
            ObjectJob.drive_file_id == drive_file_id,
            ObjectJob.model_version == model_version,
        )
    )
    if existing is not None:
        if force and existing.status in (ObjectJobStatus.DONE, ObjectJobStatus.ERROR):
            existing.status = ObjectJobStatus.PENDING
            existing.attempts = 0
            existing.error_message = None
            existing.lock_token = None
            existing.locked_at = None
            existing.scan_completed_at = None
            existing.label_count = None
        return existing
    job = ObjectJob(
        drive_file_id=drive_file_id,
        model_version=model_version,
        status=ObjectJobStatus.PENDING,
    )
    session.add(job)
    await session.flush()
    return job


async def claim_object_jobs(
    session: AsyncSession,
    *,
    limit: int,
    lease_seconds: int = 300,
    worker_token: str | None = None,
) -> list[int]:
    """Claim a bounded batch; expired PROCESSING leases are retryable."""
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(seconds=max(60, lease_seconds))
    await session.execute(
        update(ObjectJob)
        .where(
            ObjectJob.status == ObjectJobStatus.PROCESSING,
            ObjectJob.model_version == OBJECT_MODEL_VERSION,
            ObjectJob.locked_at < stale_before,
            ObjectJob.attempts >= _MAX_ATTEMPTS,
        )
        .values(
            status=ObjectJobStatus.ERROR,
            lock_token=None,
            locked_at=None,
            error_message="lease_expired",
        )
    )
    await session.execute(
        update(ObjectJob)
        .where(
            ObjectJob.status == ObjectJobStatus.PROCESSING,
            ObjectJob.model_version == OBJECT_MODEL_VERSION,
            ObjectJob.locked_at < stale_before,
            ObjectJob.attempts < _MAX_ATTEMPTS,
        )
        .values(status=ObjectJobStatus.PENDING, lock_token=None, locked_at=None)
    )
    result = await session.execute(
        text(
            """
            UPDATE object_jobs
            SET status = 'PROCESSING', lock_token = :token, locked_at = :now,
                attempts = attempts + 1, updated_at = :now
            WHERE id IN (
                SELECT id FROM object_jobs
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
            "limit": max(1, min(64, int(limit))),
            "model_version": OBJECT_MODEL_VERSION,
        },
    )
    return [int(row[0]) for row in result.fetchall()]


async def _load_inputs(
    session_factory: async_sessionmaker[AsyncSession],
    job_ids: list[int],
) -> tuple[list[ObjectInput], list[int]]:
    """Read compact DB snapshots; caller closes the session before vector work."""
    inputs: list[ObjectInput] = []
    missing: list[int] = []
    async with session_factory() as session:
        jobs = list(
            (await session.execute(select(ObjectJob).where(ObjectJob.id.in_(job_ids))))
            .scalars()
            .all()
        )
        for job in jobs:
            media = await session.scalar(
                select(Media).where(Media.drive_file_id == job.drive_file_id)
            )
            if media is None:
                missing.append(job.id)
                continue
            samples: list[tuple[str, float | None]] = []
            segments = (
                await session.execute(
                    select(
                        VideoSegment.text,
                        VideoSegment.vlm_description,
                        VideoSegment.start_sec,
                    ).where(VideoSegment.media_id == media.id)
                )
            ).all()
            for row in segments:
                value = " ".join(part for part in (row.text, row.vlm_description) if part)
                if value.strip():
                    samples.append((value, float(row.start_sec)))
            pages = (
                await session.execute(
                    select(OcrPage.text, OcrPage.page_number).where(OcrPage.media_id == media.id)
                )
            ).all()
            samples.extend(
                (row.text, float(row.page_number))
                for row in pages if (row.text or "").strip()
            )
            inputs.append(
                ObjectInput(
                    job_id=job.id,
                    drive_file_id=job.drive_file_id,
                    media_id=media.id,
                    text_samples=tuple(samples),
                )
            )
    return inputs, missing


def _merge_labels(
    text_samples: tuple[tuple[str, float | None], ...],
    vector_labels: list[dict[str, object]],
    *,
    max_labels: int,
    confidence_floor: float,
) -> list[dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    for sample, timestamp in text_samples:
        for label in classify_text(sample):
            label["best_timestamp"] = timestamp
            key = str(label["canonical_label"])
            current = merged.get(key)
            if current is None:
                merged[key] = label
            else:
                current["hit_count"] = int(current["hit_count"]) + int(label["hit_count"])
                if float(label["confidence"]) > float(current["confidence"]):
                    current.update(label)
                    current["best_timestamp"] = timestamp
    for label in vector_labels:
        key = str(label["canonical_label"])
        current = merged.get(key)
        if current is None or float(label["confidence"]) > float(current["confidence"]):
            merged[key] = label
        elif current["evidence_source"] == "caption":
            current["hit_count"] = max(
                int(current["hit_count"]), int(label.get("hit_count") or 1)
            )
    labels = [
        label for label in merged.values()
        if float(label["confidence"]) >= confidence_floor
    ]
    labels.sort(key=lambda item: (-float(item["confidence"]), str(item["canonical_label"])))
    return labels[:max(1, max_labels)]


async def process_object_jobs(
    session_factory: async_sessionmaker[AsyncSession],
    job_ids: list[int],
    *,
    confidence_floor: float,
    max_labels: int,
) -> int:
    """Classify one claimed batch without holding DB sessions over network work."""
    started = time.monotonic()
    inputs, missing = await _load_inputs(session_factory, job_ids)

    # Qdrant caption and existing-vector retrieval happen after the DB snapshot closes.
    ids = [item.drive_file_id for item in inputs]
    captions: dict[str, str] = {}
    vectors: dict[str, list[tuple[float | None, list[float]]]] = {}
    fallback_vector_labels: dict[str, list[dict[str, object]]] = {}
    if ids:
        from app.qdrant.image_captions import get_captions_by_ids_sync
        from app.objects.vectors import retrieve_media_vectors_sync

        caption_result, vector_result = await asyncio.gather(
            asyncio.to_thread(get_captions_by_ids_sync, ids),
            asyncio.to_thread(retrieve_media_vectors_sync, ids),
            return_exceptions=True,
        )
        if isinstance(caption_result, dict):
            captions = caption_result
        else:
            logger.warning("object_caption_read_failed: %s", caption_result)
        if isinstance(vector_result, dict):
            vectors = vector_result
        else:
            logger.warning(
                "object_vector_retrieval_unsupported; using batched taxonomy queries: %s",
                vector_result,
            )
            try:
                from app.objects.vectors import classify_via_taxonomy_queries_sync

                fallback_vector_labels = await asyncio.to_thread(
                    classify_via_taxonomy_queries_sync,
                    ids,
                    confidence_floor=confidence_floor,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("object_taxonomy_query_fallback_failed; lexical only: %s", exc)

    results: dict[int, list[dict[str, object]]] = {}
    for item in inputs:
        samples = list(item.text_samples)
        caption = (captions.get(item.drive_file_id) or "").strip()
        if caption:
            samples.append((caption, None))
        vector_labels: list[dict[str, object]] = fallback_vector_labels.get(
            item.drive_file_id, []
        )
        if vectors.get(item.drive_file_id):
            try:
                from app.objects.vectors import classify_vectors

                vector_labels = await asyncio.to_thread(
                    classify_vectors,
                    vectors[item.drive_file_id],
                    confidence_floor=confidence_floor,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("object_vector_classification_failed file=%s: %s", item.drive_file_id, exc)
        results[item.job_id] = _merge_labels(
            tuple(samples),
            vector_labels,
            max_labels=max_labels,
            confidence_floor=confidence_floor,
        )

    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        media_ids = [item.media_id for item in inputs]
        if media_ids:
            # A forced refresh must replace the current model's output, otherwise
            # labels removed from a newer caption remain searchable forever.
            await session.execute(
                delete(MediaObjectLabel).where(
                    MediaObjectLabel.media_id.in_(media_ids),
                    MediaObjectLabel.model_version == OBJECT_MODEL_VERSION,
                )
            )
        for item in inputs:
            labels = results[item.job_id]
            for label in labels:
                stmt = insert(MediaObjectLabel).values(
                    media_id=item.media_id,
                    canonical_label=label["canonical_label"],
                    category=label["category"],
                    confidence=label["confidence"],
                    evidence_source=label["evidence_source"],
                    evidence_text=label.get("evidence_text"),
                    best_timestamp=label.get("best_timestamp"),
                    hit_count=label.get("hit_count", 1),
                    taxonomy_version=TAXONOMY_VERSION,
                    model_version=OBJECT_MODEL_VERSION,
                    updated_at=now,
                )
                await session.execute(
                    stmt.on_conflict_do_update(
                        constraint="uq_media_object_label_media_label_model",
                        set_={
                            "confidence": stmt.excluded.confidence,
                            "category": stmt.excluded.category,
                            "evidence_source": stmt.excluded.evidence_source,
                            "evidence_text": stmt.excluded.evidence_text,
                            "best_timestamp": stmt.excluded.best_timestamp,
                            "hit_count": stmt.excluded.hit_count,
                            "taxonomy_version": stmt.excluded.taxonomy_version,
                            "updated_at": now,
                        },
                    )
                )
            await session.execute(
                update(ObjectJob)
                .where(ObjectJob.id == item.job_id)
                .values(
                    status=ObjectJobStatus.DONE,
                    lock_token=None,
                    locked_at=None,
                    error_message=None,
                    scan_completed_at=now,
                    label_count=len(labels),
                )
            )
        if missing:
            await session.execute(
                update(ObjectJob)
                .where(ObjectJob.id.in_(missing))
                .values(
                    status=ObjectJobStatus.ERROR,
                    lock_token=None,
                    locked_at=None,
                    error_message="media_missing",
                )
            )
        await session.commit()

    elapsed_ms = (time.monotonic() - started) * 1000
    _METRICS["completed"] = int(_METRICS["completed"]) + len(inputs)
    _METRICS["errors"] = int(_METRICS["errors"]) + len(missing)
    _METRICS["total_latency_ms"] = float(_METRICS["total_latency_ms"]) + elapsed_ms
    _METRICS["last_completed_at"] = now.isoformat()
    return len(inputs)


async def fail_object_jobs(
    session_factory: async_sessionmaker[AsyncSession],
    job_ids: list[int],
    error: Exception,
) -> None:
    async with session_factory() as session:
        jobs = list(
            (await session.execute(select(ObjectJob).where(ObjectJob.id.in_(job_ids))))
            .scalars().all()
        )
        for job in jobs:
            job.lock_token = None
            job.locked_at = None
            job.error_message = str(error)[:500]
            if job.attempts >= _MAX_ATTEMPTS:
                job.status = ObjectJobStatus.ERROR
                _METRICS["errors"] = int(_METRICS["errors"]) + 1
            else:
                job.status = ObjectJobStatus.PENDING
                _METRICS["retried"] = int(_METRICS["retried"]) + 1
        await session.commit()


async def object_queue_status(
    session: AsyncSession,
) -> dict[str, object]:
    rows = (
        await session.execute(
            select(ObjectJob.status, func.count(ObjectJob.id))
            .where(ObjectJob.model_version == OBJECT_MODEL_VERSION)
            .group_by(ObjectJob.status)
        )
    ).all()
    counts = {
        (status.value if hasattr(status, "value") else str(status)).lower(): int(count)
        for status, count in rows
    }
    retries = int(
        await session.scalar(
            select(
                func.coalesce(
                    func.sum(func.greatest(ObjectJob.attempts - 1, 0)),
                    0,
                )
            ).where(ObjectJob.model_version == OBJECT_MODEL_VERSION)
        )
        or 0
    )
    completed_stats = (
        await session.execute(
            select(
                func.avg(
                    func.extract(
                        "epoch",
                        ObjectJob.updated_at - ObjectJob.created_at,
                    )
                    * 1000
                ),
                func.max(ObjectJob.scan_completed_at),
            ).where(
                ObjectJob.model_version == OBJECT_MODEL_VERSION,
                ObjectJob.status == ObjectJobStatus.DONE,
            )
        )
    ).one()
    completed = counts.get("done", 0)
    average_latency_ms = float(completed_stats[0] or 0.0)
    last_completed_at = completed_stats[1]
    return {
        "counts": counts,
        "depth": counts.get("pending", 0),
        "throughput_completed": completed,
        "retries": retries,
        "errors": counts.get("error", 0),
        "average_latency_ms": round(average_latency_ms, 1),
        "last_completed_at": last_completed_at.isoformat() if last_completed_at else None,
        "last_starved_at": _METRICS["last_starved_at"],
        "model_version": OBJECT_MODEL_VERSION,
    }


async def produce_object_backfill(
    session: AsyncSession,
    *,
    limit: int = 500,
    dry_run: bool = False,
) -> dict[str, int | bool]:
    """Count/enqueue only media missing the current model, respecting pauses."""
    from app.drive.indexing_pause import (
        global_indexing_is_paused,
        load_paused_folder_paths,
    )

    if await global_indexing_is_paused(session):
        return {"paused": True, "eligible": 0, "enqueued": 0}
    paused_paths = await load_paused_folder_paths(session)
    query = (
        select(DriveFile.id)
        .join(Media, Media.drive_file_id == DriveFile.id)
        .outerjoin(
            ObjectJob,
            (ObjectJob.drive_file_id == DriveFile.id)
            & (ObjectJob.model_version == OBJECT_MODEL_VERSION),
        )
        .where(ObjectJob.id.is_(None))
    )
    for paused_path in paused_paths:
        query = query.where(
            DriveFile.path != paused_path,
            ~DriveFile.path.startswith(paused_path + "/", autoescape=True),
        )
    eligible = list(
        (
            await session.execute(
                query.order_by(Media.id).limit(max(1, min(5000, int(limit))))
            )
        ).scalars()
    )
    if dry_run:
        return {"paused": False, "eligible": len(eligible), "enqueued": 0}
    if not eligible:
        return {"paused": False, "eligible": 0, "enqueued": 0}
    result = await session.execute(
        insert(ObjectJob)
        .values(
            [
                {
                    "drive_file_id": fid,
                    "model_version": OBJECT_MODEL_VERSION,
                    "status": ObjectJobStatus.PENDING,
                }
                for fid in eligible
            ]
        )
        .on_conflict_do_nothing(constraint="uq_object_job_file_model")
    )
    await session.commit()
    return {
        "paused": False,
        "eligible": len(eligible),
        "enqueued": int(result.rowcount or 0),
    }


async def requeue_object_jobs(
    session: AsyncSession,
    *,
    include_done: bool = False,
) -> int:
    statuses = [ObjectJobStatus.ERROR]
    if include_done:
        statuses.append(ObjectJobStatus.DONE)
    result = await session.execute(
        update(ObjectJob)
        .where(
            ObjectJob.model_version == OBJECT_MODEL_VERSION,
            ObjectJob.status.in_(statuses),
        )
        .values(
            status=ObjectJobStatus.PENDING,
            attempts=0,
            lock_token=None,
            locked_at=None,
            error_message=None,
        )
    )
    await session.commit()
    return int(result.rowcount or 0)


async def face_work_pending(session: AsyncSession) -> bool:
    return bool(
        await session.scalar(
            select(FaceJob.id)
            .where(FaceJob.status.in_((FaceJobStatus.PENDING, FaceJobStatus.PROCESSING)))
            .limit(1)
        )
    )


class ObjectWorkerLoop:
    """One bounded side lane per face-worker process, with strict face priority."""

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
            self._task = asyncio.create_task(self._run(), name="object-side-lane")

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
                    if not runtime.object_lane_enabled:
                        face_priority_checks = 0
                        await session.rollback()
                        await asyncio.sleep(5)
                        continue
                    if await face_work_pending(session):
                        _METRICS["last_starved_at"] = datetime.now(timezone.utc).isoformat()
                        face_priority_checks += 1
                        if face_priority_checks < runtime.object_face_priority_ratio:
                            await session.rollback()
                            await asyncio.sleep(1)
                            continue
                    face_priority_checks = 0
                    job_ids = await claim_object_jobs(
                        session,
                        limit=runtime.object_batch_size,
                        worker_token=self._token,
                    )
                    await session.commit()
                if not job_ids:
                    if runtime.object_backfill_enabled:
                        async with self._session_factory() as producer_session:
                            produced = await produce_object_backfill(
                                producer_session,
                                limit=runtime.object_batch_size * 10,
                            )
                        if int(produced.get("enqueued", 0)):
                            continue
                    await asyncio.sleep(2)
                    continue
                try:
                    await process_object_jobs(
                        self._session_factory,
                        job_ids,
                        confidence_floor=runtime.object_confidence_floor,
                        max_labels=runtime.object_max_labels,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("object_batch_failed count=%d", len(job_ids))
                    await fail_object_jobs(self._session_factory, job_ids, exc)
                # The configured ratio bounds consecutive object batches before faces recheck.
                await asyncio.sleep(
                    min(5.0, 0.1 * max(1, runtime.object_face_priority_ratio))
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("object_worker_loop_error")
                await asyncio.sleep(3)
