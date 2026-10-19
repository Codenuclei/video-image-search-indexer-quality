"""End-to-end HTTP tests for the FastAPI routers, against a real Postgres instance."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import app.auth_session as auth_session
from app.auth_session import seal_app_session
from app.config import get_settings
from app.db.models import DriveFile, DriveFileStatus, Face, FaceCluster, FaceEmbedding, Media, MediaType
from app.db.session import get_db
from app.main import app
from tests.conftest import requires_postgres

EMBEDDING_DIM = 512
TEST_SESSION_SECRET = "test-app-session-secret-ok"  # >= 16 chars


def _unit_vector(hot_index: int) -> list[float]:
    vec = [0.0] * EMBEDDING_DIM
    vec[hot_index] = 1.0
    return vec


@pytest.fixture
def override_db(db_session):
    async def _get_db_override():
        yield db_session

    app.dependency_overrides[get_db] = _get_db_override
    yield db_session
    app.dependency_overrides.pop(get_db, None)


@pytest_asyncio.fixture
async def client(override_db):
    # ASGITransport skips the lifespan, so mark boot complete: /health and the
    # boot-gate middleware are readiness-gated in production.
    import app.main as main_module

    main_module._boot_ready.set()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _seed_unknown_cluster(session) -> tuple[Media, Face, FaceCluster]:
    drive_file = DriveFile(
        id=f"drive-{uuid.uuid4().hex}", name="a.jpg", mime_type="image/jpeg", path="/a.jpg", status=DriveFileStatus.PROCESSED
    )
    session.add(drive_file)
    await session.flush()
    media = Media(drive_file_id=drive_file.id, type=MediaType.IMAGE)
    session.add(media)
    await session.flush()
    face = Face(media_id=media.id, bbox_x=0, bbox_y=0, bbox_width=1, bbox_height=1, detection_confidence=0.9)
    session.add(face)
    await session.flush()
    session.add(FaceEmbedding(face_id=face.id, embedding=_unit_vector(0)))
    cluster = FaceCluster(centroid=_unit_vector(0), member_count=1, representative_face_id=face.id)
    session.add(cluster)
    await session.flush()
    face.cluster_id = cluster.id
    await session.commit()
    return media, face, cluster


@requires_postgres
@pytest.mark.asyncio
async def test_health_endpoint(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "themes_runtime": "themes-v5-single-pass-strict-provider",
    }


@requires_postgres
@pytest.mark.asyncio
async def test_drive_files_endpoint_lists_seeded_file(client, override_db):
    override_db.add(
        DriveFile(id="f1", name="a.jpg", mime_type="image/jpeg", path="/a.jpg", status=DriveFileStatus.PENDING)
    )
    await override_db.commit()

    response = await client.get("/drive/files")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["id"] == "f1"
    assert body[0]["status"] == "pending"


@requires_postgres
@pytest.mark.asyncio
async def test_clusters_review_queue_lists_unknown_cluster(client, override_db):
    _, face, cluster = await _seed_unknown_cluster(override_db)

    response = await client.get("/clusters")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["id"] == cluster.id
    assert body["items"][0]["status"] == "unknown"
    assert body["items"][0]["representative_face_id"] == face.id
    assert len(body["items"][0]["appears_in"]) == 1


@requires_postgres
@pytest.mark.asyncio
async def test_name_cluster_endpoint_creates_person(client, override_db):
    _, _, cluster = await _seed_unknown_cluster(override_db)

    response = await client.post(f"/clusters/{cluster.id}/name", json={"name": "Diana"})

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Diana"
    assert body["occurrence_count"] == 1

    persons_response = await client.get("/persons")
    assert any(p["name"] == "Diana" for p in persons_response.json())

    review_queue = await client.get("/clusters")
    assert review_queue.json()["items"] == []


@requires_postgres
@pytest.mark.asyncio
async def test_ignore_cluster_endpoint_removes_it_from_default_queue(client, override_db):
    _, _, cluster = await _seed_unknown_cluster(override_db)

    response = await client.post(f"/clusters/{cluster.id}/ignore")
    assert response.status_code == 204

    queue = await client.get("/clusters")
    assert queue.json()["items"] == []

    with_ignored = await client.get("/clusters", params={"include_ignored": True})
    assert len(with_ignored.json()["items"]) == 1


@requires_postgres
@pytest.mark.asyncio
async def test_search_persons_endpoint(client, override_db):
    _, _, cluster = await _seed_unknown_cluster(override_db)
    await client.post(f"/clusters/{cluster.id}/name", json={"name": "Diana Adams"})

    empty = await client.get("/persons/search", params={"q": ""})
    assert empty.json() == []

    match = await client.get("/persons/search", params={"q": "diana"})
    assert len(match.json()) == 1
    assert match.json()[0]["name"] == "Diana Adams"

    # First-name substring (Search Index #95 / merge picker "Nikhil" → "Nikhil Mittal").
    _, _, nikhil_cluster = await _seed_unknown_cluster(override_db)
    named = await client.post(f"/clusters/{nikhil_cluster.id}/name", json={"name": "Nikhil Mittal"})
    assert named.status_code == 200
    first_name = await client.get("/persons/search", params={"q": "Nikhil"})
    assert any(row["name"] == "Nikhil Mittal" for row in first_name.json())

    miss = await client.get("/persons/search", params={"q": "nobody"})
    assert miss.json() == []


@requires_postgres
@pytest.mark.asyncio
async def test_name_cluster_endpoint_404_for_missing_cluster(client):
    response = await client.post("/clusters/999999/name", json={"name": "Nobody"})
    assert response.status_code == 404


@requires_postgres
@pytest.mark.asyncio
async def test_settings_get_and_update_roundtrip(client):
    get_response = await client.get("/settings")
    assert get_response.status_code == 200
    original = get_response.json()

    update_response = await client.put(
        "/settings",
        json={"person_match_threshold": 0.6, "auto_index_enabled": True, "auto_index_interval_seconds": 120},
    )
    assert update_response.status_code == 200
    body = update_response.json()
    assert body["person_match_threshold"] == 0.6
    assert body["auto_index_enabled"] is True
    assert body["auto_index_interval_seconds"] == 120

    # restore, so this test doesn't leak global runtime state into other tests
    await client.put(
        "/settings",
        json={
            "person_match_threshold": original["person_match_threshold"],
            "auto_index_enabled": original["auto_index_enabled"],
            "auto_index_interval_seconds": original["auto_index_interval_seconds"],
        },
    )


@requires_postgres
@pytest.mark.asyncio
async def test_search_finds_named_person_and_matching_file(client, override_db):
    _, _, cluster = await _seed_unknown_cluster(override_db)
    await client.post(f"/clusters/{cluster.id}/name", json={"name": "Eve Adams"})

    override_db.add(
        DriveFile(id="f2", name="beach-trip.jpg", mime_type="image/jpeg", path="/beach-trip.jpg", status=DriveFileStatus.PENDING)
    )
    await override_db.commit()

    person_search = await client.get("/search", params={"q": "Eve"})
    assert any(p["name"] == "Eve Adams" for p in person_search.json()["persons"])

    file_search = await client.get("/search", params={"q": "beach"})
    assert any(m["name"] == "beach-trip.jpg" for m in file_search.json()["media"])


@requires_postgres
@pytest.mark.asyncio
async def test_collections_saved_files_returns_owned_subset(client, monkeypatch):
    monkeypatch.setenv("APP_SESSION_SECRET", TEST_SESSION_SECRET)
    get_settings.cache_clear()
    monkeypatch.setattr(auth_session, "_missing_warned", False)

    headers = {"Authorization": f"Bearer {seal_app_session('owner@mastersunion.org')}"}
    other = {"Authorization": f"Bearer {seal_app_session('other@mastersunion.org')}"}

    create = await client.post("/collections", json={"name": "Saved tint"}, headers=headers)
    assert create.status_code == 201
    collection_id = create.json()["id"]

    add = await client.post(
        f"/collections/{collection_id}/items",
        headers=headers,
        json={
            "items": [
                {
                    "drive_file_id": "file-saved",
                    "name": "a.jpg",
                    "mime_type": "image/jpeg",
                },
                {
                    "drive_file_id": "file-also",
                    "name": "b.jpg",
                    "mime_type": "image/jpeg",
                },
            ]
        },
    )
    assert add.status_code == 200

    owned = await client.get(
        "/collections/saved-files",
        params={"file_ids": "file-saved,missing,file-also,file-saved"},
        headers=headers,
    )
    assert owned.status_code == 200
    assert set(owned.json()["saved_file_ids"]) == {"file-saved", "file-also"}

    stranger = await client.get(
        "/collections/saved-files",
        params={"file_ids": "file-saved,file-also"},
        headers=other,
    )
    assert stranger.status_code == 200
    assert stranger.json()["saved_file_ids"] == []

    empty = await client.get("/collections/saved-files", headers=headers)
    assert empty.status_code == 200
    assert empty.json()["saved_file_ids"] == []

    missing_auth = await client.get(
        "/collections/saved-files",
        params={"file_ids": "file-saved"},
    )
    assert missing_auth.status_code == 401
