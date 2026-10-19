"""Tests for production search audit logging and admin endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import app.auth_session as auth_session
from app.auth_session import seal_app_session
from app.config import get_settings
from app.db.models import AppAdmin, SearchAuditLog
from app.db.session import get_db
from app.main import app
from app.schemas import SearchResponse
from app.search.audit_log import (
    list_search_audits,
    purge_expired_search_audits,
    record_search_audit,
)
from app.search.cancel import SearchCancelled, is_search_cancelled
from tests.conftest import requires_postgres

TEST_SESSION_SECRET = "test-app-session-secret-ok"


@pytest.fixture(autouse=True)
def _configure_session_secret(monkeypatch):
    monkeypatch.setenv("APP_SESSION_SECRET", TEST_SESSION_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_EMAIL", "admintech@mastersunion.org")
    get_settings.cache_clear()
    monkeypatch.setattr(auth_session, "_missing_warned", False)
    yield
    get_settings.cache_clear()
    monkeypatch.setattr(auth_session, "_missing_warned", False)


def _auth_header(email: str = "owner@mastersunion.org", **kwargs) -> dict[str, str]:
    return {"Authorization": f"Bearer {seal_app_session(email, **kwargs)}"}


@pytest.fixture
def override_db(db_session):
    async def _get_db_override():
        yield db_session

    app.dependency_overrides[get_db] = _get_db_override
    yield db_session
    app.dependency_overrides.pop(get_db, None)


@pytest_asyncio.fixture
async def client(override_db):
    import app.main as main_module

    main_module._boot_ready.set()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@requires_postgres
@pytest.mark.asyncio
async def test_search_writes_audit_row(client, monkeypatch, db_session):
    from app.routers import search as search_router

    async def fake_run(**_kwargs):
        return SearchResponse(
            query="wine glass",
            answer="ok",
            citations=[],
            files=[],
            moments=[],
            cache="miss",
        )

    monkeypatch.setattr(search_router, "_run_search", fake_run)

    with patch(
        "app.search.audit_log.get_session_factory",
        return_value=lambda: _session_cm(db_session),
    ):
        res = await client.get("/search", params={"q": "wine glass"})
    assert res.status_code == 200

    rows = (await db_session.execute(select(SearchAuditLog))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.query_text == "wine glass"
    assert row.status == "ok"
    assert row.http_status == 200
    assert row.cache == "miss"
    assert row.retrieval_policy == "default"
    assert isinstance(row.results_json, dict)
    assert row.results_json.get("query") == "wine glass"


class _session_cm:
    """Async context manager that reuses the test session without closing it."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *args):
        return False


@requires_postgres
@pytest.mark.asyncio
async def test_search_cancelled_audit_status(client, monkeypatch, db_session):
    from app.routers import search as search_router

    async def boom(**_kwargs):
        raise SearchCancelled()

    monkeypatch.setattr(search_router, "_run_search", boom)

    with patch(
        "app.search.audit_log.get_session_factory",
        return_value=lambda: _session_cm(db_session),
    ):
        res = await client.get("/search", params={"q": "wine glass", "search_id": "t-audit-cancel"})
    assert res.status_code == 499
    assert is_search_cancelled("t-audit-cancel") is False

    row = (await db_session.execute(select(SearchAuditLog))).scalars().one()
    assert row.status == "cancelled"
    assert row.http_status == 499


@requires_postgres
@pytest.mark.asyncio
async def test_search_audit_write_failure_does_not_break_search(client, monkeypatch):
    from app.routers import search as search_router

    async def fake_run(**_kwargs):
        return SearchResponse(
            query="ok",
            answer="",
            citations=[],
            files=[],
            moments=[],
            cache="exact",
        )

    async def boom_audit(**_kwargs):
        raise RuntimeError("audit db down")

    monkeypatch.setattr(search_router, "_run_search", fake_run)
    monkeypatch.setattr(search_router, "record_search_audit", boom_audit)

    res = await client.get("/search", params={"q": "ok"})
    assert res.status_code == 200
    assert res.json()["cache"] == "exact"


@requires_postgres
@pytest.mark.asyncio
async def test_admin_search_audit_auth(client, db_session):
    missing = await client.get("/admin/search-audit")
    assert missing.status_code == 401

    non_admin = await client.get(
        "/admin/search-audit",
        headers=_auth_header("user@mastersunion.org", is_admin=False),
    )
    assert non_admin.status_code == 403

    db_session.add(AppAdmin(email="admin@mastersunion.org", created_by="test"))
    await db_session.commit()

    ok = await client.get(
        "/admin/search-audit",
        headers=_auth_header("admin@mastersunion.org", is_admin=False),
    )
    assert ok.status_code == 200
    assert ok.json()["total"] == 0
    assert ok.json()["items"] == []

    password_admin = await client.get(
        "/admin/search-audit",
        headers=_auth_header("admintech@mastersunion.org", is_admin=True),
    )
    assert password_admin.status_code == 200


