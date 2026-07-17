"""Integration tests for IndexingWorker against a real Postgres instance."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import DriveFile, DriveFileStatus, MediaType
from app.drive.schemas import ConnectorFile, ConnectorFolder, ConnectorFolderListing
from app.workers.indexer import IndexingWorker
from tests.conftest import requires_postgres


class _FakeDriveClient:
    def __init__(self, listing: ConnectorFolderListing) -> None:
        self._listing = listing

    async def list_folder_files(self) -> ConnectorFolderListing:
        return self._listing


def _listing(files: list[ConnectorFile]) -> ConnectorFolderListing:
    return ConnectorFolderListing(folder=ConnectorFolder(id="root", name="shared"), files=files, truncated=False)


def _file(id_: str, name: str, mime: str, is_folder: bool = False) -> ConnectorFile:
    return ConnectorFile(
        id=id_,
        name=name,
        mimeType=mime,
        isFolder=is_folder,
        parentId="root",
        path=f"/{name}",
        modifiedTime=datetime.now(timezone.utc).isoformat(),
    )


def _session_factory(db_session) -> async_sessionmaker:
    """Wraps the single test session in a factory so IndexingWorker can open 'new' sessions
    that all share the same underlying transaction/connection for test visibility."""

    class _Factory:
        def __call__(self):
            return _NoCloseSessionCtx(db_session)

    return _Factory()


class _NoCloseSessionCtx:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


@requires_postgres
@pytest.mark.asyncio
async def test_sync_file_list_upserts_files_and_folder_markers(db_session):
    listing = _listing(
        [
            _file("f1", "a.jpg", "image/jpeg"),
            _file("folder1", "Sub", "application/vnd.google-apps.folder", is_folder=True),
            _file("f2", "b.mp4", "video/mp4"),
        ]
    )
    worker = IndexingWorker(session_factory=_session_factory(db_session), client=_FakeDriveClient(listing))

    seen = await worker.sync_file_list()

    assert seen == 2
    rows = (await db_session.execute(select(DriveFile))).scalars().all()
    by_id = {r.id: r for r in rows}
    assert set(by_id) == {"f1", "f2", "folder1"}
    assert by_id["f1"].status == DriveFileStatus.PENDING
    assert by_id["f2"].status == DriveFileStatus.PENDING
    assert by_id["folder1"].mime_type == "application/vnd.google-apps.folder"
    assert by_id["folder1"].status == DriveFileStatus.SKIPPED
    assert by_id["folder1"].error_message == "folder_marker"


@requires_postgres
@pytest.mark.asyncio
async def test_sync_file_list_marks_changed_processed_file_as_pending_again(db_session):
    old_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
    existing = DriveFile(
        id="f1",
        name="a.jpg",
        mime_type="image/jpeg",
        path="/a.jpg",
        modified_time=old_time,
        status=DriveFileStatus.PROCESSED,
    )
    db_session.add(existing)
    await db_session.commit()

    new_time = datetime(2025, 6, 1, tzinfo=timezone.utc)
    listing = _listing([_file("f1", "a.jpg", "image/jpeg")])
    listing.files[0].modified_time = new_time
    worker = IndexingWorker(session_factory=_session_factory(db_session), client=_FakeDriveClient(listing))

    await worker.sync_file_list()

    refreshed = await db_session.get(DriveFile, "f1")
    assert refreshed.status == DriveFileStatus.PENDING


@requires_postgres
@pytest.mark.asyncio
async def test_process_pending_skips_unsupported_mime_types(db_session):
    db_session.add(
        DriveFile(
            id="doc1",
            name="notes.txt",
            mime_type="text/plain",
            path="/notes.txt",
            status=DriveFileStatus.PENDING,
        )
    )
    await db_session.commit()

    worker = IndexingWorker(session_factory=_session_factory(db_session), client=_FakeDriveClient(_listing([])))
    summary = await worker.process_pending()

    assert summary["skipped"] == 1
    refreshed = await db_session.get(DriveFile, "doc1")
    assert refreshed.status == DriveFileStatus.SKIPPED


@requires_postgres
@pytest.mark.asyncio
async def test_process_pending_does_not_block_queue_when_one_file_errors(db_session):
    """A failing video must not prevent other pending files from being handled in the same cycle."""
    db_session.add(
        DriveFile(id="vid1", name="clip.mp4", mime_type="video/mp4", path="/clip.mp4", status=DriveFileStatus.PENDING)
    )
    db_session.add(
        DriveFile(id="doc1", name="notes.txt", mime_type="text/plain", path="/notes.txt", status=DriveFileStatus.PENDING)
    )
    await db_session.commit()

    worker = IndexingWorker(session_factory=_session_factory(db_session), client=_FakeDriveClient(_listing([])))
    summary = await worker.process_pending()

    assert summary["errored"] == 1  # video handler runs but fake client cannot stream content
    assert summary["skipped"] == 1

    vid = await db_session.get(DriveFile, "vid1")
    assert vid.status == DriveFileStatus.ERROR
    doc = await db_session.get(DriveFile, "doc1")
    assert doc.status == DriveFileStatus.SKIPPED
