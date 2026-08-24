"""Per-file index turnaround (claim → done) logging and admin aggregates.

End stamp is ``DriveFile.completed_at``. Images are stamped on PROCESSED (index
done) and refreshed on caption upsert when captions land later. Videos stamp on
PROCESSED/ERROR. Stats also fall back to ``last_synced_at`` so Admin is never
empty when completed_at lagged behind.
"""
from __future__ import annotations

import logging
from collections.abc import Collection
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Select, case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile, DriveFileStatus
from app.pipelines.common import is_image_mime, is_video_mime

logger = logging.getLogger(__name__)

TAT_SAMPLE_LIMIT = 500


def index_kind_for_mime(mime_type: str | None, name: str = "") -> str | None:
    mime = (mime_type or "").strip()
    if not mime and not name:
        return None
    try:
        if is_video_mime(mime):
            return "video"
        if is_image_mime(mime, name):
            return "image"
    except Exception:  # noqa: BLE001 — never break callers
        return None
    return None


def tat_ms_from_started(
    started_at: datetime | None,
    ended_at: datetime | None = None,
) -> int | None:
    if started_at is None:
        return None
    end = ended_at or datetime.now(timezone.utc)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    ms = int((end - started_at).total_seconds() * 1000)
    return max(0, ms)


def log_index_tat(
    *,
    file_id: str,
    kind: str | None,
    status: str,
    tat_ms: int | None,
    size: int | None = None,
    name: str | None = None,
    phase: str = "done",
) -> None:
    if kind is None or tat_ms is None:
        return
    logger.info(
        "index_tat file_id=%s kind=%s status=%s phase=%s tat_ms=%d size=%s name=%s",
        file_id,
        kind,
        status,
        phase,
        tat_ms,
        size if size is not None else "",
        (name or "")[:80],
    )


def empty_kind_bucket() -> dict[str, int]:
    return {"count": 0, "min_ms": 0, "max_ms": 0, "avg_ms": 0}


async def stamp_completed_at(
    session: AsyncSession,
    file_ids: Collection[str],
    *,
    now: datetime | None = None,
    reason: str = "done",
    force: bool = False,
) -> list[str]:
    """Set completed_at. First write wins unless ``force`` (caption extends the end)."""
    ids = [fid for fid in file_ids if fid]
    if not ids:
        return []
    stamp = now or datetime.now(timezone.utc)
    where = [
        DriveFile.id.in_(ids),
        DriveFile.processing_started_at.is_not(None),
    ]
    if not force:
        where.append(DriveFile.completed_at.is_(None))
    result = await session.execute(
        update(DriveFile)
        .where(*where)
        .values(completed_at=stamp)
        .returning(
            DriveFile.id,
            DriveFile.mime_type,
            DriveFile.name,
            DriveFile.size,
            DriveFile.status,
            DriveFile.processing_started_at,
        )
    )
    stamped: list[str] = []
    for row in result.all():
        file_id, mime_type, name, size, status, started = row
        stamped.append(str(file_id))
        try:
            log_index_tat(
                file_id=str(file_id),
                kind=index_kind_for_mime(mime_type, name or ""),
                status=status.value if hasattr(status, "value") else str(status),
                tat_ms=tat_ms_from_started(started, stamp),
                size=size,
                name=name,
                phase=reason,
            )
        except Exception:  # noqa: BLE001
            logger.exception("index_tat_log_failed file_id=%s", str(file_id)[:12])
    return stamped


async def stamp_completed_at_ids(
    file_ids: Collection[str],
    *,
    reason: str = "captioned",
    force: bool = False,
) -> int:
    """Open a short session and stamp completion (for caption upsert / fire-and-forget)."""
    ids = [fid for fid in file_ids if fid]
    if not ids:
        return 0
    try:
        from app.db.session import get_session_factory

        async with get_session_factory() as session:
            stamped = await stamp_completed_at(
                session, ids, reason=reason, force=force
            )
            await session.commit()
            return len(stamped)
    except Exception:  # noqa: BLE001 — never fail caption/index over TAT
        logger.exception("stamp_completed_at_ids_failed count=%d", len(ids))
        return 0


