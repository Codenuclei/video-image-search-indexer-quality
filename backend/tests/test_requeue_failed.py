"""Tests for requeue_failed worker."""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import DriveFile, DriveFileStatus
from app.workers.requeue_failed import _is_permanent_skip, requeue_failed_files


@pytest.mark.parametrize(
    "error_message,expected",
    [
        ("indexing_paused: indexing stopped for folder /foo", True),
        ("Unsupported mime type for indexing: application/octet-stream", True),
        ("decode_exhausted: gave up after 3 attempts", False),
        ("Failed to download from Drive", False),
        (None, False),
    ],
)
def test_is_permanent_skip(error_message: str | None, expected: bool) -> None:
    drive_file = DriveFile(
        id="1",
        name="test.jpg",
        mime_type="image/jpeg",
        path="/test.jpg",
        status=DriveFileStatus.SKIPPED,
        error_message=error_message,
    )
    assert _is_permanent_skip(drive_file) is expected


@pytest.mark.asyncio
async def test_requeue_errored_files(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        session.add(
            DriveFile(
                id="err-1",
                name="photo.jpg",
                mime_type="image/jpeg",
                path="/photos/photo.jpg",
                status=DriveFileStatus.ERROR,
                error_message="Network timeout",
            )
        )
        session.add(
            DriveFile(
                id="skip-1",
                name="video.mp4",
                mime_type="video/mp4",
                path="/videos/video.mp4",
                status=DriveFileStatus.SKIPPED,
                error_message="corrupt_file: decode failed",
            )
        )
        await session.commit()

    result = await requeue_failed_files(
        session_factory,
        reindex_errored=True,
        reindex_skipped=False,
    )
    assert result["errored_requeued"] == 1
    assert result["skipped_requeued"] == 0

    async with session_factory() as session:
        errored = await session.get(DriveFile, "err-1")
        skipped = await session.get(DriveFile, "skip-1")
        assert errored is not None
        assert errored.status == DriveFileStatus.PENDING
        assert errored.error_message is None
        assert skipped is not None
        assert skipped.status == DriveFileStatus.SKIPPED


@pytest.mark.asyncio
async def test_requeue_skipped_files(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        session.add(
            DriveFile(
                id="skip-2",
                name="clip.mp4",
                mime_type="video/mp4",
                path="/clips/clip.mp4",
                status=DriveFileStatus.SKIPPED,
                error_message="corrupt_file: decode failed",
            )
        )
        session.add(
            DriveFile(
                id="skip-paused",
                name="paused.jpg",
                mime_type="image/jpeg",
                path="/paused/paused.jpg",
                status=DriveFileStatus.SKIPPED,
                error_message="indexing_paused: indexing stopped for folder /paused",
            )
        )
        await session.commit()

    result = await requeue_failed_files(
        session_factory,
        reindex_errored=False,
        reindex_skipped=True,
    )
    assert result["errored_requeued"] == 0
    assert result["skipped_requeued"] == 1

    async with session_factory() as session:
        requeued = await session.get(DriveFile, "skip-2")
        paused = await session.get(DriveFile, "skip-paused")
        assert requeued is not None
        assert requeued.status == DriveFileStatus.PENDING
        assert paused is not None
        assert paused.status == DriveFileStatus.SKIPPED
