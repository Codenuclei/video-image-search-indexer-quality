"""Browser-scoped Drive session — no global email/token leak."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings, get_settings
from app.drive_browser_session import (
    HEADER_NAME,
    seal_drive_browser_session,
    unseal_drive_browser_session,
)


def test_seal_roundtrip():
    settings = Settings(
        carousel_google_client_secret="test-secret",
        google_client_secret="",
        webhook_secret="",
    )
    token = seal_drive_browser_session("google-sub-1", settings)
    assert unseal_drive_browser_session(token, settings) == "google-sub-1"
    assert unseal_drive_browser_session(token + "x", settings) is None
    other = Settings(carousel_google_client_secret="other-secret")
    assert unseal_drive_browser_session(token, other) is None


@pytest.mark.asyncio
async def test_session_hides_drive_without_browser_token(monkeypatch):
    from app import main as main_module
    from app.db import models
    from app.db.session import get_db
    from app.main import app

    monkeypatch.setenv("CAROUSEL_GOOGLE_CLIENT_SECRET", "test-secret")
    get_settings.cache_clear()
    main_module._boot_ready.set()

    class _FakeSession:
        async def get(self, model, key):
            if model is models.DriveUser and key == "sub-1":
                return models.DriveUser(
                    id="sub-1",
                    email="owner@example.com",
                    access_token="tok",
                    refresh_token="ref",
                )
            return None

    async def _override():
        yield _FakeSession()

    app.dependency_overrides[get_db] = _override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            bare = await ac.get("/api/session")
            assert bare.status_code == 200
            assert bare.json() == {"connected": False}

            denied = await ac.get("/api/drive-token")
            assert denied.status_code == 401

            token = seal_drive_browser_session("sub-1", get_settings())
            ok = await ac.get("/api/session", headers={HEADER_NAME: token})
            assert ok.status_code == 200
            body = ok.json()
            assert body["connected"] is True
            assert body["email"] == "owner@example.com"
    finally:
        app.dependency_overrides.pop(get_db, None)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_logout_without_browser_session_does_not_disconnect(monkeypatch):
    from app import main as main_module
    from app.db import models
    from app.db.session import get_db
    from app.main import app

    monkeypatch.setenv("CAROUSEL_GOOGLE_CLIENT_SECRET", "test-secret")
    get_settings.cache_clear()
    main_module._boot_ready.set()

    deleted = {"count": 0}

    class _FakeSession:
        async def get(self, model, key):
            if model is models.DriveUser and key == "sub-1":
                return models.DriveUser(
                    id="sub-1",
                    email="owner@example.com",
                    access_token="tok",
                    refresh_token="ref",
                )
            return None

        async def delete(self, _obj):
            deleted["count"] += 1

        async def commit(self):
            return None

    async def _override():
        yield _FakeSession()

    app.dependency_overrides[get_db] = _override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/logout")
            assert resp.status_code == 200
            assert resp.json()["ok"] is True
            assert deleted["count"] == 0

            token = seal_drive_browser_session("sub-1", get_settings())
            resp2 = await ac.post("/api/logout", headers={HEADER_NAME: token})
            assert resp2.status_code == 200
            assert deleted["count"] == 1
    finally:
        app.dependency_overrides.pop(get_db, None)
        get_settings.cache_clear()
