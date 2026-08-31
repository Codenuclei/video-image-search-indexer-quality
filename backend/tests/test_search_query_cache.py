"""Exact + semantic search cache, folder fingerprints, and tab-session contracts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.db.models import (
    ClusterStatus,
    DriveFile,
    DriveFileStatus,
    Face,
    FaceCluster,
    Media,
    MediaType,
    Person,
)
from app.schemas import SearchResponse, SearchResultFile
from app.search.query_cache import (
    SEARCH_CACHE_VERSION,
    SEMANTIC_MIN_COSINE,
    cosine_similarity,
    exact_row_is_fresh,
    fingerprints_valid,
    lookup_exact,
    lookup_semantic,
    make_cache_key,
    row_matches_search_cache_version,
    store_search_cache,
)
from tests.conftest import requires_postgres

REPO = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _response(query: str, file_id: str = "f1") -> SearchResponse:
    return SearchResponse(
        query=query,
        answer="",
        citations=[],
        files=[
            SearchResultFile(
                drive_file_id=file_id,
                name=f"{file_id}.jpg",
                path=f"/Events/{file_id}.jpg",
                mime_type="image/jpeg",
                score=0.9,
            )
        ],
        cache="miss",
    )


def test_semantic_cache_requires_current_logic_version() -> None:
    current = SimpleNamespace(
        response_json={"_search_cache_version": SEARCH_CACHE_VERSION, "query": "x"}
    )
    stale = SimpleNamespace(response_json={"query": "x"})
    other = SimpleNamespace(response_json={"_search_cache_version": "v1-old"})
    assert row_matches_search_cache_version(current) is True
    assert row_matches_search_cache_version(stale) is False
    assert row_matches_search_cache_version(other) is False


async def _add_file(session, *, file_id: str, path: str, synced: datetime) -> DriveFile:
    row = DriveFile(
        id=file_id,
        name=path.rsplit("/", 1)[-1],
        mime_type="image/jpeg",
        path=path,
        status=DriveFileStatus.PROCESSED,
        last_synced_at=synced,
    )
    session.add(row)
    await session.flush()
    return row


def test_cache_key_normalizes_query_and_folder() -> None:
    a = make_cache_key(
        query=" Wine Glass ",
        person=None,
        mime="all",
        folder_path="/Events/",
        captions=False,
        rerank=True,
    )
    b = make_cache_key(
        query="wine glass",
        person="",
        mime="ALL",
        folder_path="/Events",
        captions=False,
        rerank=True,
    )
    assert a == b


def test_cosine_similarity_balanced_paraphrase_bar() -> None:
    wine = [1.0, 0.0, 0.0]
    glass_of_wine = [0.94, 0.34, 0.0]
    unrelated = [0.0, 0.0, 1.0]
    assert cosine_similarity(wine, glass_of_wine) >= SEMANTIC_MIN_COSINE
    assert cosine_similarity(wine, unrelated) < SEMANTIC_MIN_COSINE


def test_exact_cache_freshness_window() -> None:
    now = datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc)
    assert exact_row_is_fresh(SimpleNamespace(created_at=now - timedelta(minutes=9)), now)
    assert not exact_row_is_fresh(SimpleNamespace(created_at=now - timedelta(minutes=11)), now)


def test_fingerprints_folder_vs_global_person() -> None:
    folder_row = SimpleNamespace(folder_path="/Events", person="", folder_fp="1:t0", cluster_fp="7:2")
    assert fingerprints_valid(folder_row, "1:t0", "7:2") is True
    assert fingerprints_valid(folder_row, "2:t1", "7:2") is False
    assert fingerprints_valid(folder_row, "1:t0", "7:3") is False

    global_visual = SimpleNamespace(folder_path="", person="", folder_fp="10:t0", cluster_fp="")
    assert fingerprints_valid(global_visual, "10:t0", "") is True
    assert fingerprints_valid(global_visual, "11:t1", "") is False

    global_person = SimpleNamespace(folder_path="", person="Ada", folder_fp="10:t0", cluster_fp="7:2")
    assert fingerprints_valid(global_person, "11:t1", "7:2") is True
    assert fingerprints_valid(global_person, "11:t1", "7:3") is False


def test_grids_use_thumbs_enlarge_uses_preview() -> None:
    repo = Path(__file__).resolve().parents[2]
    ui = (repo / "frontend/src/components/ui.tsx").read_text()
    search = (repo / "frontend/src/app/search/page.tsx").read_text()
    api = (repo / "frontend/src/lib/api.ts").read_text()
    library = (repo / "frontend/src/app/library/page.tsx").read_text()
    assert "driveFileThumbnailUrl" in ui
    assert "src={thumbUrl}" in ui
    assert "driveFilePreviewUrl(previewFile.drive_file_id" in search
    assert "/thumbnail" in api
    assert "driveFileThumbnailUrl" in library
    assert 'src={`https://drive.google.com' not in ui
    assert "drive.google.com/thumbnail" not in api
    assert "lh3.googleusercontent.com" not in api
    search_src = (REPO / "frontend/src/lib/search-session.ts").read_text()
    reverse_src = (REPO / "frontend/src/lib/reverse-face-session.ts").read_text()
    search_page = (REPO / "frontend/src/app/search/page.tsx").read_text()
    reverse_page = (REPO / "frontend/src/app/labs/reverse-face/page.tsx").read_text()
    assert "AbortController" not in search_src
    assert "let inFlight" in search_src
    assert "useSyncExternalStore" in search_src
    assert "useSearchSession" in search_page
    assert "AbortController" not in reverse_src
    assert "useSyncExternalStore" in reverse_src
    assert "useReverseFaceSession" in reverse_page


@pytest.mark.asyncio
async def test_search_exact_hit_skips_gemini_rerank(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.routers.search import search

    cached = SearchResponse(query="wine glass", answer="", citations=[], cache="exact")
    monkeypatch.setattr("app.search.query_cache.lookup_exact", AsyncMock(return_value=cached))
    with patch("app.routers.search.rerank_image_files", new_callable=AsyncMock) as rerank:
        result = await search(q="wine glass", session=AsyncMock())
    assert result.cache == "exact"
    rerank.assert_not_called()


@requires_postgres
@pytest.mark.asyncio
async def test_exact_hit_then_folder_add_misses(db_session) -> None:
    await _add_file(db_session, file_id="a1", path="/Events/a1.jpg", synced=T0)
    await db_session.commit()
    await store_search_cache(
        db_session,
        query="wine glass",
        person=None,
        mime="all",
        folder_path="/Events",
        captions=False,
        rerank=True,
        response=_response("wine glass", "a1"),
        query_embedding=[1.0, 0.0, 0.0],
    )
    hit = await lookup_exact(
        db_session,
        query="wine glass",
        person=None,
        mime="all",
        folder_path="/Events",
        captions=False,
        rerank=True,
    )
    assert hit is not None
    assert hit.cache == "exact"

    await _add_file(db_session, file_id="a2", path="/Events/a2.jpg", synced=T0 + timedelta(hours=1))
    await db_session.commit()
    miss = await lookup_exact(
        db_session,
        query="wine glass",
        person=None,
        mime="all",
        folder_path="/Events",
        captions=False,
        rerank=True,
    )
    assert miss is None


@requires_postgres
@pytest.mark.asyncio
async def test_other_folder_add_keeps_folder_cache(db_session) -> None:
    await _add_file(db_session, file_id="a1", path="/Events/a1.jpg", synced=T0)
    await db_session.commit()
    await store_search_cache(
        db_session,
        query="wine glass",
        person=None,
        mime="all",
        folder_path="/Events",
        captions=False,
        rerank=True,
        response=_response("wine glass", "a1"),
        query_embedding=[1.0, 0.0, 0.0],
    )
    await _add_file(db_session, file_id="b1", path="/Other/b1.jpg", synced=T0 + timedelta(hours=2))
    await db_session.commit()
    hit = await lookup_exact(
        db_session,
        query="wine glass",
        person=None,
        mime="all",
        folder_path="/Events",
        captions=False,
        rerank=True,
    )
    assert hit is not None
    assert hit.cache == "exact"


@requires_postgres
@pytest.mark.asyncio
async def test_paraphrase_same_folder_semantic_hit(db_session) -> None:
    await _add_file(db_session, file_id="a1", path="/Events/a1.jpg", synced=T0)
    await db_session.commit()
    await store_search_cache(
        db_session,
        query="wine glass",
        person=None,
        mime="all",
        folder_path="/Events",
        captions=False,
        rerank=True,
        response=_response("wine glass", "a1"),
        query_embedding=[1.0, 0.0, 0.0],
    )
    hit = await lookup_semantic(
        db_session,
        query_embedding=[0.94, 0.34, 0.0],
        person=None,
        mime="all",
        folder_path="/Events",
        captions=False,
        rerank=True,
    )
    assert hit is not None
    assert hit.cache == "semantic"
    assert hit.query == "wine glass"

    other_folder = await lookup_semantic(
        db_session,
        query_embedding=[0.94, 0.34, 0.0],
        person=None,
        mime="all",
        folder_path="/Other",
        captions=False,
        rerank=True,
    )
    assert other_folder is None


@requires_postgres
@pytest.mark.asyncio
async def test_paraphrase_stale_folder_misses(db_session) -> None:
    await _add_file(db_session, file_id="a1", path="/Events/a1.jpg", synced=T0)
    await db_session.commit()
    await store_search_cache(
        db_session,
        query="wine glass",
        person=None,
        mime="all",
        folder_path="/Events",
        captions=False,
        rerank=True,
        response=_response("wine glass", "a1"),
        query_embedding=[1.0, 0.0, 0.0],
    )
    await _add_file(db_session, file_id="a2", path="/Events/a2.jpg", synced=T0 + timedelta(hours=1))
    await db_session.commit()
    miss = await lookup_semantic(
        db_session,
        query_embedding=[0.94, 0.34, 0.0],
        person=None,
        mime="all",
        folder_path="/Events",
        captions=False,
        rerank=True,
    )
    assert miss is None


@requires_postgres
@pytest.mark.asyncio
async def test_global_person_cache_misses_when_new_file_joins_cluster(db_session) -> None:
    file_a = await _add_file(db_session, file_id="ada1", path="/Events/ada1.jpg", synced=T0)
    media = Media(drive_file_id=file_a.id, type=MediaType.IMAGE)
    db_session.add(media)
    await db_session.flush()
    person = Person(name="Ada")
    db_session.add(person)
    await db_session.flush()
    cluster = FaceCluster(status=ClusterStatus.NAMED, person_id=person.id, member_count=1)
    db_session.add(cluster)
    await db_session.flush()
    db_session.add(
        Face(
            media_id=media.id,
            bbox_x=0.0,
            bbox_y=0.0,
            bbox_width=10.0,
            bbox_height=10.0,
            detection_confidence=0.99,
            cluster_id=cluster.id,
            person_id=person.id,
        )
    )
    await db_session.commit()

    await store_search_cache(
        db_session,
        query="Ada smiling",
        person="Ada",
        mime="all",
        folder_path="",
        captions=False,
        rerank=True,
        response=_response("Ada smiling", "ada1"),
        query_embedding=[1.0, 0.0, 0.0],
    )
    hit = await lookup_exact(
        db_session,
        query="Ada smiling",
        person="Ada",
        mime="all",
        folder_path="",
        captions=False,
        rerank=True,
    )
    assert hit is not None

    unrelated = await _add_file(
        db_session, file_id="noise", path="/Other/noise.jpg", synced=T0 + timedelta(hours=3)
    )
    db_session.add(Media(drive_file_id=unrelated.id, type=MediaType.IMAGE))
    await db_session.commit()
    still = await lookup_exact(
        db_session,
        query="Ada smiling",
        person="Ada",
        mime="all",
        folder_path="",
        captions=False,
        rerank=True,
    )
    assert still is not None
    assert still.cache == "exact"

    new_file = await _add_file(
        db_session, file_id="ada2", path="/Other/ada2.jpg", synced=T0 + timedelta(hours=4)
    )
    new_media = Media(drive_file_id=new_file.id, type=MediaType.IMAGE)
    db_session.add(new_media)
    await db_session.flush()
    db_session.add(
        Face(
            media_id=new_media.id,
            bbox_x=0.0,
            bbox_y=0.0,
            bbox_width=10.0,
            bbox_height=10.0,
            detection_confidence=0.99,
            cluster_id=cluster.id,
            person_id=person.id,
        )
    )
    await db_session.commit()
    miss = await lookup_exact(
        db_session,
        query="Ada smiling",
        person="Ada",
        mime="all",
        folder_path="",
        captions=False,
        rerank=True,
    )
    assert miss is None


@requires_postgres
@pytest.mark.asyncio
async def test_global_visual_cache_misses_on_library_growth(db_session) -> None:
    await _add_file(db_session, file_id="v1", path="/Events/v1.jpg", synced=T0)
    await db_session.commit()
    await store_search_cache(
        db_session,
        query="wine glass",
        person=None,
        mime="all",
        folder_path="",
        captions=False,
        rerank=True,
        response=_response("wine glass", "v1"),
        query_embedding=[1.0, 0.0, 0.0],
    )
    await _add_file(db_session, file_id="v2", path="/Other/v2.jpg", synced=T0 + timedelta(hours=1))
    await db_session.commit()
    miss = await lookup_exact(
        db_session,
        query="wine glass",
        person=None,
        mime="all",
        folder_path="",
        captions=False,
        rerank=True,
    )
    assert miss is None
