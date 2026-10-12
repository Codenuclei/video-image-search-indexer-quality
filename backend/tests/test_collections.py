"""Tests for shareable collections auth and helpers."""
from __future__ import annotations

import time

import pytest
import pytest_asyncio
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

import app.auth_session as auth_session
from app.auth_session import (
    current_user_email,
    seal_app_session,
    verify_app_session_token,
)
from app.config import get_settings
from app.db.models import Collection
from app.db.session import get_db
from app.main import app
from app.routers.collections import (
    MAX_ITEMS_PER_REQUEST,
    MAX_QUERY_LEN,
    CollectionItemIn,
    CollectionItemsIn,
    is_owner,
    normalize_name,
    require_owner,
)
from tests.conftest import requires_postgres

TEST_SESSION_SECRET = "test-app-session-secret-ok"  # >= 16 chars


@pytest.fixture(autouse=True)
def _configure_session_secret(monkeypatch):
    """Fail-closed auth requires an explicit secret for seal/verify in tests."""
    monkeypatch.setenv("APP_SESSION_SECRET", TEST_SESSION_SECRET)
    get_settings.cache_clear()
    monkeypatch.setattr(auth_session, "_missing_warned", False)
    yield
    get_settings.cache_clear()
    monkeypatch.setattr(auth_session, "_missing_warned", False)


def _auth_header(email: str = "owner@mastersunion.org", **kwargs) -> dict[str, str]:
    return {"Authorization": f"Bearer {seal_app_session(email, **kwargs)}"}


def test_verify_valid_token():
    token = seal_app_session("Alice@MastersUnion.org", is_admin=False)
    assert verify_app_session_token(token) == "alice@mastersunion.org"


def test_verify_tampered_signature():
    token = seal_app_session("owner@mastersunion.org")
    body, _, sig = token.partition(".")
    tampered = f"{body}.{sig[:-2]}xx" if len(sig) > 2 else f"{body}.deadbeef"
    assert verify_app_session_token(tampered) is None


def test_verify_expired_token():
    token = seal_app_session("owner@mastersunion.org", exp=int(time.time()) - 60)
    assert verify_app_session_token(token) is None


def test_verify_wrong_domain():
    token = seal_app_session("user@gmail.com")
    assert verify_app_session_token(token) is None


def test_verify_missing_and_garbled():
    assert verify_app_session_token(None) is None
    assert verify_app_session_token("") is None
    assert verify_app_session_token("not-a-token") is None
    assert verify_app_session_token("onlybody.") is None
    assert verify_app_session_token(".onlysig") is None


def test_unset_secret_fails_closed(monkeypatch):
    monkeypatch.setenv("APP_SESSION_SECRET", "")
    get_settings.cache_clear()
    monkeypatch.setattr(auth_session, "_missing_warned", False)
    assert auth_session.configured_session_secret() is None
    assert verify_app_session_token("anything.goes") is None


def test_short_secret_fails_closed(monkeypatch):
    monkeypatch.setenv("APP_SESSION_SECRET", "tooshort")
    get_settings.cache_clear()
    monkeypatch.setattr(auth_session, "_missing_warned", False)
    assert auth_session.configured_session_secret() is None
    assert verify_app_session_token("anything.goes") is None


@pytest.mark.asyncio
async def test_unset_secret_current_user_email_503(monkeypatch):
    monkeypatch.setenv("APP_SESSION_SECRET", "")
    get_settings.cache_clear()
    monkeypatch.setattr(auth_session, "_missing_warned", False)
    with pytest.raises(HTTPException) as exc:
        await current_user_email(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials="x.y")
        )
    assert exc.value.status_code == 503
    assert "APP_SESSION_SECRET" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_configured_secret_valid_token_dependency():
    token = seal_app_session("owner@mastersunion.org")
    email = await current_user_email(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    )
    assert email == "owner@mastersunion.org"


def test_normalize_name_trims_and_rejects_blank():
    assert normalize_name("  Campaign shots  ") == "Campaign shots"
    with pytest.raises(HTTPException) as exc:
        normalize_name("   ")
    assert exc.value.status_code == 422
    long_name = "x" * 200
    assert len(normalize_name(long_name)) == 120


def test_is_owner_case_insensitive():
    collection = Collection(
        id="abc",
        owner_email="owner@mastersunion.org",
        name="Demo",
    )
    assert is_owner(collection, "Owner@MastersUnion.org") is True
    assert is_owner(collection, "other@mastersunion.org") is False
    assert is_owner(collection, None) is False

    with pytest.raises(HTTPException) as exc:
        require_owner(collection, "other@mastersunion.org")
    assert exc.value.status_code == 403


def test_collection_item_query_max_length():
    ok = CollectionItemIn(
        drive_file_id="f1",
        name="a.jpg",
        mime_type="image/jpeg",
        query="q" * MAX_QUERY_LEN,
    )
    assert len(ok.query or "") == MAX_QUERY_LEN
    with pytest.raises(ValidationError):
        CollectionItemIn(
            drive_file_id="f1",
            name="a.jpg",
            mime_type="image/jpeg",
            query="q" * (MAX_QUERY_LEN + 1),
        )


