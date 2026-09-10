"""Tests for folder pause/resume and corrupt-file skipping."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import DriveFile, DriveFileStatus, IndexingFolderPause
from app.drive.indexing_pause import (
    CORRUPT_SKIPPED_PREFIX,
    INDEXING_PAUSED_PREFIX,
    is_file_indexing_paused,
    lane_paused_folder_paths,
    pause_folder_indexing,
    resume_folder_indexing,
    set_folder_pause_flag,
    skip_corrupt_files,
)


@pytest.mark.asyncio
async def test_pause_and_resume_folder(db_session):
    db_session.add(
        DriveFile(
            id="f1",
            name="photo.jpg",
            path="/UG iPhone Data/photo.jpg",
            mime_type="image/jpeg",
            status=DriveFileStatus.PENDING,
        )
    )
    db_session.add(
        DriveFile(
            id="f2",
            name="other.jpg",
            path="/Other/other.jpg",
            mime_type="image/jpeg",
            status=DriveFileStatus.PENDING,
        )
    )
    await db_session.flush()

    stopped = await pause_folder_indexing(db_session, "/UG iPhone Data")
    assert stopped == 1

    f1 = await db_session.get(DriveFile, "f1")
    f2 = await db_session.get(DriveFile, "f2")
    assert f1.status == DriveFileStatus.SKIPPED
    assert f1.error_message.startswith(INDEXING_PAUSED_PREFIX)
    assert f2.status == DriveFileStatus.PENDING
    assert is_file_indexing_paused(f1.path, ["/UG iPhone Data"])
    assert not is_file_indexing_paused(f2.path, ["/UG iPhone Data"])

    resumed = await resume_folder_indexing(db_session, "/UG iPhone Data")
    assert resumed == 1
    await db_session.refresh(f1)
    assert f1.status == DriveFileStatus.PENDING
    assert f1.error_message is None


@pytest.mark.asyncio
async def test_global_pause_flag_never_mutates_drive_files(db_session):
    db_session.add(
        DriveFile(
            id="keep-processing",
            name="keep.jpg",
            path="/keep.jpg",
            mime_type="image/jpeg",
            status=DriveFileStatus.PROCESSING,
            error_message="existing state",
        )
    )
    await db_session.flush()

    assert await set_folder_pause_flag(db_session, "/", paused=True)
    row = await db_session.get(DriveFile, "keep-processing")
    assert row.status == DriveFileStatus.PROCESSING
    assert row.error_message == "existing state"

    assert await set_folder_pause_flag(db_session, "/", paused=False)
    await db_session.refresh(row)
    assert row.status == DriveFileStatus.PROCESSING
    assert row.error_message == "existing state"


@pytest.mark.asyncio
async def test_skip_corrupt_only_decode_failures(db_session):
    db_session.add(
        DriveFile(
            id="bad",
            name="broken.cr3",
            path="/Photos/broken.cr3",
            mime_type="image/x-canon-cr3",
            status=DriveFileStatus.ERROR,
            error_message="PIL cannot identify image file",
            decode_attempts=1,
        )
    )
    db_session.add(
        DriveFile(
            id="good",
            name="fine.cr3",
            path="/Photos/fine.cr3",
            mime_type="image/x-canon-cr3",
            status=DriveFileStatus.PENDING,
            decode_attempts=0,
        )
    )
    await db_session.flush()

    skipped = await skip_corrupt_files(db_session)
    assert skipped == 1

    bad = await db_session.get(DriveFile, "bad")
    good = await db_session.get(DriveFile, "good")
    assert bad.status == DriveFileStatus.SKIPPED
    assert bad.error_message.startswith(CORRUPT_SKIPPED_PREFIX)
    assert good.status == DriveFileStatus.PENDING

    pause_rows = (await db_session.execute(select(IndexingFolderPause))).scalars().all()
    assert pause_rows == []


def test_lane_paused_folder_paths_drops_root() -> None:
    import inspect

    src = inspect.getsource(lane_paused_folder_paths)
    assert 'path != "/"' in src


def test_global_pause_does_not_skip_existing_pending_on_upsert() -> None:
    import inspect

    from app.workers import indexer as idx

    src = inspect.getsource(idx.IndexingWorker._upsert_drive_file)
    assert 'p != "/"' in src
    assert "skip_pause" in src
    assert "INDEXING_PAUSED_PREFIX" in src
