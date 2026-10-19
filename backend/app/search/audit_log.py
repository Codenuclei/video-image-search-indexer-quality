"""Soft-fail persistence for production search audit rows."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import defer
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SearchAuditLog
from app.db.session import get_session_factory
from app.schemas import SearchResponse

logger = logging.getLogger(__name__)

_ERROR_DETAIL_MAX = 2000


def _truncate(value: str | None, limit: int = _ERROR_DETAIL_MAX) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards so user filters are literal substrings."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def record_search_audit(
    *,
    tat_ms: int,
    status: str,
    http_status: int | None = None,
    error_detail: str | None = None,
    query_text: str = "",
    person: str | None = None,
    mime: str | None = None,
    folder_path: str | None = None,
    source: str | None = None,
    rerank: str | None = None,
    captions: str | None = None,
    retrieval_policy: str | None = None,
    cache: str | None = None,
    search_id: str | None = None,
    user_email: str | None = None,
    client_host: str | None = None,
    response: SearchResponse | None = None,
    results_json: dict[str, Any] | None = None,
    purge_after: bool = True,
    retain_days: int = 30,
) -> int | None:
    """Insert one audit row. Never raises to the caller."""
    try:
        payload = results_json
        file_count = 0
        moment_count = 0
        cache_value = cache
        if response is not None:
            try:
                payload = response.model_dump(mode="json")
            except Exception:  # noqa: BLE001
                payload = payload or {}
            file_count = len(response.files or [])
            moment_count = len(response.moments or [])
            if cache_value is None:
                cache_value = response.cache

        factory = get_session_factory()
        async with factory() as session:
            row = SearchAuditLog(
                tat_ms=max(0, int(tat_ms)),
                status=(status or "error").strip().lower()[:32],
                http_status=http_status,
                error_detail=_truncate(error_detail),
                query_text=query_text or "",
                person=(person or None),
                mime=(mime or None),
                folder_path=(folder_path or None),
                source=(source or None),
                rerank=(rerank if rerank is not None else None),
                captions=(captions if captions is not None else None),
                retrieval_policy=(retrieval_policy or None),
                cache=(cache_value or None),
                search_id=(search_id or None),
                user_email=(user_email.strip().lower() if user_email else None),
                client_host=(client_host or None),
                file_count=file_count,
                moment_count=moment_count,
                results_json=payload,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            row_id = int(row.id)
            if purge_after:
                try:
                    await purge_expired_search_audits(session, retain_days=retain_days)
                    await session.commit()
                except Exception:  # noqa: BLE001
                    logger.debug("search audit purge failed", exc_info=True)
                    await session.rollback()
            return row_id
    except Exception:  # noqa: BLE001
        logger.warning("search audit write failed", exc_info=True)
        return None


async def list_search_audits(
    session: AsyncSession,
    *,
    limit: int = 50,
    offset: int = 0,
    q: str | None = None,
    status: str | None = None,
    since: datetime | None = None,
) -> tuple[list[SearchAuditLog], int]:
    """Return (rows, total) without loading results_json in the list projection."""
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))

    filters = []
    if status and status.strip():
        filters.append(SearchAuditLog.status == status.strip().lower())
    if since is not None:
        filters.append(SearchAuditLog.created_at >= since)
    if q and q.strip():
        needle = f"%{_escape_like(q.strip().lower())}%"
        filters.append(
            or_(
                func.lower(SearchAuditLog.query_text).like(needle, escape="\\"),
                func.lower(func.coalesce(SearchAuditLog.user_email, "")).like(
                    needle, escape="\\"
                ),
                func.lower(func.coalesce(SearchAuditLog.person, "")).like(
                    needle, escape="\\"
                ),
            )
        )

    count_stmt = select(func.count()).select_from(SearchAuditLog)
    # Defer JSONB snapshots — list responses never need them and they can be large.
    list_stmt = (
        select(SearchAuditLog)
        .options(defer(SearchAuditLog.results_json))
        .order_by(SearchAuditLog.created_at.desc())
    )
    for clause in filters:
        count_stmt = count_stmt.where(clause)
        list_stmt = list_stmt.where(clause)

    total = int(await session.scalar(count_stmt) or 0)
    rows = list(
        (
            await session.execute(list_stmt.offset(offset).limit(limit))
        ).scalars().all()
    )
    return rows, total


async def get_search_audit(session: AsyncSession, audit_id: int) -> SearchAuditLog | None:
    return await session.get(SearchAuditLog, int(audit_id))


async def purge_expired_search_audits(
    session: AsyncSession,
    *,
    retain_days: int = 30,
) -> int:
    """Delete audit rows older than retain_days. Returns deleted count."""
    days = max(1, int(retain_days or 30))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = await session.execute(
        delete(SearchAuditLog).where(SearchAuditLog.created_at < cutoff)
    )
    return int(result.rowcount or 0)
