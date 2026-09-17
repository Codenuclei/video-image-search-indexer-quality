"""PostgreSQL-backed durable jobs for Carousel Studio long operations.

App Platform caps HTTP at ~100s and the container filesystem is ephemeral.
Visual prep / extract / generate job state must live in Postgres so
preparing→ready|error survives restarts. Themes reuse ``CarouselGenerationSave``;
transcripts reuse DriveFile phase markers in ``whisper_backfill``.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CarouselStudioJob

logger = logging.getLogger(__name__)

KIND_VISUAL_PREP = "visual_prep"
KIND_EXTRACT = "extract"
KIND_EXTRACT_HOOKS = "extract_hooks"
KIND_GENERATE = "generate"

STATUS_PREPARING = "preparing"
STATUS_RUNNING = "running"
STATUS_READY = "ready"
STATUS_ERROR = "error"

_LOCK = threading.Lock()
_RUNNING: set[str] = set()


def new_job_id() -> str:
    return uuid.uuid4().hex


def mark_running(job_id: str) -> bool:
    with _LOCK:
        if job_id in _RUNNING:
            return False
        _RUNNING.add(job_id)
        return True


def clear_running(job_id: str) -> None:
    with _LOCK:
        _RUNNING.discard(job_id)


def is_running(job_id: str) -> bool:
    with _LOCK:
        return job_id in _RUNNING


def job_to_dict(row: CarouselStudioJob) -> dict[str, Any]:
    return {
        "job_id": row.id,
        "id": row.id,
        "kind": row.kind,
        "drive_file_id": row.drive_file_id,
        "status": row.status,
        "slides_fingerprint": row.slides_fingerprint,
        "request": dict(row.request or {}),
        "result": dict(row.result or {}),
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def write_job(
    session: AsyncSession,
    *,
    kind: str,
    drive_file_id: str,
    status: str,
    job_id: str | None = None,
    slides_fingerprint: str | None = None,
    request_body: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    error: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Create or update a durable studio job row."""
    jid = (job_id or new_job_id()).strip()[:64]
    if not jid:
        jid = new_job_id()
    row = await session.get(CarouselStudioJob, jid)
    if row is None:
        row = CarouselStudioJob(
            id=jid,
            kind=kind,
            drive_file_id=drive_file_id,
            status=status,
            slides_fingerprint=slides_fingerprint,
            request=dict(request_body or {}),
            result=dict(payload or {}),
            error=error,
        )
        session.add(row)
    else:
        row.kind = kind
        row.drive_file_id = drive_file_id
        row.status = status
        if slides_fingerprint is not None:
            row.slides_fingerprint = slides_fingerprint
        if request_body is not None:
            row.request = dict(request_body)
        if payload is not None:
            row.result = dict(payload)
        row.error = error
    if commit:
        await session.commit()
        await session.refresh(row)
    else:
        await session.flush()
    return job_to_dict(row)


async def read_job(
    session: AsyncSession,
    job_id: str,
    *,
    kind: str | None = None,
    drive_file_id: str | None = None,
) -> dict[str, Any] | None:
    row = await session.get(CarouselStudioJob, job_id.strip())
    if row is None:
        return None
    if kind and row.kind != kind:
        return None
    if drive_file_id and row.drive_file_id != drive_file_id:
        return None
    return job_to_dict(row)


async def latest_job(
    session: AsyncSession,
    *,
    drive_file_id: str,
    kind: str,
    slides_fingerprint: str | None = None,
) -> dict[str, Any] | None:
    stmt = (
        select(CarouselStudioJob)
        .where(
            CarouselStudioJob.drive_file_id == drive_file_id,
            CarouselStudioJob.kind == kind,
        )
        .order_by(CarouselStudioJob.created_at.desc())
        .limit(24)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    if slides_fingerprint is not None:
        for row in rows:
            fp = row.slides_fingerprint or str((row.request or {}).get("slides_fingerprint") or "")
            if fp == slides_fingerprint:
                return job_to_dict(row)
        return None
    return job_to_dict(rows[0]) if rows else None
