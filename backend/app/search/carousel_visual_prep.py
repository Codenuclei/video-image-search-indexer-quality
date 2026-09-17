"""Durable visual preparation jobs for Choose images (Postgres-backed).

When RunPod cold-start exceeds the interactive select-images budget, persist a
job so ``/test/studio`` can poll preparing → ready. State lives in
``carousel_studio_jobs`` (not the ephemeral thumbnail filesystem).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.search.carousel_studio_jobs import (
    KIND_VISUAL_PREP,
    STATUS_ERROR,
    STATUS_PREPARING,
    STATUS_READY,
    clear_running,
    is_running,
    latest_job,
    mark_running,
    read_job as read_studio_job,
    write_job as write_studio_job,
)

logger = logging.getLogger(__name__)

__all__ = [
    "KIND_VISUAL_PREP",
    "STATUS_ERROR",
    "STATUS_PREPARING",
    "STATUS_READY",
    "clear_running",
    "is_running",
    "latest_job_for_fingerprint",
    "mark_running",
    "read_job",
    "slides_fingerprint",
    "write_job",
]


def slides_fingerprint(slides: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for slide in slides:
        if not isinstance(slide, dict):
            continue
        parts.append(
            f"{slide.get('timestamp_sec')}:{slide.get('end_timestamp_sec')}:"
            f"{(slide.get('transcript_text') or slide.get('hook_line') or '')[:40]}"
        )
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


async def write_job(
    session: AsyncSession,
    drive_file_id: str,
    *,
    job_id: str | None = None,
    status: str,
    payload: dict[str, Any] | None = None,
    error: str | None = None,
    request_body: dict[str, Any] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    fp = None
    if request_body is not None:
        fp = str(request_body.get("slides_fingerprint") or "").strip() or None
    data = await write_studio_job(
        session,
        kind=KIND_VISUAL_PREP,
        drive_file_id=drive_file_id,
        status=status,
        job_id=job_id,
        slides_fingerprint=fp,
        request_body=request_body,
        payload=payload,
        error=error,
        commit=commit,
    )
    # Preserve filesystem-era shape used by status polling.
    return {
        **data,
        "result": data.get("result") or {},
    }


async def read_job(
    session: AsyncSession,
    drive_file_id: str,
    job_id: str,
) -> dict[str, Any] | None:
    data = await read_studio_job(
        session,
        job_id,
        kind=KIND_VISUAL_PREP,
        drive_file_id=drive_file_id,
    )
    return data


async def latest_job_for_fingerprint(
    session: AsyncSession,
    drive_file_id: str,
    fingerprint: str,
) -> dict[str, Any] | None:
    return await latest_job(
        session,
        drive_file_id=drive_file_id,
        kind=KIND_VISUAL_PREP,
        slides_fingerprint=fingerprint,
    )
