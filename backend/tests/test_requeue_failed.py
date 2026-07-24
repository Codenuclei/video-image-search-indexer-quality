"""Tests for requeue_failed worker."""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import DriveFile, DriveFileStatus, IndexingFolderPause
from app.workers.requeue_failed import (
    _is_permanent_skip,
    normalize_skip_reason,
    requeue_failed_files,
    requeue_skipped_by_reason,
)
from tests.conftest import requires_postgres


def _session_factory(db_session: AsyncSession) -> async_sessionmaker[Any]:
    """Wrap the shared test session so workers that open/close sessions still work."""

    class _NoClose:
        def __init__(self, session: AsyncSession) -> None:
            self._session = session

        async def __aenter__(self) -> AsyncSession:
            return self._session

        async def __aexit__(self, *args: object) -> None:
            return None

    class _Factory:
        def __call__(self) -> _NoClose:
            return _NoClose(db_session)

    return _Factory()  # type: ignore[return-value]


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


@pytest.mark.parametrize(
    "msg,key",
    [
        ("indexing_paused: indexing stopped for folder /foo", "indexing_paused"),
        ("Unsupported mime type for indexing: application/pdf", "unsupported_mime"),
        ("corrupt_file: decode failed", "corrupt_file"),
        ("decode_exhausted: gave up after 3 attempts", "decode_exhausted"),
        ("folder_marker", "folder_marker"),
        ("weird_custom: detail", "weird_custom"),
        (None, "unknown"),
    ],
)
def test_normalize_skip_reason(msg: str | None, key: str) -> None:
    assert normalize_skip_reason(msg) == key


@requires_postgres
@pytest.mark.asyncio
async def test_requeue_errored_files(db_session: AsyncSession) -> None:
    db_session.add(
        DriveFile(
            id="err-1",
            name="photo.jpg",
            mime_type="image/jpeg",
            path="/photos/photo.jpg",
            status=DriveFileStatus.ERROR,
            error_message="Network timeout",
        )
    )
    db_session.add(
        DriveFile(
            id="skip-1",
            name="video.mp4",
            mime_type="video/mp4",
            path="/videos/video.mp4",
            status=DriveFileStatus.SKIPPED,
            error_message="corrupt_file: decode failed",
        )
    )
    await db_session.commit()

    result = await requeue_failed_files(
        _session_factory(db_session),
        reindex_errored=True,
        reindex_skipped=False,
    )
    assert result["errored_requeued"] == 1
    assert result["skipped_requeued"] == 0

    errored = await db_session.get(DriveFile, "err-1")
    skipped = await db_session.get(DriveFile, "skip-1")
    assert errored is not None
    assert errored.status == DriveFileStatus.PENDING
    assert errored.error_message is None
    assert skipped is not None
    assert skipped.status == DriveFileStatus.SKIPPED


@requires_postgres
@pytest.mark.asyncio
async def test_requeue_skipped_files(db_session: AsyncSession) -> None:
    db_session.add(
        DriveFile(
            id="skip-2",
            name="clip.mp4",
            mime_type="video/mp4",
            path="/clips/clip.mp4",
            status=DriveFileStatus.SKIPPED,
            error_message="corrupt_file: decode failed",
        )
    )
    db_session.add(
        DriveFile(
            id="skip-paused",
            name="paused.jpg",
            mime_type="image/jpeg",
            path="/paused/paused.jpg",
            status=DriveFileStatus.SKIPPED,
            error_message="indexing_paused: indexing stopped for folder /paused",
        )
    )
    await db_session.commit()

    result = await requeue_failed_files(
        _session_factory(db_session),
        reindex_errored=False,
        reindex_skipped=True,
    )
    assert result["errored_requeued"] == 0
    assert result["skipped_requeued"] == 1

    requeued = await db_session.get(DriveFile, "skip-2")
    paused = await db_session.get(DriveFile, "skip-paused")
    assert requeued is not None
    assert requeued.status == DriveFileStatus.PENDING
    assert paused is not None
    assert paused.status == DriveFileStatus.SKIPPED


@requires_postgres
@pytest.mark.asyncio
async def test_retry_by_reason_corrupt(db_session: AsyncSession) -> None:
    db_session.add(
        DriveFile(
            id="c1",
            name="a.jpg",
            mime_type="image/jpeg",
            path="/a.jpg",
            status=DriveFileStatus.SKIPPED,
            error_message="corrupt_file: bad bytes",
        )
    )
    db_session.add(
        DriveFile(
            id="d1",
            name="b.jpg",
            mime_type="image/jpeg",
            path="/b.jpg",
            status=DriveFileStatus.SKIPPED,
            error_message="decode_exhausted: gave up",
        )
    )
    await db_session.commit()

    result = await requeue_skipped_by_reason(db_session, "corrupt_file")
    await db_session.commit()

    assert result["action"] == "requeue"
    assert result["requeued"] == 1

    c1 = await db_session.get(DriveFile, "c1")
    d1 = await db_session.get(DriveFile, "d1")
    assert c1 is not None and c1.status == DriveFileStatus.PENDING
    assert d1 is not None and d1.status == DriveFileStatus.SKIPPED


@requires_postgres
@pytest.mark.asyncio
async def test_retry_by_reason_unsupported_noop(db_session: AsyncSession) -> None:
    db_session.add(
        DriveFile(
            id="u1",
            name="archive.zip",
            mime_type="application/zip",
            path="/archive.zip",
            status=DriveFileStatus.SKIPPED,
            error_message="Unsupported mime type for indexing: application/zip",
        )
    )
    await db_session.commit()

    result = await requeue_skipped_by_reason(db_session, "unsupported_mime")
    await db_session.commit()

    assert result["action"] == "unsupported"
    assert result["requeued"] == 0
    assert result["ineligible"] == 1

    u1 = await db_session.get(DriveFile, "u1")
    assert u1 is not None and u1.status == DriveFileStatus.SKIPPED


@requires_postgres
@pytest.mark.asyncio
async def test_retry_by_reason_resume_paused(db_session: AsyncSession) -> None:
    db_session.add(IndexingFolderPause(folder_path="/paused"))
    db_session.add(
        DriveFile(
            id="p1",
            name="paused.jpg",
            mime_type="image/jpeg",
            path="/paused/paused.jpg",
            status=DriveFileStatus.SKIPPED,
            error_message="indexing_paused: indexing stopped for folder /paused",
        )
    )
    db_session.add(
        DriveFile(
            id="p2",
            name="other.jpg",
            mime_type="image/jpeg",
            path="/other/other.jpg",
            status=DriveFileStatus.SKIPPED,
            error_message="corrupt_file: x",
        )
    )
    await db_session.commit()

    result = await requeue_skipped_by_reason(db_session, "indexing_paused")
    await db_session.commit()

    assert result["action"] == "resume_paused"
    assert result["requeued"] == 1
    assert result["folders_resumed"] == 1

    p1 = await db_session.get(DriveFile, "p1")
    p2 = await db_session.get(DriveFile, "p2")
    pause_row = (
        await db_session.execute(
            select(IndexingFolderPause).where(IndexingFolderPause.folder_path == "/paused")
        )
    ).scalar_one_or_none()
    assert p1 is not None and p1.status == DriveFileStatus.PENDING
    assert p2 is not None and p2.status == DriveFileStatus.SKIPPED
    assert pause_row is None
