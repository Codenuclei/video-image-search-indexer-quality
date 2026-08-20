"""HTTP + unit coverage for library revision/shell/folder TTFB endpoints."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.db.models import DriveFile, DriveFileStatus
from app.drive.library_shell_cache import LibraryShellCache, get_library_shell_cache
from tests.conftest import requires_postgres


@pytest.mark.asyncio
async def test_shell_cache_304_and_hit_without_rebuild():
    """Unit: cache serves same payload; invalidate clears."""
    cache = LibraryShellCache()
    cache.put("rev-a", {"revision": "rev-a", "tree": {"files": []}})
    assert cache.get("rev-a")["revision"] == "rev-a"
    cache.invalidate()
    assert cache.get("rev-a") is None


@requires_postgres
@pytest.mark.asyncio
async def test_http_library_revision_shell_folder(db_session):
    """ASGI: revision → shell (miss then hit/304) → folder thin payload."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db.session import get_db
    from app.main import app
    from tests.conftest import TEST_DATABASE_URL

    get_library_shell_cache().invalidate()

    for i in range(5):
        db_session.add(
            DriveFile(
                id=f"shell-{i}",
                name=f"img-{i}.jpg",
                mime_type="image/jpeg",
                path=f"/Album/img-{i}.jpg",
                status=DriveFileStatus.PROCESSED if i % 2 == 0 else DriveFileStatus.PENDING,
                last_synced_at=datetime.now(timezone.utc),
            )
        )
    # Nested file should not appear as direct child of /Album
    db_session.add(
        DriveFile(
            id="nested-1",
            name="deep.jpg",
            mime_type="image/jpeg",
            path="/Album/Sub/deep.jpg",
            status=DriveFileStatus.PROCESSED,
            last_synced_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    engine = create_async_engine(TEST_DATABASE_URL, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _get_db_override():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db_override

    try:
        transport = ASGITransport(app=app)
        with (
            patch("app.qdrant.image_captions.valid_caption_ids_sync", return_value=set()),
            patch("app.qdrant.images.existing_image_ids_sync", return_value=set()),
            patch("app.qdrant.image_captions.get_captions_by_ids_sync", return_value={}),
        ):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                rev = await ac.get("/drive/library/revision")
                assert rev.status_code == 200
                revision = rev.json()["revision"]
                assert revision.startswith("6:")
                assert "processed:" in revision

                miss = await ac.get("/drive/library/shell")
                assert miss.status_code == 200
                assert miss.headers.get("x-library-cache") == "miss"
                assert miss.headers.get("cache-control") == "private, max-age=5"
                assert miss.headers.get("etag") == f'"{revision}"'
                body = miss.json()
                assert body["revision"] == revision
                assert body["summary"]["caption_stats_ready"] is False
                assert body["tree"]["files"] == []
                # Folder node should exist without nested file lists
                album = next(f for f in body["tree"]["folders"] if f["name"] == "Album")
                assert album["files"] == []
                assert album["file_count"] >= 5

                hit = await ac.get("/drive/library/shell")
                assert hit.status_code == 200
                assert hit.headers.get("x-library-cache") == "hit"
                assert hit.json()["revision"] == revision

                not_mod = await ac.get(
                    "/drive/library/shell",
                    headers={"If-None-Match": f'"{revision}"'},
                )
                assert not_mod.status_code == 304

                folder = await ac.get(
                    "/drive/library/folder",
                    params={"path": "/Album", "limit": 3},
                )
                assert folder.status_code == 200
                page = folder.json()
                assert page["path"] == "/Album"
                assert page["total"] == 5  # not nested deep.jpg
                assert len(page["files"]) == 3
                assert page["next_cursor"] is not None
                for f in page["files"]:
                    assert f["caption_preview"] is None
                    assert "error_message" in f
                    assert f["folder_path"] == "/Album"
                    assert "/" not in f["name"]  # direct child name only

                page2 = await ac.get(
                    "/drive/library/folder",
                    params={"path": "/Album", "limit": 3, "cursor": page["next_cursor"]},
                )
                assert page2.status_code == 200
                assert len(page2.json()["files"]) == 2
                assert page2.json()["next_cursor"] is None
    finally:
        app.dependency_overrides.pop(get_db, None)
        get_library_shell_cache().invalidate()
        await engine.dispose()