@requires_postgres
@pytest.mark.asyncio
async def test_admin_search_audit_detail_and_list_omit_results(client, db_session):
    db_session.add(AppAdmin(email="admin@mastersunion.org", created_by="test"))
    await db_session.commit()

    with patch(
        "app.search.audit_log.get_session_factory",
        return_value=lambda: _session_cm(db_session),
    ):
        row_id = await record_search_audit(
            tat_ms=42,
            status="ok",
            http_status=200,
            query_text="detail query",
            response=SearchResponse(
                query="detail query",
                answer="ans",
                citations=[],
                files=[],
                moments=[],
                cache="miss",
            ),
            purge_after=False,
        )
    assert row_id is not None

    headers = _auth_header("admin@mastersunion.org")
    listed = await client.get("/admin/search-audit", headers=headers)
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 1
    assert "results_json" not in body["items"][0]
    assert body["items"][0]["query_text"] == "detail query"

    detail = await client.get(f"/admin/search-audit/{row_id}", headers=headers)
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["results_json"]["answer"] == "ans"
    assert detail_body["tat_ms"] == 42


@requires_postgres
@pytest.mark.asyncio
async def test_purge_expired_search_audits(db_session):
    old = SearchAuditLog(
        tat_ms=1,
        status="ok",
        query_text="old",
        file_count=0,
        moment_count=0,
        created_at=datetime.now(timezone.utc) - timedelta(days=45),
    )
    recent = SearchAuditLog(
        tat_ms=2,
        status="ok",
        query_text="recent",
        file_count=0,
        moment_count=0,
        created_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    db_session.add_all([old, recent])
    await db_session.commit()

    deleted = await purge_expired_search_audits(db_session, retain_days=30)
    await db_session.commit()
    assert deleted == 1

    rows, total = await list_search_audits(db_session, limit=10, offset=0)
    assert total == 1
    assert rows[0].query_text == "recent"


@pytest.mark.asyncio
async def test_search_handler_audit_on_cancel_unit(monkeypatch):
    """Unit-level: cancelled path still invokes record_search_audit."""
    from app.routers import search as search_router

    recorded: list[dict] = []

    async def boom(**_kwargs):
        raise SearchCancelled()

    async def capture(**kwargs):
        recorded.append(kwargs)
        return None

    monkeypatch.setattr(search_router, "_run_search", boom)
    monkeypatch.setattr(search_router, "record_search_audit", capture)

    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)
    request.headers = {}
    request.client = MagicMock(host="127.0.0.1")

    with pytest.raises(HTTPException) as exc:
        await search_router.search(
            request=request,
            q="wine glass",
            search_id="t-audit-unit",
            session=AsyncMock(),
        )
    assert exc.value.status_code == 499
    assert len(recorded) == 1
    assert recorded[0]["status"] == "cancelled"
    assert recorded[0]["http_status"] == 499
    assert recorded[0]["client_host"] == "127.0.0.1"


@pytest.mark.asyncio
async def test_search_audit_attributes_bearer_email(monkeypatch):
    from app.routers import search as search_router

    recorded: list[dict] = []

    async def fake_run(**_kwargs):
        return SearchResponse(
            query="attributed",
            answer="",
            citations=[],
            files=[],
            moments=[],
            cache="miss",
        )

    async def capture(**kwargs):
        recorded.append(kwargs)
        return None

    token = seal_app_session("searcher@mastersunion.org", is_admin=False)
    monkeypatch.setattr(search_router, "_run_search", fake_run)
    monkeypatch.setattr(search_router, "record_search_audit", capture)

    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)
    request.headers = {
        "authorization": f"Bearer {token}",
        "x-forwarded-for": "203.0.113.9, 10.0.0.1",
    }
    request.client = MagicMock(host="10.0.0.1")

    result = await search_router.search(
        request=request,
        q="attributed",
        session=AsyncMock(),
    )
    assert result.query == "attributed"
    assert len(recorded) == 1
    assert recorded[0]["user_email"] == "searcher@mastersunion.org"
    assert recorded[0]["client_host"] == "203.0.113.9"


@pytest.mark.asyncio
async def test_testv1_does_not_write_search_audit(monkeypatch):
    from app.routers import search as search_router

    recorded: list[dict] = []

    async def fake_run(**_kwargs):
        return SearchResponse(
            query="lab",
            answer="",
            citations=[],
            files=[],
            moments=[],
            cache=None,
        )

    async def capture(**kwargs):
        recorded.append(kwargs)
        return None

    monkeypatch.setattr(search_router, "_run_search", fake_run)
    monkeypatch.setattr(search_router, "record_search_audit", capture)

    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)
    request.headers = {}
    request.client = MagicMock(host="127.0.0.1")

    result = await search_router.search_testv1(
        request=request,
        q="lab",
        session=AsyncMock(),
    )
    assert result.query == "lab"
    assert recorded == []


def test_escape_like_wildcards():
    from app.search.audit_log import _escape_like

    assert _escape_like("a%b_c\\d") == "a\\%b\\_c\\\\d"
