"""Pauseable Qwen identify lane: Drive originals → SGLang photos → isolated labels.

Writes only identify_jobs and media_identify_labels. Never touches object_jobs
or media_object_labels.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db.models import (
    DriveFile,
    DriveFileStatus,
    IdentifyJob,
    IdentifyJobStatus,
    Media,
    MediaIdentifyLabel,
    MediaType,
)
from app.db.session import get_session_factory
from app.objects.identify_tags import (
    IDENTIFY_AND_CAPTION_PROMPT,
    QWEN_IDENTIFY_MODEL_VERSION,
    parse_identify_output,
    persist_rows,
)

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_DRIVE_URL_HINTS = ("drive.google.com", "docs.google.com", "googleapis.com/drive")
_WORKING_DIR_NAME = "identify_working"

_METRICS: dict[str, float | int | str | None] = {
    "completed": 0,
    "retried": 0,
    "errors": 0,
    "total_latency_ms": 0.0,
    "total_gpu_ms": 0.0,
    "last_completed_at": None,
    "last_starved_at": None,
    "last_gpu_ms": None,
    "last_jpeg_kb": None,
    "working_set_files": 0,
    "gpu_in_flight": 0,
    "fetch_in_flight": 0,
    "ready_queue": 0,
    "persist_queue": 0,
}


@dataclass(frozen=True)
class IdentifyInput:
    job_id: int
    drive_file_id: str
    media_id: int
    name: str
    mime_type: str
    path: str
    size: int | None


@dataclass(frozen=True)
class _ReadyIdentify:
    item: IdentifyInput
    jpeg_bytes: bytes
    fetch_ms: float
    started: float


@dataclass(frozen=True)
class _PersistIdentify:
    item: IdentifyInput
    parsed: object
    gpu_ms: float
    jpeg_kb: float
    started: float


def identify_working_dir(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return Path(settings.temp_dir) / _WORKING_DIR_NAME


def wipe_identify_working_dir(directory: Path | None = None) -> int:
    """Delete leftover identify JPEGs. Returns how many files were removed."""
    root = directory or identify_working_dir()
    if not root.is_dir():
        _METRICS["working_set_files"] = 0
        return 0
    removed = 0
    for child in root.iterdir():
        try:
            if child.is_file():
                child.unlink()
                removed += 1
            elif child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        except OSError:
            logger.warning("identify_temp_unlink_failed path=%s", child)
    _METRICS["working_set_files"] = _count_working_files(root)
    return removed


def _count_working_files(directory: Path | None = None) -> int:
    root = directory or identify_working_dir()
    if not root.is_dir():
        return 0
    return sum(1 for child in root.iterdir() if child.is_file())


def sglang_base_url(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return (settings.qwen_identify_base_url or settings.qwen_vlm_base_url or "").rstrip("/")


def build_identify_payload(
    jpeg_bytes: bytes,
    *,
    model: str,
    prompt: str = IDENTIFY_AND_CAPTION_PROMPT,
    max_tokens: int = 1536,
) -> dict[str, object]:
    """One photo as a JPEG data URL. Never a Drive HTTP URL."""
    b64 = base64.b64encode(jpeg_bytes).decode("ascii")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }
    if payload_contains_drive_url(payload):
        raise ValueError("identify payload must not contain Drive URLs")
    return payload


def payload_contains_drive_url(payload: object) -> bool:
    blob = str(payload).casefold()
    return any(hint in blob for hint in _DRIVE_URL_HINTS)


def identify_metrics_path(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return Path(settings.temp_dir) / "identify_metrics.json"


def _publish_metrics() -> None:
    path = identify_metrics_path()
    tmp = path.with_suffix(".tmp")
    payload = {
        "gpu_in_flight": int(_METRICS["gpu_in_flight"] or 0),
        "fetch_in_flight": int(_METRICS["fetch_in_flight"] or 0),
        "ready_queue": int(_METRICS["ready_queue"] or 0),
        "persist_queue": int(_METRICS["persist_queue"] or 0),
        "last_gpu_ms": _METRICS["last_gpu_ms"],
        "last_jpeg_kb": _METRICS["last_jpeg_kb"],
        "completed": int(_METRICS["completed"] or 0),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.debug("identify_metrics_publish_failed path=%s", path)


def _read_published_metrics() -> dict[str, object]:
    path = identify_metrics_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def encode_identify_jpeg(
    raw_bytes: bytes,
    *,
    file_name: str,
    max_edge: int,
    quality: int,
    max_bytes: int,
) -> bytes:
    """Convert a Drive original to identify-quality JPEG bytes (no disk)."""
    from PIL import Image

    from app.pipelines.common import open_image_rgb

    img = open_image_rgb(raw_bytes, file_name=file_name)
    width, height = img.size
    edge = max(width, height)
    if max_edge > 0 and edge > max_edge:
        scale = max_edge / float(edge)
        img = img.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
            Image.Resampling.LANCZOS,
        )
    jpeg_quality = max(40, min(95, int(quality)))
    data = b""
    while jpeg_quality >= 40:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=jpeg_quality)
        data = buf.getvalue()
        if len(data) <= max_bytes:
            break
        jpeg_quality -= 10
    return data


def write_identify_jpeg(
    raw_bytes: bytes,
    dest: Path,
    *,
    file_name: str,
    max_edge: int,
    quality: int,
    max_bytes: int,
) -> None:
    """Convert a Drive original to identify-quality JPEG and write dest."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(
        encode_identify_jpeg(
            raw_bytes,
            file_name=file_name,
            max_edge=max_edge,
            quality=quality,
            max_bytes=max_bytes,
        )
    )


