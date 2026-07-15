"""Tests for folder pause/resume and corrupt-file skipping."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import DriveFile, DriveFileStatus, IndexingFolderPause
from app.drive.indexing_pause import (
    CORRUPT_SKIPPED_PREFIX,
    INDEXING_PAUSED_PREFIX,
    is_file_indexing_paused,
    pause_folder_indexing,
    resume_folder_indexing,
    skip_corrupt_files,
)


@pytest.mark.asyncio
async def test_pause_and_resume_folder(session):
    session.add(
        DriveFile(
            id="f1",
            name="photo.jpg",
            path="/UG iPhone Data/photo.jpg",
            mime_type="image/jpeg",
            status=DriveFileStatus.PENDING,
        )
    )
    session.add(
        DriveFile(
            id="f2",
            name="other.jpg",
            path="/Other/other.jpg",
            mime_type="image/jpeg",
            status=DriveFileStatus.PENDING,
        )
    )
    await session.flush()

    stopped = await pause_folder_indexing(session, "/UG iPhone Data")
    assert stopped == 1

    f1 = await session.get(DriveFile, "f1")
    f2 = await session.get(DriveFile, "f2")
    assert f1.status == DriveFileStatus.SKIPPED
    assert f1.error_message.startswith(INDEXING_PAUSED_PREFIX)
    assert f2.status == DriveFileStatus.PENDING
    assert is_file_indexing_paused(f1.path, ["/UG iPhone Data"])
    assert not is_file_indexing_paused(f2.path, ["/UG iPhone Data"])

    resumed = await resume_folder_indexing(session, "/UG iPhone Data")
    assert resumed == 1
    await session.refresh(f1)
    assert f1.status == DriveFileStatus.PENDING
    assert f1.error_message is None


@pytest.mark.asyncio
async def test_skip_corrupt_only_decode_failures(session):
    session.add(
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
    session.add(
        DriveFile(
            id="good",
            name="fine.cr3",
            path="/Photos/fine.cr3",
            mime_type="image/x-canon-cr3",
            status=DriveFileStatus.PENDING,
            decode_attempts=0,
        )
    )
    await session.flush()

    skipped = await skip_corrupt_files(session)
    assert skipped == 1

    bad = await session.get(DriveFile, "bad")
    good = await session.get(DriveFile, "good")
    assert bad.status == DriveFileStatus.SKIPPED
    assert bad.error_message.startswith(CORRUPT_SKIPPED_PREFIX)
    assert good.status == DriveFileStatus.PENDING

    pause_rows = (await session.execute(select(IndexingFolderPause))).scalars().all()
    assert pause_rows == []
