"""Pauseable Qwen identify lane: Drive originals → SGLang photos → isolated labels.

Writes only identify_jobs and media_identify_labels. Never touches object_jobs
or media_object_labels.
"""
from __future__ import annotations

import asyncio
import base64
import io
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
    IDENTIFY_PROMPT,
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
    "last_completed_at": None,
    "last_starved_at": None,
    "working_set_files": 0,
    "gpu_in_flight": 0,
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
    prompt: str = IDENTIFY_PROMPT,
    max_tokens: int = 640,
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
    from PIL import Image

    from app.pipelines.common import open_image_rgb

    dest.parent.mkdir(parents=True, exist_ok=True)
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
    dest.write_bytes(data)


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
    lease_seconds: int = 1800,
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


async def _load_input(
    session: AsyncSession,
    job_id: int,
) -> IdentifyInput | None:
    job = await session.get(IdentifyJob, job_id)
    if job is None:
        return None
    media = await session.scalar(
        select(Media).where(Media.drive_file_id == job.drive_file_id)
    )
    drive_file = await session.get(DriveFile, job.drive_file_id)
    if media is None or drive_file is None:
        return None
    return IdentifyInput(
        job_id=job.id,
        drive_file_id=job.drive_file_id,
        media_id=media.id,
        name=drive_file.name or "",
        mime_type=drive_file.mime_type or "",
        path=drive_file.path or "",
        size=drive_file.size,
    )


async def persist_identify_labels(
    session: AsyncSession,
    media_id: int,
    parsed,
    *,
    model_version: str = QWEN_IDENTIFY_MODEL_VERSION,
) -> int:
    """Replace this media's Qwen identify rows only."""
    await session.execute(
        delete(MediaIdentifyLabel).where(
            MediaIdentifyLabel.media_id == media_id,
            MediaIdentifyLabel.model_version == model_version,
        )
    )
    now = datetime.now(timezone.utc)
    rows = persist_rows(parsed)
    for row in rows:
        stmt = insert(MediaIdentifyLabel).values(
            media_id=media_id,
            canonical_label=row["canonical_label"],
            category=row["category"],
            confidence=row["confidence"],
            evidence_source=row["evidence_source"],
            evidence_text=row.get("evidence_text"),
            best_timestamp=None,
            hit_count=row.get("hit_count", 1),
            model_version=model_version,
            updated_at=now,
        )
        await session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_media_identify_label_media_label_model",
                set_={
                    "confidence": stmt.excluded.confidence,
                    "category": stmt.excluded.category,
                    "evidence_source": stmt.excluded.evidence_source,
                    "evidence_text": stmt.excluded.evidence_text,
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
        "working_set_files": working_set_files,
        "gpu_in_flight": int(_METRICS["gpu_in_flight"] or 0),
        "model_version": QWEN_IDENTIFY_MODEL_VERSION,
    }