def unlink_quietly(path: Path | str | None) -> None:
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        logger.warning("identify_jpeg_unlink_failed path=%s", path)


async def enqueue_identify_job(
    session: AsyncSession,
    drive_file_id: str,
    *,
    model_version: str = QWEN_IDENTIFY_MODEL_VERSION,
    force: bool = False,
) -> IdentifyJob | None:
    if not drive_file_id:
        return None
    existing = await session.scalar(
        select(IdentifyJob).where(
            IdentifyJob.drive_file_id == drive_file_id,
            IdentifyJob.model_version == model_version,
        )
    )
    if existing is not None:
        if force and existing.status in (IdentifyJobStatus.DONE, IdentifyJobStatus.ERROR):
            existing.status = IdentifyJobStatus.PENDING
            existing.attempts = 0
            existing.error_message = None
            existing.lock_token = None
            existing.locked_at = None
            existing.scan_completed_at = None
            existing.label_count = None
        return existing
    job = IdentifyJob(
        drive_file_id=drive_file_id,
        model_version=model_version,
        status=IdentifyJobStatus.PENDING,
    )
    session.add(job)
    await session.flush()
    return job


async def claim_identify_jobs(
    session: AsyncSession,
    *,
    limit: int,
    lease_seconds: int = 180,
    worker_token: str | None = None,
    model_version: str = QWEN_IDENTIFY_MODEL_VERSION,
) -> list[int]:
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(seconds=max(60, lease_seconds))
    await session.execute(
        update(IdentifyJob)
        .where(
            IdentifyJob.status == IdentifyJobStatus.PROCESSING,
            IdentifyJob.model_version == model_version,
            IdentifyJob.locked_at < stale_before,
            IdentifyJob.attempts >= _MAX_ATTEMPTS,
        )
        .values(
            status=IdentifyJobStatus.ERROR,
            lock_token=None,
            locked_at=None,
            error_message="lease_expired",
        )
    )
    await session.execute(
        update(IdentifyJob)
        .where(
            IdentifyJob.status == IdentifyJobStatus.PROCESSING,
            IdentifyJob.model_version == model_version,
            IdentifyJob.locked_at < stale_before,
            IdentifyJob.attempts < _MAX_ATTEMPTS,
        )
        .values(status=IdentifyJobStatus.PENDING, lock_token=None, locked_at=None)
    )
    result = await session.execute(
        text(
            """
            UPDATE identify_jobs
            SET status = 'PROCESSING', lock_token = :token, locked_at = :now,
                attempts = attempts + 1, updated_at = :now
            WHERE id IN (
                SELECT id FROM identify_jobs
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
            "limit": max(1, min(1000, int(limit))),
            "model_version": model_version,
        },
    )
    return [int(row[0]) for row in result.fetchall()]


async def recover_identify_processing(
    session: AsyncSession,
    *,
    model_version: str = QWEN_IDENTIFY_MODEL_VERSION,
) -> int:
    """Return PROCESSING jobs to PENDING after a worker restart (leader only)."""
    result = await session.execute(
        update(IdentifyJob)
        .where(
            IdentifyJob.status == IdentifyJobStatus.PROCESSING,
            IdentifyJob.model_version == model_version,
        )
        .values(
            status=IdentifyJobStatus.PENDING,
            lock_token=None,
            locked_at=None,
            attempts=func.greatest(IdentifyJob.attempts - 1, 0),
        )
    )
    return int(result.rowcount or 0)


async def _load_input(
    session: AsyncSession,
    job_id: int,
) -> IdentifyInput | None:
    loaded = await _load_inputs(session, [job_id])
    return loaded[0] if loaded else None


async def _load_inputs(
    session: AsyncSession,
    job_ids: list[int],
) -> list[IdentifyInput]:
    if not job_ids:
        return []
    rows = (
        await session.execute(
            select(IdentifyJob, Media, DriveFile)
            .join(Media, Media.drive_file_id == IdentifyJob.drive_file_id)
            .join(DriveFile, DriveFile.id == IdentifyJob.drive_file_id)
            .where(IdentifyJob.id.in_(job_ids))
        )
    ).all()
    by_id: dict[int, IdentifyInput] = {}
    for job, media, drive_file in rows:
        by_id[int(job.id)] = IdentifyInput(
            job_id=int(job.id),
            drive_file_id=job.drive_file_id,
            media_id=int(media.id),
            name=drive_file.name or "",
            mime_type=drive_file.mime_type or "",
            path=drive_file.path or "",
            size=drive_file.size,
        )
    missing = [job_id for job_id in job_ids if job_id not in by_id]
    for job_id in missing:
        await _mark_job(
            session,
            job_id,
            status=IdentifyJobStatus.ERROR,
            error_message="media_missing",
        )
        _METRICS["errors"] = int(_METRICS["errors"]) + 1
    return [by_id[job_id] for job_id in job_ids if job_id in by_id]


async def persist_identify_labels(
    session: AsyncSession,
    media_id: int,
    parsed,
    *,
    model_version: str = QWEN_IDENTIFY_MODEL_VERSION,
    best_timestamp: float | None = None,
    replace: bool = True,
) -> int:
    """Persist Qwen labels, optionally preserving timestamped video evidence."""
    model_version = str(model_version or QWEN_IDENTIFY_MODEL_VERSION)[:96]
    if replace:
        await session.execute(
            delete(MediaIdentifyLabel).where(
                MediaIdentifyLabel.media_id == media_id,
                MediaIdentifyLabel.model_version == model_version,
            )
        )
    now = datetime.now(timezone.utc)
    rows = persist_rows(parsed)
    if not rows:
        return 0
    payload = []
    for row in rows:
        label = str(row["canonical_label"] or "").strip()[:96]
        if not label:
            continue
        evidence = row.get("evidence_text")
        payload.append(
            {
                "media_id": media_id,
                "canonical_label": label,
                "category": str(row["category"] or "object")[:48],
                "confidence": float(row["confidence"]),
                "evidence_source": str(row["evidence_source"] or "qwen_identify")[:32],
                "evidence_text": (str(evidence)[:240] if evidence else None),
                "best_timestamp": best_timestamp,
                "hit_count": int(row.get("hit_count", 1) or 1),
                "model_version": str(model_version or QWEN_IDENTIFY_MODEL_VERSION)[:96],
                "updated_at": now,
            }
        )
    if not payload:
        return 0
    stmt = insert(MediaIdentifyLabel).values(payload)
    await session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_media_identify_label_media_label_model",
            set_={
                "confidence": stmt.excluded.confidence,
                "category": stmt.excluded.category,
                "evidence_source": stmt.excluded.evidence_source,
                "evidence_text": stmt.excluded.evidence_text,
                "best_timestamp": stmt.excluded.best_timestamp,
                "hit_count": stmt.excluded.hit_count,
                "updated_at": now,
            },
        )
    )
    return len(rows)


async def _mark_job(
    session: AsyncSession,
    job_id: int,
    *,
    status: IdentifyJobStatus,
    error_message: str | None = None,
    label_count: int | None = None,
) -> None:
    values: dict[str, object] = {
        "status": status,
        "lock_token": None,
        "locked_at": None,
        "error_message": error_message,
    }
    if status == IdentifyJobStatus.DONE:
        values["scan_completed_at"] = datetime.now(timezone.utc)
        values["label_count"] = label_count or 0
    await session.execute(update(IdentifyJob).where(IdentifyJob.id == job_id).values(**values))


async def fail_identify_job(
    session_factory: async_sessionmaker[AsyncSession],
    job_id: int,
    error: Exception | str,
    *,
    attempts: int | None = None,
) -> None:
    message = str(error)[:500]
    async with session_factory() as session:
        job = await session.get(IdentifyJob, job_id)
        if job is None:
            return
        job.lock_token = None
        job.locked_at = None
        job.error_message = message
        used = attempts if attempts is not None else job.attempts
        if used >= _MAX_ATTEMPTS:
            job.status = IdentifyJobStatus.ERROR
            _METRICS["errors"] = int(_METRICS["errors"]) + 1
        else:
            job.status = IdentifyJobStatus.PENDING
            _METRICS["retried"] = int(_METRICS["retried"]) + 1
        await session.commit()


async def release_identify_job(
    session_factory: async_sessionmaker[AsyncSession],
    job_id: int,
) -> None:
    """Return a claimed job to PENDING without counting a failure (pause)."""
    async with session_factory() as session:
        job = await session.get(IdentifyJob, job_id)
        if job is None:
            return
        if job.attempts > 0:
            job.attempts -= 1
        job.status = IdentifyJobStatus.PENDING
        job.lock_token = None
        job.locked_at = None
        job.error_message = None
        await session.commit()


async def identify_queue_status(session: AsyncSession) -> dict[str, object]:
    rows = (
        await session.execute(
            select(IdentifyJob.status, func.count(IdentifyJob.id))
            .where(IdentifyJob.model_version == QWEN_IDENTIFY_MODEL_VERSION)
            .group_by(IdentifyJob.status)
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
                    func.sum(func.greatest(IdentifyJob.attempts - 1, 0)),
                    0,
                )
            ).where(IdentifyJob.model_version == QWEN_IDENTIFY_MODEL_VERSION)
        )
        or 0
    )
    completed_stats = (
        await session.execute(
            select(
                func.avg(
                    func.extract(
                        "epoch",
                        IdentifyJob.updated_at - IdentifyJob.created_at,
                    )
                    * 1000
                ),
                func.max(IdentifyJob.scan_completed_at),
            ).where(
                IdentifyJob.model_version == QWEN_IDENTIFY_MODEL_VERSION,
                IdentifyJob.status == IdentifyJobStatus.DONE,
            )
        )
    ).one()
    last_completed_at = completed_stats[1]
    working_dir = identify_working_dir()
    working_set_files = _count_working_files(working_dir)
    _METRICS["working_set_files"] = working_set_files
    published = _read_published_metrics()
    gpu_in_flight = int(published.get("gpu_in_flight") or _METRICS["gpu_in_flight"] or 0)
    fetch_in_flight = int(published.get("fetch_in_flight") or _METRICS["fetch_in_flight"] or 0)
    ready_queue = int(published.get("ready_queue") or _METRICS["ready_queue"] or 0)
    persist_queue = int(published.get("persist_queue") or _METRICS["persist_queue"] or 0)
    last_gpu_ms = published.get("last_gpu_ms", _METRICS["last_gpu_ms"])
    last_jpeg_kb = published.get("last_jpeg_kb", _METRICS["last_jpeg_kb"])
    return {
        "counts": counts,
        "depth": counts.get("pending", 0),
        "processing": counts.get("processing", 0),
        "throughput_completed": counts.get("done", 0),
        "retries": retries,
        "errors": counts.get("error", 0),
        "average_latency_ms": round(float(completed_stats[0] or 0.0), 1),
        "last_completed_at": last_completed_at.isoformat() if last_completed_at else None,
        "last_starved_at": _METRICS["last_starved_at"],
        "last_gpu_ms": last_gpu_ms,
        "last_jpeg_kb": last_jpeg_kb,
        "working_set_files": working_set_files,
        "gpu_in_flight": gpu_in_flight,
        "fetch_in_flight": fetch_in_flight,
        "ready_queue": ready_queue,
        "persist_queue": persist_queue,
        "model_version": QWEN_IDENTIFY_MODEL_VERSION,
    }


async def produce_identify_backfill(
    session: AsyncSession,
    *,
    limit: int = 1000,
    dry_run: bool = False,
) -> dict[str, int | bool]:
    """Enqueue processed images missing a done/pending identify job."""
    from app.drive.indexing_pause import lane_paused_folder_paths

    paused_paths = await lane_paused_folder_paths(session)
    query = (
        select(DriveFile.id)
        .join(Media, Media.drive_file_id == DriveFile.id)
        .outerjoin(
            IdentifyJob,
            (IdentifyJob.drive_file_id == DriveFile.id)
            & (IdentifyJob.model_version == QWEN_IDENTIFY_MODEL_VERSION),
        )
        .where(
            IdentifyJob.id.is_(None),
            Media.type == MediaType.IMAGE,
            DriveFile.status == DriveFileStatus.PROCESSED,
        )
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
        insert(IdentifyJob)
        .values(
            [
                {
                    "drive_file_id": fid,
                    "model_version": QWEN_IDENTIFY_MODEL_VERSION,
                    "status": IdentifyJobStatus.PENDING,
                }
                for fid in eligible
            ]
        )
        .on_conflict_do_nothing(constraint="uq_identify_job_file_model")
    )
    await session.commit()
    return {
        "paused": False,
        "eligible": len(eligible),
        "enqueued": int(result.rowcount or 0),
    }


async def requeue_identify_jobs(
    session: AsyncSession,
    *,
    include_done: bool = False,
) -> int:
    statuses = [IdentifyJobStatus.ERROR]
    if include_done:
        statuses.append(IdentifyJobStatus.DONE)
    result = await session.execute(
        update(IdentifyJob)
        .where(
            IdentifyJob.model_version == QWEN_IDENTIFY_MODEL_VERSION,
            IdentifyJob.status.in_(statuses),
        )
        .values(
            status=IdentifyJobStatus.PENDING,
            attempts=0,
            lock_token=None,
            locked_at=None,
            error_message=None,
        )
    )
    await session.commit()
    return int(result.rowcount or 0)


async def _post_identify(
    client: httpx.AsyncClient,
    jpeg_bytes: bytes,
    settings: Settings,
) -> str:
    from app.qwen.runpod_serverless import identify_jpeg_runpod, runpod_qwen_configured

    if runpod_qwen_configured(settings):
        return await identify_jpeg_runpod(jpeg_bytes, settings)
    payload = build_identify_payload(
        jpeg_bytes,
        model=settings.qwen_identify_model or settings.qwen_vlm_model,
        max_tokens=settings.qwen_identify_max_tokens,
    )
    base = sglang_base_url(settings)
    if not base:
        raise RuntimeError("qwen identify base URL is not configured")
    if "proxy.runpod.net" in base:
        raise RuntimeError("refusing dedicated RunPod proxy; set RUNPOD_QWEN_ENDPOINT_ID")
    url = f"{base}/v1/chat/completions"
    resp = await client.post(url, json=payload, headers={"Authorization": "Bearer EMPTY"})
    resp.raise_for_status()
    data = resp.json()
    try:
        text_out = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected Qwen identify response: {data!r}") from exc
    return (text_out or "").strip()


async def process_identify_job(
    session_factory: async_sessionmaker[AsyncSession],
    job_id: int,
    *,
    gpu_sem: asyncio.Semaphore,
    decode_sem: asyncio.Semaphore,
    working_dir: Path,
    http: httpx.AsyncClient,
    lane_enabled: Callable[[], bool],
) -> None:
    """Download one original, send as a photo, persist isolated labels, delete JPEG."""
    from app.db.app_settings_store import refresh_runtime_settings_from_db
    from app.dependencies import get_drive_client
    from app.drive.indexing_pause import file_under_folder, lane_paused_folder_paths
    from app.pipelines.common import download_to_memory

    settings = get_settings()
    jpeg_path: Path | None = None
    started = time.monotonic()
    async with session_factory() as session:
        if not lane_enabled():
            await session.rollback()
            await release_identify_job(session_factory, job_id)
            return
        runtime = await refresh_runtime_settings_from_db(session)
        if not runtime.identify_lane_enabled:
            await session.rollback()
            await release_identify_job(session_factory, job_id)
            return
        item = await _load_input(session, job_id)
        if item is None:
            await _mark_job(
                session,
                job_id,
                status=IdentifyJobStatus.ERROR,
                error_message="media_missing",
            )
            await session.commit()
            _METRICS["errors"] = int(_METRICS["errors"]) + 1
            return
        paused_paths = await lane_paused_folder_paths(session)
        if any(file_under_folder(item.path, paused) for paused in paused_paths):
            await session.rollback()
            await release_identify_job(session_factory, job_id)
            return
        cap = settings.qwen_identify_download_cap_bytes
        if item.size and item.size > cap:
            await _mark_job(
                session,
                job_id,
                status=IdentifyJobStatus.ERROR,
                error_message="file_too_large",
            )
            await session.commit()
            _METRICS["errors"] = int(_METRICS["errors"]) + 1
            return

    try:
        if not lane_enabled():
            await release_identify_job(session_factory, job_id)
            return
        client = get_drive_client()
        raw = await download_to_memory(client, item.drive_file_id)
        if not lane_enabled():
            await release_identify_job(session_factory, job_id)
            return
        jpeg_path = working_dir / f"{item.drive_file_id}_{job_id}.jpg"
        async with decode_sem:
            await asyncio.to_thread(
                write_identify_jpeg,
                raw,
                jpeg_path,
                file_name=item.name,
                max_edge=settings.qwen_identify_max_edge,
                quality=settings.qwen_identify_jpeg_quality,
                max_bytes=settings.qwen_identify_max_bytes,
            )
        del raw
        _METRICS["working_set_files"] = _count_working_files(working_dir)
        if not lane_enabled():
            unlink_quietly(jpeg_path)
            await release_identify_job(session_factory, job_id)
            return
        jpeg_bytes = jpeg_path.read_bytes()
        gpu_started = time.monotonic()
        async with gpu_sem:
            _METRICS["gpu_in_flight"] = int(_METRICS["gpu_in_flight"] or 0) + 1
            _publish_metrics()
            try:
                text_out = await _post_identify(http, jpeg_bytes, settings)
            finally:
                _METRICS["gpu_in_flight"] = max(0, int(_METRICS["gpu_in_flight"] or 0) - 1)
                _publish_metrics()
        gpu_ms = (time.monotonic() - gpu_started) * 1000
        parsed = parse_identify_output(text_out)
        async with session_factory() as session:
            label_count = await persist_identify_labels(session, item.media_id, parsed)
            await session.commit()
        if parsed.caption:
            from app.search.images import index_image_caption_texts

            await index_image_caption_texts([(item.drive_file_id, parsed.caption)])
        async with session_factory() as session:
            await _mark_job(
                session,
                job_id,
                status=IdentifyJobStatus.DONE,
                label_count=label_count,
            )
            await session.commit()
        elapsed_ms = (time.monotonic() - started) * 1000
        _METRICS["completed"] = int(_METRICS["completed"]) + 1
        _METRICS["total_latency_ms"] = float(_METRICS["total_latency_ms"]) + elapsed_ms
        _METRICS["total_gpu_ms"] = float(_METRICS["total_gpu_ms"] or 0) + gpu_ms
        _METRICS["last_gpu_ms"] = round(gpu_ms, 1)
        _METRICS["last_jpeg_kb"] = round(len(jpeg_bytes) / 1024.0, 1)
        _METRICS["last_completed_at"] = datetime.now(timezone.utc).isoformat()
        _publish_metrics()
        logger.info(
            "identify_done file=%s labels=%d jpeg_kb=%.1f gpu_ms=%.0f wall_ms=%.0f",
            item.drive_file_id[:12],
            label_count,
            float(_METRICS["last_jpeg_kb"] or 0),
            gpu_ms,
            elapsed_ms,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("identify_job_failed id=%s", job_id)
        await fail_identify_job(session_factory, job_id, exc)
    finally:
        unlink_quietly(jpeg_path)
        _METRICS["working_set_files"] = _count_working_files(working_dir)


async def _download_identify_original(drive_file_id: str) -> bytes:
    """Drive original bytes. Identify fetch pool is the concurrency cap (not the global 8)."""
    from app.dependencies import get_drive_client
    from app.workers.index_errors import is_transient_network_error

    client = get_drive_client()
    last_exc: BaseException | None = None
    for attempt in range(1, 4):
        try:
            chunks: list[bytes] = []
            async with client.stream_file_content(drive_file_id) as response:
                async for chunk in response.aiter_bytes(chunk_size=1024 * 256):
                    chunks.append(chunk)
            return b"".join(chunks)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if not is_transient_network_error(exc) or attempt >= 3:
                raise
            logger.warning(
                "identify_download_retry %d/3 file_id=%s err=%s",
                attempt,
                drive_file_id[:12],
                type(exc).__name__,
            )
            await asyncio.sleep(0.4 * attempt)
    raise last_exc or RuntimeError("identify download failed")


class IdentifyWorkerLoop:
    """dfi-backend consumer: 16 fetch workers + 32 GPU posts, like the SGLang dry-run."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._session_factory = session_factory or get_session_factory()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._token = uuid.uuid4().hex
        self._pipeline: list[asyncio.Task] = []
        self._http: httpx.AsyncClient | None = None
        self._fetch_q: asyncio.Queue[IdentifyInput | None] | None = None
        self._ready_q: asyncio.Queue[_ReadyIdentify | None] | None = None
        self._persist_q: asyncio.Queue[_PersistIdentify | None] | None = None
        self._paused_paths: list[str] = []
        self._qwen_idle_ticks = 0

    def ensure_started(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="identify-qwen-lane")

    async def stop(self) -> None:
        self._stop.set()
        for task in list(self._pipeline):
            task.cancel()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, *list(self._pipeline), return_exceptions=True)
            self._task = None
        self._pipeline.clear()
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        wipe_identify_working_dir()

    def _lane_enabled(self) -> bool:
        if self._stop.is_set():
            return False
        from app.runtime_settings import get_runtime_settings

        return bool(get_runtime_settings().identify_lane_enabled)

    def _sync_queue_metrics(self) -> None:
        _METRICS["ready_queue"] = self._ready_q.qsize() if self._ready_q is not None else 0
        _METRICS["persist_queue"] = (
            self._persist_q.qsize() if self._persist_q is not None else 0
        )
        _publish_metrics()

    async def _run(self) -> None:
        settings = get_settings()
        working_dir = identify_working_dir(settings)
        working_dir.mkdir(parents=True, exist_ok=True)
        wipe_identify_working_dir(working_dir)
        fetch_n = max(1, settings.qwen_identify_fetch_concurrency)
        gpu_n = max(1, settings.qwen_identify_concurrency)
        from app.qwen.runpod_serverless import runpod_qwen_configured

        if runpod_qwen_configured(settings):
            gpu_n = min(8, gpu_n)
        persist_n = max(1, settings.qwen_identify_persist_concurrency)
        prefetch = max(gpu_n, settings.qwen_identify_prefetch)
        decode_sem = asyncio.Semaphore(min(16, fetch_n))
        self._fetch_q = asyncio.Queue(maxsize=fetch_n * 2)
        self._ready_q = asyncio.Queue(maxsize=prefetch)
        self._persist_q = asyncio.Queue(maxsize=gpu_n * 2)
        timeout = httpx.Timeout(settings.qwen_identify_timeout_seconds)
        limits = httpx.Limits(
            max_connections=max(16, gpu_n + 8),
            max_keepalive_connections=gpu_n,
        )
        self._http = httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, limits=limits
        )
        async with self._session_factory() as session:
            recovered = await recover_identify_processing(session)
            await session.commit()
        if recovered:
            logger.info("identify_recovered_processing count=%s", recovered)
        self._pipeline = [
            asyncio.create_task(
                self._fetch_worker(decode_sem), name=f"identify-fetch-{index}"
            )
            for index in range(fetch_n)
        ]
        self._pipeline.extend(
            asyncio.create_task(self._gpu_worker(), name=f"identify-gpu-{index}")
            for index in range(gpu_n)
        )
        self._pipeline.extend(
            asyncio.create_task(self._persist_worker(), name=f"identify-persist-{index}")
            for index in range(persist_n)
        )
        logger.info(
            "identify_pipeline_start fetch=%s gpu=%s persist=%s prefetch=%s",
            fetch_n,
            gpu_n,
            persist_n,
            prefetch,
        )
        try:
            while not self._stop.is_set():
                try:
                    await self._claim_tick()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.exception("identify_loop_tick_failed")
                    await asyncio.sleep(2)
        finally:
            for task in list(self._pipeline):
                task.cancel()
            await asyncio.gather(*self._pipeline, return_exceptions=True)
            self._pipeline.clear()
            if self._http is not None:
                await self._http.aclose()
                self._http = None
            wipe_identify_working_dir(working_dir)

    async def _claim_tick(self) -> None:
        from app.db.app_settings_store import refresh_runtime_settings_from_db
        from app.drive.indexing_pause import lane_paused_folder_paths

        settings = get_settings()
        fetch_q = self._fetch_q
        if fetch_q is None:
            await asyncio.sleep(0.2)
            return
        async with self._session_factory() as session:
            runtime = await refresh_runtime_settings_from_db(session)
            if not runtime.identify_lane_enabled:
                await session.rollback()
                await asyncio.sleep(5)
                return
            self._paused_paths = await lane_paused_folder_paths(session)
            free = fetch_q.maxsize - fetch_q.qsize()
            if free <= 0:
                await session.rollback()
                await asyncio.sleep(0.05)
                return
            job_ids = await claim_identify_jobs(
                session,
                limit=min(free, max(1, settings.qwen_identify_fetch_concurrency)),
                lease_seconds=settings.qwen_identify_lease_seconds,
                worker_token=self._token,
            )
            items = await _load_inputs(session, job_ids) if job_ids else []
            await session.commit()

        if not items:
            if runtime.identify_backfill_enabled:
                async with self._session_factory() as producer_session:
                    produced = await produce_identify_backfill(
                        producer_session,
                        limit=max(1, min(1000, settings.qwen_identify_working_set)),
                    )
                if int(produced.get("enqueued", 0)):
                    return
            busy = (
                fetch_q.qsize()
                or (self._ready_q.qsize() if self._ready_q is not None else 0)
                or int(_METRICS["gpu_in_flight"] or 0)
                or int(_METRICS["fetch_in_flight"] or 0)
                or int(_METRICS["persist_queue"] or 0)
            )
            if busy:
                self._qwen_idle_ticks = 0
                await asyncio.sleep(0.2)
            else:
                _METRICS["last_starved_at"] = datetime.now(timezone.utc).isoformat()
                from app.qwen.runpod_serverless import runpod_qwen_configured, set_qwen_workers_max

                self._qwen_idle_ticks += 1
                if self._qwen_idle_ticks >= 45 and runpod_qwen_configured(settings):
                    try:
                        await set_qwen_workers_max(settings, 0)
                    except Exception:  # noqa: BLE001
                        logger.warning("qwen_gpu_scale_zero_failed", exc_info=True)
                    self._qwen_idle_ticks = 0
                await asyncio.sleep(2)
            return

        for item in items:
            await fetch_q.put(item)
        self._qwen_idle_ticks = 0
        self._sync_queue_metrics()

    async def _fetch_worker(self, decode_sem: asyncio.Semaphore) -> None:
        from app.drive.indexing_pause import file_under_folder

        settings = get_settings()
        fetch_q = self._fetch_q
        ready_q = self._ready_q
        if fetch_q is None or ready_q is None:
            return
        while not self._stop.is_set():
            item = await fetch_q.get()
            if item is None:
                return
            started = time.monotonic()
            if not self._lane_enabled():
                await release_identify_job(self._session_factory, item.job_id)
                continue
            if any(file_under_folder(item.path, paused) for paused in self._paused_paths):
                await release_identify_job(self._session_factory, item.job_id)
                continue
            cap = settings.qwen_identify_download_cap_bytes
            if item.size and item.size > cap:
                async with self._session_factory() as session:
                    await _mark_job(
                        session,
                        item.job_id,
                        status=IdentifyJobStatus.ERROR,
                        error_message="file_too_large",
                    )
                    await session.commit()
                _METRICS["errors"] = int(_METRICS["errors"]) + 1
                continue
            _METRICS["fetch_in_flight"] = int(_METRICS["fetch_in_flight"] or 0) + 1
            _publish_metrics()
            try:
                raw = await _download_identify_original(item.drive_file_id)
                if not self._lane_enabled():
                    await release_identify_job(self._session_factory, item.job_id)
                    continue
                async with decode_sem:
                    jpeg_bytes = await asyncio.to_thread(
                        encode_identify_jpeg,
                        raw,
                        file_name=item.name,
                        max_edge=settings.qwen_identify_max_edge,
                        quality=settings.qwen_identify_jpeg_quality,
                        max_bytes=settings.qwen_identify_max_bytes,
                    )
                del raw
                await ready_q.put(
                    _ReadyIdentify(
                        item=item,
                        jpeg_bytes=jpeg_bytes,
                        fetch_ms=(time.monotonic() - started) * 1000,
                        started=started,
                    )
                )
                self._sync_queue_metrics()
            except asyncio.CancelledError:
                await release_identify_job(self._session_factory, item.job_id)
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("identify_fetch_failed id=%s", item.job_id)
                await fail_identify_job(self._session_factory, item.job_id, exc)
            finally:
                _METRICS["fetch_in_flight"] = max(
                    0, int(_METRICS["fetch_in_flight"] or 0) - 1
                )
                _publish_metrics()

    async def _gpu_worker(self) -> None:
        ready_q = self._ready_q
        persist_q = self._persist_q
        http = self._http
        if ready_q is None or persist_q is None or http is None:
            return
        settings = get_settings()
        while not self._stop.is_set():
            ready = await ready_q.get()
            if ready is None:
                return
            self._sync_queue_metrics()
            _METRICS["gpu_in_flight"] = int(_METRICS["gpu_in_flight"] or 0) + 1
            _publish_metrics()
            parsed = None
            gpu_ms = 0.0
            jpeg_kb = round(len(ready.jpeg_bytes) / 1024.0, 1)
            try:
                gpu_started = time.monotonic()
                text_out = await _post_identify(http, ready.jpeg_bytes, settings)
                gpu_ms = (time.monotonic() - gpu_started) * 1000
                _METRICS["last_gpu_ms"] = round(gpu_ms, 1)
                _METRICS["last_jpeg_kb"] = jpeg_kb
                parsed = parse_identify_output(text_out)
            except asyncio.CancelledError:
                await release_identify_job(self._session_factory, ready.item.job_id)
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("identify_gpu_failed id=%s", ready.item.job_id)
                await fail_identify_job(self._session_factory, ready.item.job_id, exc)
            finally:
                _METRICS["gpu_in_flight"] = max(0, int(_METRICS["gpu_in_flight"] or 0) - 1)
                _publish_metrics()
            if parsed is None:
                continue
            try:
                await persist_q.put(
                    _PersistIdentify(
                        item=ready.item,
                        parsed=parsed,
                        gpu_ms=gpu_ms,
                        jpeg_kb=jpeg_kb,
                        started=ready.started,
                    )
                )
                self._sync_queue_metrics()
            except asyncio.CancelledError:
                await release_identify_job(self._session_factory, ready.item.job_id)
                raise

    async def _persist_worker(self) -> None:
        persist_q = self._persist_q
        if persist_q is None:
            return
        while not self._stop.is_set():
            item = await persist_q.get()
            if item is None:
                return
            self._sync_queue_metrics()
            try:
                persist_started = time.monotonic()
                async with self._session_factory() as session:
                    label_count = await persist_identify_labels(
                        session, item.item.media_id, item.parsed
                    )
                    await _mark_job(
                        session,
                        item.item.job_id,
                        status=IdentifyJobStatus.DONE,
                        label_count=label_count,
                    )
                    await session.commit()
                elapsed_ms = (time.monotonic() - item.started) * 1000
                persist_ms = (time.monotonic() - persist_started) * 1000
                _METRICS["completed"] = int(_METRICS["completed"]) + 1
                _METRICS["total_latency_ms"] = (
                    float(_METRICS["total_latency_ms"]) + elapsed_ms
                )
                _METRICS["total_gpu_ms"] = float(_METRICS["total_gpu_ms"] or 0) + item.gpu_ms
                _METRICS["last_completed_at"] = datetime.now(timezone.utc).isoformat()
                _publish_metrics()
                logger.info(
                    "identify_done file=%s labels=%d jpeg_kb=%.1f gpu_ms=%.0f persist_ms=%.0f wall_ms=%.0f",
                    item.item.drive_file_id[:12],
                    label_count,
                    item.jpeg_kb,
                    item.gpu_ms,
                    persist_ms,
                    elapsed_ms,
                )
            except asyncio.CancelledError:
                await release_identify_job(self._session_factory, item.item.job_id)
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("identify_persist_failed id=%s", item.item.job_id)
                await fail_identify_job(self._session_factory, item.item.job_id, exc)