async def backfill_completed_at_from_synced(session: AsyncSession) -> int:
    """One-shot repair: rows that finished before completed_at existed."""
    result = await session.execute(
        update(DriveFile)
        .where(
            DriveFile.completed_at.is_(None),
            DriveFile.processing_started_at.is_not(None),
            DriveFile.last_synced_at.is_not(None),
            DriveFile.status.in_((DriveFileStatus.PROCESSED, DriveFileStatus.ERROR)),
        )
        .values(completed_at=DriveFile.last_synced_at)
    )
    n = int(result.rowcount or 0)
    if n:
        logger.info("index_tat_backfill_completed_at rows=%d", n)
    return n


async def reset_index_tat_samples(session: AsyncSession) -> dict[str, int]:
    """Clear claim→done TAT stamps only so Admin min/avg/max restart empty.

    Does not change status, Media, embeddings, or captions. Only NULLs
    ``processing_started_at`` and ``completed_at`` on finished rows that already
    had a started stamp (so they drop out of ``/index/tat-stats``).
    """
    result = await session.execute(
        update(DriveFile)
        .where(
            DriveFile.processing_started_at.is_not(None),
            DriveFile.status.in_((DriveFileStatus.PROCESSED, DriveFileStatus.ERROR)),
        )
        .values(processing_started_at=None, completed_at=None)
    )
    cleared = int(result.rowcount or 0)
    logger.info("index_tat_samples_reset cleared=%d", cleared)
    return {"cleared": cleared}


async def build_index_tat_stats(session: AsyncSession) -> dict[str, Any]:
    """Min/max/avg claim→done TAT for recent completed image/video rows."""
    # Repair rows that finished before completed_at existed (never re-stamps
    # after an explicit reset — those rows have processing_started_at NULL).
    try:
        await backfill_completed_at_from_synced(session)
        await session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("index_tat_backfill_failed")
        await session.rollback()

    # Prefer completed_at; last_synced_at only when started stamp still present
    # (legacy rows). Reset clears both so old samples cannot reappear via coalesce.
    end_at = func.coalesce(DriveFile.completed_at, DriveFile.last_synced_at)
    recent: Select = (
        select(
            DriveFile.id,
            DriveFile.mime_type,
            DriveFile.name,
            DriveFile.processing_started_at,
            end_at.label("ended_at"),
        )
        .where(
            DriveFile.processing_started_at.is_not(None),
            DriveFile.completed_at.is_not(None),
            DriveFile.status.in_((DriveFileStatus.PROCESSED, DriveFileStatus.ERROR)),
        )
        .order_by(DriveFile.completed_at.desc())
        .limit(TAT_SAMPLE_LIMIT)
        .subquery()
    )

    kind_expr = case(
        (recent.c.mime_type.like("video/%"), "video"),
        (recent.c.mime_type.like("image/%"), "image"),
        else_="other",
    )
    tat_ms_expr = (
        func.extract("epoch", recent.c.ended_at - recent.c.processing_started_at) * 1000.0
    )

    rows = (
        await session.execute(
            select(
                kind_expr.label("kind"),
                func.count().label("count"),
                func.min(tat_ms_expr).label("min_ms"),
                func.max(tat_ms_expr).label("max_ms"),
                func.avg(tat_ms_expr).label("avg_ms"),
            )
            .select_from(recent)
            .where(kind_expr.in_(("image", "video")))
            .group_by(kind_expr)
        )
    ).all()

    out: dict[str, Any] = {
        "image": empty_kind_bucket(),
        "video": empty_kind_bucket(),
        "sample_window": f"last_{TAT_SAMPLE_LIMIT}_completed",
        "metric": "claim_to_done",
        "done_means": "processed_or_captioned_or_error",
    }
    for kind, count, min_ms, max_ms, avg_ms in rows:
        if kind not in ("image", "video"):
            continue
        out[kind] = {
            "count": int(count or 0),
            "min_ms": int(round(float(min_ms or 0))),
            "max_ms": int(round(float(max_ms or 0))),
            "avg_ms": int(round(float(avg_ms or 0))),
        }
    return out
