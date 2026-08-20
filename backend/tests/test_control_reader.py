from __future__ import annotations

import asyncio

import pytest

from app.db.models import AppSettings, DriveFile, DriveFileStatus
from app.drive.indexing_pause import global_indexing_is_paused
from app.drive.library_reader_runtime import LibraryReaderRuntime
from app.routers.control_reader import pause_all_indexing, resume_all_indexing
from app.workers.indexer import IndexingWorker


@pytest.mark.asyncio
async def test_control_pause_resume_is_constant_time_and_non_destructive(session):
    session.add(AppSettings(id=1, auto_index_enabled=True))
    file_row = DriveFile(
        id="untouched",
        name="untouched.jpg",
        path="/untouched.jpg",
        mime_type="image/jpeg",
        status=DriveFileStatus.PROCESSING,
        error_message="preserve",
    )
    session.add(file_row)
    await session.commit()

    paused = await pause_all_indexing(session)
    assert paused["drive_files_mutated"] == 0
    assert await global_indexing_is_paused(session)
    await session.refresh(file_row)
    assert file_row.status == DriveFileStatus.PROCESSING
    assert file_row.error_message == "preserve"

    resumed = await resume_all_indexing(session)
    assert resumed["drive_files_mutated"] == 0
    assert not await global_indexing_is_paused(session)
    await session.refresh(file_row)
    assert file_row.status == DriveFileStatus.PROCESSING
    assert file_row.error_message == "preserve"


@pytest.mark.asyncio
async def test_library_reader_runtime_restart_keeps_thread_ready():
    runtime = LibraryReaderRuntime()
    try:
        first = await runtime.warm()
        before = runtime.status()
        after = await runtime.restart()
        assert first
        assert before["thread_alive"] is True
        assert after["thread_alive"] is True
        assert after["generation"] == before["generation"] + 1
    finally:
        runtime.shutdown()


@pytest.mark.asyncio
async def test_cancel_all_indexing_tasks_does_not_need_database():
    worker = object.__new__(IndexingWorker)
    worker._image_tasks = {"image": asyncio.create_task(asyncio.sleep(30))}
    worker._video_tasks = {"video": asyncio.create_task(asyncio.sleep(30))}
    worker._image_started_at = {}
    worker._video_started_at = {}
    assert await worker.cancel_all_indexing_tasks() == 2
    assert worker.active_image_count == 0
    assert worker.active_video_count == 0
