"""HTTP-style tests for library shell without requiring Postgres.

Patches SQL revision + row load so we can verify ETag/304/cache headers.
"""
from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.drive.library_shell_cache import get_library_shell_cache
from app.routers.drive import drive_library_folder, drive_library_revision, drive_library_shell


def _status(value: str):
    return SimpleNamespace(value=value)


def _drive_file(**kwargs):
    defaults = dict(
        id="f1",
        name="a.jpg",
        path="/Album/a.jpg",
        mime_type="image/jpeg",
        status=_status("processed"),
        size=100,
        source="drive",
        error_message=None,
        last_synced_at=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@pytest.mark.asyncio
async def test_shell_endpoint_miss_hit_304_without_postgres():
    get_library_shell_cache().invalidate()

    session = AsyncMock()
    # Not used when revision is patched, but keep a safe default.
    session.execute = AsyncMock()

    with (
        patch(
            "app.routers.drive.compute_library_revision_sql",
            new=AsyncMock(return_value="2:2020-01-01T00:00:00+00:00:processed:2"),
        ),
        patch("app.routers.drive.load_paused_folder_paths", new=AsyncMock(return_value=[])),
        patch(
            "app.routers.drive.select",
            return_value=MagicMock(),
        ),
        patch.object(
            session,
            "execute",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    scalars=lambda: SimpleNamespace(
                        all=lambda: [
                            _drive_file(id="1", name="a.jpg", path="/Album/a.jpg"),
                            _drive_file(id="2", name="b.jpg", path="/Album/b.jpg", status=_status("pending")),
                        ]
                    )
                )
            ),
        ),
        patch("app.workers.maintenance.maintenance_status", return_value={"caption_backfill_running": False}),
    ):
        miss = await drive_library_shell(session=session, if_none_match=None)
        assert miss.status_code == 200
        assert miss.headers["X-Library-Cache"] == "miss"
        assert miss.headers["Cache-Control"] == "private, max-age=5"
        body = miss.body
        assert b'"files":[]' in body or b'"files": []' in body
        assert b"caption_stats_ready" in body

        hit = await drive_library_shell(session=session, if_none_match=None)
        assert hit.status_code == 200
        assert hit.headers["X-Library-Cache"] == "hit"

        etag = miss.headers["ETag"]
        not_mod = await drive_library_shell(session=session, if_none_match=etag)
        assert not_mod.status_code == 304

    get_library_shell_cache().invalidate()


@pytest.mark.asyncio
async def test_revision_endpoint_uses_sql_helper():
    session = AsyncMock()
    with patch(
        "app.routers.drive.compute_library_revision_sql",
        new=AsyncMock(return_value="9::pending:9"),
    ) as rev:
        out = await drive_library_revision(session=session)
        assert out == {"revision": "9::pending:9"}
        rev.assert_awaited_once()


@pytest.mark.asyncio
async def test_folder_endpoint_thin_and_paginated_without_postgres():
    files = [
        _drive_file(id=f"id-{i}", name=f"img-{i}.jpg", path=f"/Album/img-{i}.jpg")
        for i in range(5)
    ]
    session = MagicMock()
    session.execute = MagicMock(
        return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: files))
    )
    reader = SimpleNamespace(
        run=AsyncMock(side_effect=lambda fn, *args: fn(*args)),
        session=lambda: nullcontext(session),
    )
    get_library_shell_cache().put_revision("5::processed:5")

    with (
        patch("app.routers.drive.get_library_reader_runtime", return_value=reader),
        patch("app.qdrant.image_captions.valid_caption_ids_sync", return_value=set()),
        patch("app.qdrant.images.existing_image_ids_sync", return_value=set()),
    ):
        page = await drive_library_folder(path="/Album", limit=2, cursor=None)
        assert page["total"] == 5
        assert len(page["files"]) == 2
        assert page["next_cursor"] == "id-1"
        assert page["files"][0]["caption_preview"] is None
        assert page["files"][0]["folder_path"] == "/Album"

        page2 = await drive_library_folder(path="/Album", limit=2, cursor="id-1")
        assert len(page2["files"]) == 2
        assert page2["next_cursor"] == "id-3"


@pytest.mark.asyncio
async def test_folder_endpoint_accepts_historical_paths_without_leading_slash():
    files = [
        _drive_file(id="direct", name="a.jpg", path="Dump/a.jpg"),
        _drive_file(id="nested", name="b.jpg", path="Dump/Nested/b.jpg"),
        _drive_file(id="other", name="c.jpg", path="Other/c.jpg"),
    ]
    session = MagicMock()
    session.execute = MagicMock(
        return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: files))
    )
    reader = SimpleNamespace(
        run=AsyncMock(side_effect=lambda fn, *args: fn(*args)),
        session=lambda: nullcontext(session),
    )
    get_library_shell_cache().put_revision("3::processed:3")

    with (
        patch("app.routers.drive.get_library_reader_runtime", return_value=reader),
        patch("app.qdrant.image_captions.valid_caption_ids_sync", return_value=set()),
        patch("app.qdrant.images.existing_image_ids_sync", return_value=set()),
    ):
        page = await drive_library_folder(path="/Dump", limit=150, cursor=None)

    assert page["total"] == 1
    assert [item["id"] for item in page["files"]] == ["direct"]
