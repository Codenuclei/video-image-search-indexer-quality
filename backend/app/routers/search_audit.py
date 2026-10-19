"""Admin-only search audit list/detail endpoints."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SearchAuditLog
from app.db.session import get_db
from app.routers.app_auth import require_app_admin
from app.search.audit_log import get_search_audit, list_search_audits

router = APIRouter(prefix="/admin/search-audit", tags=["search-audit"])


class SearchAuditSummary(BaseModel):
    id: int
    created_at: datetime | None = None
    tat_ms: int
    status: str
    http_status: int | None = None
    error_detail: str | None = None
    query_text: str
    person: str | None = None
    mime: str | None = None
    folder_path: str | None = None
    source: str | None = None
    rerank: str | None = None
    captions: str | None = None
    retrieval_policy: str | None = None
    cache: str | None = None
    search_id: str | None = None
    user_email: str | None = None
    client_host: str | None = None
    file_count: int = 0
    moment_count: int = 0

    model_config = {"from_attributes": True}


class SearchAuditListResponse(BaseModel):
    items: list[SearchAuditSummary]
    total: int
    limit: int
    offset: int


class SearchAuditDetail(SearchAuditSummary):
    results_json: dict[str, Any] | None = None


def _summary(row: SearchAuditLog) -> SearchAuditSummary:
    return SearchAuditSummary.model_validate(row)


@router.get("", response_model=SearchAuditListResponse)
async def admin_list_search_audits(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    q: str | None = None,
    status: str | None = None,
    since: datetime | None = None,
    _admin_email: str = Depends(require_app_admin),
    session: AsyncSession = Depends(get_db),
) -> SearchAuditListResponse:
    rows, total = await list_search_audits(
        session,
        limit=limit,
        offset=offset,
        q=q,
        status=status,
        since=since,
    )
    return SearchAuditListResponse(
        items=[_summary(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{audit_id}", response_model=SearchAuditDetail)
async def admin_get_search_audit(
    audit_id: int,
    _admin_email: str = Depends(require_app_admin),
    session: AsyncSession = Depends(get_db),
) -> SearchAuditDetail:
    row = await get_search_audit(session, audit_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Search audit not found")
    return SearchAuditDetail(
        **_summary(row).model_dump(),
        results_json=row.results_json,
    )