async def produce_identify_backfill(
    session: AsyncSession,
    *,
    limit: int = 1000,
    dry_run: bool = False,
) -> dict[str, int | bool]:
    """Enqueue processed images missing a done/pending identify job."""
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
    payload = build_identify_payload(
        jpeg_bytes,
        model=settings.qwen_identify_model or settings.qwen_vlm_model,
        max_tokens=settings.qwen_identify_max_tokens,
    )
    base = sglang_base_url(settings)
    if not base:
        raise RuntimeError("qwen identify base URL is not configured")
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
    from app.drive.indexing_pause import (
        file_under_folder,
        global_indexing_is_paused,
        load_paused_folder_paths,
    )
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
        if not runtime.identify_lane_enabled or await global_indexing_is_paused(session):
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
        paused_paths = await load_paused_folder_paths(session)
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
        async with gpu_sem:
            _METRICS["gpu_in_flight"] = int(_METRICS["gpu_in_flight"] or 0) + 1
            try:
                text_out = await _post_identify(http, jpeg_bytes, settings)
            finally:
                _METRICS["gpu_in_flight"] = max(0, int(_METRICS["gpu_in_flight"] or 0) - 1)
        parsed = parse_identify_output(text_out)
        async with session_factory() as session:
            label_count = await persist_identify_labels(session, item.media_id, parsed)
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
        _METRICS["last_completed_at"] = datetime.now(timezone.utc).isoformat()
        logger.info(
            "identify_done file=%s labels=%d ms=%.0f",
            item.drive_file_id[:12],
            label_count,
            elapsed_ms,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("identify_job_failed id=%s", job_id)
        await fail_identify_job(session_factory, job_id, exc)
    finally:
        unlink_quietly(jpeg_path)
        _METRICS["working_set_files"] = _count_working_files(working_dir)


class IdentifyWorkerLoop:
    """dfi-backend consumer: overlap Drive download with SGLang identify."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._session_factory = session_factory or get_session_factory()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._token = uuid.uuid4().hex
        self._active: set[asyncio.Task] = set()
        self._http: httpx.AsyncClient | None = None

    def ensure_started(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="identify-qwen-lane")

    async def stop(self) -> None:
        self._stop.set()
        for task in list(self._active):
            task.cancel()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, *list(self._active), return_exceptions=True)
            self._task = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        wipe_identify_working_dir()

    def _lane_enabled(self) -> bool:
        if self._stop.is_set():
            return False
        from app.runtime_settings import get_runtime_settings

        return bool(get_runtime_settings().identify_lane_enabled)

    async def _run(self) -> None:
        settings = get_settings()
        working_dir = identify_working_dir(settings)
        working_dir.mkdir(parents=True, exist_ok=True)
        gpu_sem = asyncio.Semaphore(max(1, settings.qwen_identify_concurrency))
        decode_sem = asyncio.Semaphore(4)
        timeout = httpx.Timeout(settings.qwen_identify_timeout_seconds)
        limits = httpx.Limits(
            max_connections=max(16, settings.qwen_identify_concurrency + 8),
            max_keepalive_connections=settings.qwen_identify_concurrency,
        )
        self._http = httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, limits=limits
        )
        try:
            while not self._stop.is_set():
                try:
                    await self._tick(
                        working_dir=working_dir,
                        gpu_sem=gpu_sem,
                        decode_sem=decode_sem,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.exception("identify_loop_tick_failed")
                    await asyncio.sleep(2)
        finally:
            if self._http is not None:
                await self._http.aclose()
                self._http = None
            wipe_identify_working_dir(working_dir)

    async def _tick(
        self,
        *,
        working_dir: Path,
        gpu_sem: asyncio.Semaphore,
        decode_sem: asyncio.Semaphore,
    ) -> None:
        from app.db.app_settings_store import refresh_runtime_settings_from_db
        from app.drive.indexing_pause import global_indexing_is_paused

        settings = get_settings()
        working_set = max(1, min(1000, settings.qwen_identify_working_set))
        async with self._session_factory() as session:
            runtime = await refresh_runtime_settings_from_db(session)
            if not runtime.identify_lane_enabled:
                await session.rollback()
                if self._active:
                    await asyncio.wait(self._active, timeout=1.0)
                    return
                wipe_identify_working_dir(working_dir)
                await asyncio.sleep(5)
                return
            if await global_indexing_is_paused(session):
                await session.rollback()
                await asyncio.sleep(5)
                return
            slots = working_set - len(self._active)
            if slots <= 0:
                await session.rollback()
                if self._active:
                    await asyncio.wait(
                        self._active, return_when=asyncio.FIRST_COMPLETED
                    )
                return
            job_ids = await claim_identify_jobs(
                session,
                limit=slots,
                lease_seconds=settings.qwen_identify_lease_seconds,
                worker_token=self._token,
            )
            await session.commit()

        if not job_ids:
            if runtime.identify_backfill_enabled:
                async with self._session_factory() as producer_session:
                    produced = await produce_identify_backfill(
                        producer_session,
                        limit=working_set,
                    )
                if int(produced.get("enqueued", 0)):
                    return
            if self._active:
                await asyncio.wait(
                    self._active, timeout=2.0, return_when=asyncio.FIRST_COMPLETED
                )
            else:
                _METRICS["last_starved_at"] = datetime.now(timezone.utc).isoformat()
                await asyncio.sleep(2)
            return

        http = self._http
        if http is None:
            return
        for job_id in job_ids:
            task = asyncio.create_task(
                process_identify_job(
                    self._session_factory,
                    job_id,
                    gpu_sem=gpu_sem,
                    decode_sem=decode_sem,
                    working_dir=working_dir,
                    http=http,
                    lane_enabled=self._lane_enabled,
                ),
                name=f"identify-job-{job_id}",
            )
            self._active.add(task)
            task.add_done_callback(self._active.discard)
