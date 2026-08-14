"""Never-delete / soft-archive + global library visibility (P0)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import DriveFile, DriveFileStatus, DriveUser, Media, MediaType
from app.drive.cleanup import remove_drive_file
from app.drive.library_tree import build_library_tree
from app.drive.schemas import ConnectorFile, ConnectorFolder, ConnectorFolderListing
from app.workers.indexer import IndexingWorker
from tests.conftest import requires_postgres


class _FakeDriveClient:
    def __init__(self, listing: ConnectorFolderListing) -> None:
        self._listing = listing

    async def list_folder_files(self) -> ConnectorFolderListing:
        return self._listing


def _listing(
    files: list[ConnectorFile],
    *,
    root_id: str = "root-a",
    root_name: str = "FolderA",
) -> ConnectorFolderListing:
    return ConnectorFolderListing(
        folder=ConnectorFolder(id=root_id, name=root_name),
        files=files,
        truncated=False,
    )


def _file(
    id_: str,
    name: str,
    mime: str = "image/jpeg",
    *,
    parent: str = "root-a",
    path: str | None = None,
) -> ConnectorFile:
    return ConnectorFile(
        id=id_,
        name=name,
        mimeType=mime,
        isFolder=False,
        parentId=parent,
        path=path or f"/{name}",
        modifiedTime=datetime.now(timezone.utc).isoformat(),
    )


class _NoCloseSessionCtx:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc) -> bool:
        return False


def _session_factory(db_session: AsyncSession) -> async_sessionmaker:
    class _Factory:
        def __call__(self):
            return _NoCloseSessionCtx(db_session)

    return _Factory()  # type: ignore[return-value]


def _fail_if_qdrant_delete(*_a, **_k):
    raise AssertionError("Qdrant delete must never run on folder switch / soft-archive")


@requires_postgres
@pytest.mark.asyncio
async def test_sync_soft_archives_stale_same_root_without_qdrant_delete(db_session):
    """PROCESSED stale files stay PROCESSED; incomplete queue rows may soft-archive."""
    for fid, name, status in (
        ("keep", "keep.jpg", DriveFileStatus.PROCESSED),
        ("gone", "gone.jpg", DriveFileStatus.PROCESSED),
        ("pend-gone", "pend.jpg", DriveFileStatus.PENDING),
    ):
        db_session.add(
            DriveFile(
                id=fid,
                name=name,
                mime_type="image/jpeg",
                path=f"/{name}",
                status=status,
                root_folder_id="root-a",
                last_synced_at=datetime.now(timezone.utc),
                gemini_document_name=f"docs/{fid}",
                cache_rel_path=f"cache/{fid}.jpg",
            )
        )
        await db_session.flush()
        if status == DriveFileStatus.PROCESSED:
            db_session.add(Media(drive_file_id=fid, type=MediaType.IMAGE))
    await db_session.commit()

    listing = _listing([_file("keep", "keep.jpg")])
    worker = IndexingWorker(
        session_factory=_session_factory(db_session),
        client=_FakeDriveClient(listing),
    )

    with (
        patch("app.qdrant.images.delete_image_sync", side_effect=_fail_if_qdrant_delete),
        patch("app.qdrant.image_captions.delete_caption_sync", side_effect=_fail_if_qdrant_delete),
    ):
        await worker.sync_file_list()

    keep = await db_session.get(DriveFile, "keep")
    gone = await db_session.get(DriveFile, "gone")
    pend = await db_session.get(DriveFile, "pend-gone")
    assert keep is not None and keep.status == DriveFileStatus.PROCESSED
    assert gone is not None
    assert gone.status == DriveFileStatus.PROCESSED  # permanent library
    assert pend is not None
    assert pend.status == DriveFileStatus.ARCHIVED
    media = (
        await db_session.execute(select(Media).where(Media.drive_file_id == "gone"))
    ).scalar_one_or_none()
    assert media is not None
    assert gone.gemini_document_name == "docs/gone"
    assert gone.cache_rel_path == "cache/gone.jpg"


@requires_postgres
@pytest.mark.asyncio
async def test_sync_never_touches_other_root_on_folder_switch(db_session):
    """Switching sync root must not archive or delete files from a prior root."""
    db_session.add(
        DriveFile(
            id="old-root-file",
            name="old.jpg",
            mime_type="image/jpeg",
            path="/OldFolder/old.jpg",
            status=DriveFileStatus.PROCESSED,
            root_folder_id="root-old",
            last_synced_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    listing = _listing(
        [_file("new1", "new.jpg", parent="root-b", path="/NewFolder/new.jpg")],
        root_id="root-b",
        root_name="FolderB",
    )
    worker = IndexingWorker(
        session_factory=_session_factory(db_session),
        client=_FakeDriveClient(listing),
    )

    with (
        patch("app.qdrant.images.delete_image_sync", side_effect=_fail_if_qdrant_delete),
        patch("app.qdrant.image_captions.delete_caption_sync", side_effect=_fail_if_qdrant_delete),
    ):
        await worker.sync_file_list()

    old = await db_session.get(DriveFile, "old-root-file")
    assert old is not None
    assert old.status == DriveFileStatus.PROCESSED
    assert old.archived_at is None
    new = await db_session.get(DriveFile, "new1")
    assert new is not None
    assert new.root_folder_id == "root-b"


@requires_postgres
@pytest.mark.asyncio
async def test_archived_file_restored_when_reappears_in_listing(db_session):
    db_session.add(
        DriveFile(
            id="f1",
            name="a.jpg",
            mime_type="image/jpeg",
            path="/a.jpg",
            status=DriveFileStatus.ARCHIVED,
            archived_at=datetime.now(timezone.utc),
            error_message="archived: removed from live Drive listing",
            root_folder_id="root-a",
            last_synced_at=datetime.now(timezone.utc),
            cache_rel_path="cache/f1.jpg",
        )
    )
    await db_session.commit()

    listing = _listing([_file("f1", "a.jpg")])
    worker = IndexingWorker(
        session_factory=_session_factory(db_session),
        client=_FakeDriveClient(listing),
    )
    await worker.sync_file_list()

    row = await db_session.get(DriveFile, "f1")
    assert row.status == DriveFileStatus.PROCESSED
    assert row.archived_at is None
    assert row.error_message is None


@requires_postgres
@pytest.mark.asyncio
async def test_logout_preserves_drive_files_and_media(db_session):
    """Disconnect deletes only DriveUser tokens — never indexed rows."""
    from httpx import ASGITransport, AsyncClient

    from app.db.session import get_db
    from app.main import app

    db_session.add(
        DriveUser(
            id="sub-1",
            email="user@example.com",
            access_token="tok",
            refresh_token="ref",
        )
    )
    db_session.add(
        DriveFile(
            id="indexed-1",
            name="keep.jpg",
            mime_type="image/jpeg",
            path="/keep.jpg",
            status=DriveFileStatus.PROCESSED,
            root_folder_id="root-a",
        )
    )
    await db_session.flush()
    db_session.add(Media(drive_file_id="indexed-1", type=MediaType.IMAGE))
    await db_session.commit()

    async def _get_db_override():
        yield db_session

    app.dependency_overrides[get_db] = _get_db_override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/logout")
            assert resp.status_code == 200
            assert resp.json()["ok"] is True
    finally:
        app.dependency_overrides.pop(get_db, None)

    user = (await db_session.execute(select(DriveUser).limit(1))).scalar_one_or_none()
    assert user is None
    row = await db_session.get(DriveFile, "indexed-1")
    assert row is not None
    assert row.status == DriveFileStatus.PROCESSED
    media = (
        await db_session.execute(select(Media).where(Media.drive_file_id == "indexed-1"))
    ).scalar_one_or_none()
    assert media is not None


@requires_postgres
@pytest.mark.asyncio
async def test_library_lists_all_roots_including_archived(db_session):
    """Global library visibility: prior roots + soft-archived files remain listed."""
    db_session.add(
        DriveFile(
            id="a1",
            name="a.jpg",
            mime_type="image/jpeg",
            path="/RootA/a.jpg",
            status=DriveFileStatus.PROCESSED,
            root_folder_id="root-a",
        )
    )
    db_session.add(
        DriveFile(
            id="b1",
            name="b.jpg",
            mime_type="image/jpeg",
            path="/RootB/b.jpg",
            status=DriveFileStatus.ARCHIVED,
            archived_at=datetime.now(timezone.utc),
            root_folder_id="root-b",
            error_message="archived: stale",
        )
    )
    await db_session.commit()

    rows = list((await db_session.execute(select(DriveFile))).scalars().all())
    _root, all_files, summary = build_library_tree(
        rows,
        captioned_ids={"a1"},
        embedded_ids={"a1", "b1"},
        caption_texts={"a1": "caption a"},
    )
    ids = {f.id for f in all_files}
    assert ids == {"a1", "b1"}
    assert summary["total_files"] == 2
    assert summary["archived"] == 1
    assert summary["embedded"] == 2


@requires_postgres
@pytest.mark.asyncio
async def test_soft_archive_preserves_carousel_deep_dive_saves(db_session):
    """Carousel / deep-dive artifacts stay after soft-archive (no FK wipe)."""
    from app.db.models import CarouselGenerationSave
    from app.drive.cleanup import remove_drive_file

    db_session.add(
        DriveFile(
            id="vid-1",
            name="talk.mp4",
            mime_type="video/mp4",
            path="/A/talk.mp4",
            status=DriveFileStatus.PROCESSED,
            root_folder_id="root-a",
        )
    )
    await db_session.flush()
    db_session.add(
        CarouselGenerationSave(
            drive_file_id="vid-1",
            kind="topics_hooks",
            theme_key="deep-dive",
            label="Deep dive 2x",
            payload={"hooks": ["h1"], "quality": "2x"},
        )
    )
    await db_session.commit()

    row = await db_session.get(DriveFile, "vid-1")
    await remove_drive_file(db_session, row, reason="folder switch")
    await db_session.commit()

    archived = await db_session.get(DriveFile, "vid-1")
    assert archived.status == DriveFileStatus.ARCHIVED
    save = (
        await db_session.execute(
            select(CarouselGenerationSave).where(CarouselGenerationSave.drive_file_id == "vid-1")
        )
    ).scalar_one_or_none()
    assert save is not None
    assert save.payload.get("quality") == "2x"


@requires_postgres
@pytest.mark.asyncio
async def test_remove_drive_file_is_soft_archive_only(db_session):
    db_session.add(
        DriveFile(
            id="x1",
            name="x.jpg",
            mime_type="image/jpeg",
            path="/x.jpg",
            status=DriveFileStatus.PROCESSED,
            gemini_document_name="docs/x1",
        )
    )
    await db_session.flush()
    db_session.add(Media(drive_file_id="x1", type=MediaType.IMAGE))
    await db_session.commit()

    row = await db_session.get(DriveFile, "x1")
    with (
        patch("app.qdrant.images.delete_image_sync", side_effect=_fail_if_qdrant_delete),
        patch("app.qdrant.image_captions.delete_caption_sync", side_effect=_fail_if_qdrant_delete),
    ):
        await remove_drive_file(db_session, row, reason="unit test")
    await db_session.commit()

    row = await db_session.get(DriveFile, "x1")
    assert row.status == DriveFileStatus.ARCHIVED
    assert row.gemini_document_name == "docs/x1"
    media = (
        await db_session.execute(select(Media).where(Media.drive_file_id == "x1"))
    ).scalar_one_or_none()
    assert media is not None


@requires_postgres
@pytest.mark.asyncio
async def test_library_api_concurrent_readers(db_session):
    """Load: concurrent Library GET readers succeed without errors."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db.session import get_db
    from app.main import app
    from tests.conftest import TEST_DATABASE_URL

    for i in range(40):
        db_session.add(
            DriveFile(
                id=f"lib-{i}",
                name=f"img-{i}.jpg",
                mime_type="image/jpeg",
                path=f"/Lib/img-{i}.jpg",
                status=DriveFileStatus.PROCESSED if i % 3 else DriveFileStatus.ARCHIVED,
                root_folder_id="root-lib",
                archived_at=datetime.now(timezone.utc) if i % 3 == 0 else None,
            )
        )
    await db_session.commit()

    # Separate sessions per request — AsyncSession is not concurrency-safe.
    engine = create_async_engine(TEST_DATABASE_URL, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _get_db_override():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db_override

    async def _one(ac: AsyncClient) -> int:
        resp = await ac.get("/drive/library")
        return resp.status_code

    try:
        transport = ASGITransport(app=app)
        with (
            patch("app.qdrant.image_captions.valid_caption_ids_sync", return_value=set()),
            patch("app.qdrant.images.existing_image_ids_sync", return_value=set()),
            patch("app.qdrant.image_captions.get_captions_by_ids_sync", return_value={}),
        ):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                codes = await asyncio.gather(*[_one(ac) for _ in range(24)])
    finally:
        app.dependency_overrides.pop(get_db, None)
        await engine.dispose()

    assert codes == [200] * 24


@requires_postgres
@pytest.mark.asyncio
async def test_e2e_connect_index_switch_disconnect_reconnect_preserves(db_session):
    """E2E (DB-level): index → folder switch → disconnect → reconnect → data intact."""
    from httpx import ASGITransport, AsyncClient

    from app.db.session import get_db
    from app.main import app
    from app.drive.indexed_folders import record_indexed_folder

    # Simulate indexed sample under root-a
    db_session.add(
        DriveFile(
            id="sample-1",
            name="sample.jpg",
            mime_type="image/jpeg",
            path="/A/sample.jpg",
            status=DriveFileStatus.PROCESSED,
            root_folder_id="root-a",
            last_synced_at=datetime.now(timezone.utc),
            cache_rel_path="cache/sample-1.jpg",
            gemini_document_name="docs/sample-1",
        )
    )
    await db_session.flush()
    db_session.add(Media(drive_file_id="sample-1", type=MediaType.IMAGE))
    await record_indexed_folder(
        db_session,
        folder_id="root-a",
        folder_name="FolderA",
        mark_active=True,
        file_count=1,
    )
    db_session.add(
        DriveUser(
            id="sub-1",
            email="user@example.com",
            access_token="tok",
            refresh_token="ref",
        )
    )
    await db_session.commit()

    # Folder switch to root-b (only new listing)
    listing_b = _listing(
        [_file("new-b", "b.jpg", parent="root-b", path="/B/b.jpg")],
        root_id="root-b",
        root_name="FolderB",
    )
    worker = IndexingWorker(
        session_factory=_session_factory(db_session),
        client=_FakeDriveClient(listing_b),
    )
    with (
        patch("app.qdrant.images.delete_image_sync", side_effect=_fail_if_qdrant_delete),
        patch("app.qdrant.image_captions.delete_caption_sync", side_effect=_fail_if_qdrant_delete),
    ):
        await worker.sync_file_list()

    sample = await db_session.get(DriveFile, "sample-1")
    assert sample is not None
    assert sample.status == DriveFileStatus.PROCESSED  # other root untouched

    # Disconnect
    async def _get_db_override():
        yield db_session

    app.dependency_overrides[get_db] = _get_db_override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            assert (await ac.post("/api/logout")).status_code == 200
            # Library still lists prior indexed file
            with (
                patch("app.qdrant.image_captions.valid_caption_ids_sync", return_value={"sample-1"}),
                patch("app.qdrant.images.existing_image_ids_sync", return_value={"sample-1"}),
                patch(
                    "app.qdrant.image_captions.get_captions_by_ids_sync",
                    return_value={"sample-1": "a sample"},
                ),
            ):
                lib = await ac.get("/drive/library")
            assert lib.status_code == 200
            body = lib.json()
            assert body["summary"]["total_files"] >= 2
    finally:
        app.dependency_overrides.pop(get_db, None)

    # Reconnect simulation: tokens restored; sample still present with media
    sample = await db_session.get(DriveFile, "sample-1")
    assert sample.status == DriveFileStatus.PROCESSED
    assert sample.cache_rel_path == "cache/sample-1.jpg"
    assert sample.gemini_document_name == "docs/sample-1"
    media = (
        await db_session.execute(select(Media).where(Media.drive_file_id == "sample-1"))
    ).scalar_one_or_none()
    assert media is not None