def test_collection_items_batch_max_length():
    item = {
        "drive_file_id": "f1",
        "name": "a.jpg",
        "mime_type": "image/jpeg",
    }
    CollectionItemsIn(items=[item])
    with pytest.raises(ValidationError):
        CollectionItemsIn(items=[item] * (MAX_ITEMS_PER_REQUEST + 1))


def test_strip_before_dedupe_logic():
    """drive_file_id must be stripped before membership / duplicate checks."""
    existing = {"file-1"}
    added: list[str] = []
    for raw in ("  file-1  ", " file-2 ", "", "  "):
        drive_file_id = raw.strip()
        if not drive_file_id or drive_file_id in existing:
            continue
        added.append(drive_file_id)
        existing.add(drive_file_id)
    assert added == ["file-2"]
    assert "file-1" in existing


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
async def test_collections_create_add_duplicate_and_share(client):
    headers = _auth_header("owner@mastersunion.org")
    create = await client.post("/collections", json={"name": " Summit picks "}, headers=headers)
    assert create.status_code == 201
    body = create.json()
    assert body["name"] == "Summit picks"
    assert body["is_owner"] is True
    assert body["items"] == []
    collection_id = body["id"]

    add = await client.post(
        f"/collections/{collection_id}/items",
        headers=headers,
        json={
            "items": [
                {
                    "drive_file_id": "file-1",
                    "name": "a.jpg",
                    "mime_type": "image/jpeg",
                    "query": "summit stage",
                }
            ]
        },
    )
    assert add.status_code == 200
    assert len(add.json()["items"]) == 1

    # Whitespace-padded id must still count as a duplicate after strip.
    dup = await client.post(
        f"/collections/{collection_id}/items",
        headers=headers,
        json={
            "items": [
                {
                    "drive_file_id": "  file-1  ",
                    "name": "a.jpg",
                    "mime_type": "image/jpeg",
                }
            ]
        },
    )
    assert dup.status_code == 200
    assert len(dup.json()["items"]) == 1

    listed_with_file = await client.get("/collections?file_id=file-1", headers=headers)
    assert listed_with_file.status_code == 200
    mine = listed_with_file.json()["mine"]
    assert len(mine) == 1
    assert mine[0]["contains_file"] is True

    listed_other_file = await client.get("/collections?file_id=missing", headers=headers)
    assert listed_other_file.json()["mine"][0]["contains_file"] is False

    other = _auth_header("other@mastersunion.org")
    forbidden = await client.post(
        f"/collections/{collection_id}/items",
        headers=other,
        json={
            "items": [
                {
                    "drive_file_id": "file-2",
                    "name": "b.jpg",
                    "mime_type": "image/jpeg",
                }
            ]
        },
    )
    assert forbidden.status_code == 403

    shared = await client.get(f"/collections/{collection_id}", headers=other)
    assert shared.status_code == 200
    shared_body = shared.json()
    assert shared_body["is_owner"] is False
    assert len(shared_body["items"]) == 1
    assert shared_body["items"][0]["drive_file_id"] == "file-1"

    listed = await client.get("/collections", headers=other)
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["mine"] == []
    assert len(payload["shared"]) == 1
    assert payload["shared"][0]["id"] == collection_id
    assert payload["shared"][0]["owner_email"] == "owner@mastersunion.org"
    assert payload["shared"][0]["contains_file"] is False

    removed = await client.delete(f"/collections/{collection_id}/view", headers=other)
    assert removed.status_code == 204
    listed2 = await client.get("/collections", headers=other)
    assert listed2.json()["shared"] == []


@requires_postgres
@pytest.mark.asyncio
async def test_collections_list_requires_bearer(client):
    missing = await client.get("/collections")
    assert missing.status_code == 401

    bad = await client.get("/collections", headers={"Authorization": "Bearer not-valid"})
    assert bad.status_code == 401

    headers = _auth_header("owner@mastersunion.org")
    await client.post("/collections", json={"name": "Mine"}, headers=headers)
    listed = await client.get("/collections", headers=headers)
    assert listed.status_code == 200
    payload = listed.json()
    assert len(payload["mine"]) == 1
    assert payload["mine"][0]["item_count"] == 0
    assert payload["mine"][0]["contains_file"] is False
    assert payload["shared"] == []


@requires_postgres
@pytest.mark.asyncio
async def test_unset_secret_http_503(client, monkeypatch):
    monkeypatch.setenv("APP_SESSION_SECRET", "")
    get_settings.cache_clear()
    monkeypatch.setattr(auth_session, "_missing_warned", False)
    res = await client.get(
        "/collections",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert res.status_code == 503
    assert "APP_SESSION_SECRET" in res.json()["detail"]
