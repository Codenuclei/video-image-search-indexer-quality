from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import Settings
from app.db.models import DriveFileStatus
from app.pipelines import common as common_mod
from app.pipelines import video as video_mod
from app.storage import RetryableDiskSpaceError, ensure_disk_space
from app.video import whisper_engine as whisper_engine_mod
from app.workers.index_batch import bulk_claim_files
from app.workers.indexer import IndexingWorker, cached_drive_folder_listing, needs_drive_folder_listing


def test_upload_videos_skip_drive_folder_listing() -> None:
    upload = SimpleNamespace(id="upload:abc", source="upload", name="clip.mp4")
    youtube = SimpleNamespace(id="yt:dQw4w9wgGcQ", source="youtube", name="yt.mp4")
    drive = SimpleNamespace(id="1DriveFileIdxxxxx", source="drive", name="drive.mp4")
    assert needs_drive_folder_listing(upload) is False
    assert needs_drive_folder_listing(youtube) is False
    assert needs_drive_folder_listing(drive) is True


def test_new_video_creation_uses_module_media_model() -> None:
    """A branch-local Media import made every new-video Media(...) call unbound."""
    source = inspect.getsource(video_mod.process_video_file)
    assert "from app.db.models import Media" not in source
    assert "media = Media(" in source


@pytest.mark.asyncio
async def test_video_job_releases_transaction_before_drive_and_gemini_io() -> None:
    """Postgres idle_in_transaction_session_timeout=120s closes connections
    left open across list_folder_files() / Gemini delete. The video job must
    commit+close that session, then process_video_file on a fresh one.
    """
    events: list[str] = []
    sessions: list[SimpleNamespace] = []

    class FakeSession:
        def __init__(self) -> None:
            self.in_transaction = False
            self.closed = False
            self.drive_file = SimpleNamespace(
                id="drive-video-1",
                name="wa.mp4",
                mime_type="video/mp4",
                size=1_000_000,
                source="drive",
                gemini_document_name="files/old-doc",
                status=DriveFileStatus.PROCESSING,
                error_message=None,
                last_synced_at=None,
            )

        async def get(self, *_args, **_kwargs):
            assert not self.closed
            self.in_transaction = True
            events.append("session.get")
            return self.drive_file

        async def commit(self):
            self.in_transaction = False
            events.append("session.commit")

        async def rollback(self):
            self.in_transaction = False

    @asynccontextmanager
    async def factory():
        session = FakeSession()
        sessions.append(session)
        events.append("session.open")
        try:
            yield session
        finally:
            session.closed = True
            session.in_transaction = False
            events.append("session.close")

    async def list_folder_files():
        assert sessions, "listing must happen after the prelude session opens"
        assert all(not session.in_transaction for session in sessions)
        assert all(session.closed for session in sessions)
        events.append("list_folder_files")
        return SimpleNamespace(files=[], folder=None, truncated=False)

    def delete_document(name: str) -> None:
        assert all(not session.in_transaction for session in sessions)
        assert all(session.closed for session in sessions)
        events.append(f"gemini.delete:{name}")

    async def process_video_file(session, drive_file, *_args, **_kwargs):
        events.append("process_video_file")
        assert session is sessions[-1]
        assert session is not sessions[0]
        assert not session.closed
        assert drive_file.gemini_document_name is None
        return SimpleNamespace(face_job_queued=False, gemini_document_name=None)

    worker = object.__new__(IndexingWorker)
    worker._session_factory = factory
    worker._client = SimpleNamespace(list_folder_files=list_folder_files)
    worker._settings = Settings(face_jobs_enabled=False)
    worker._video_tasks = {"drive-video-1": MagicMock()}
    worker._video_started_at = {"drive-video-1": 1.0}
    worker._schedule_video_refill = MagicMock()
    worker._start_carousel_task = MagicMock()

    gemini = SimpleNamespace(delete_document=delete_document)
    lock = AsyncMock()

    with (
        patch("app.workers.indexer.get_gemini_service", return_value=gemini),
        patch(
            "app.db.advisory_locks.try_acquire_advisory_lock",
            new=AsyncMock(return_value=lock),
        ),
        patch("app.workers.indexer.process_video_file", new=process_video_file),
        patch("app.workers.indexer.cached_drive_folder_listing", return_value=None),
        patch("app.workers.index_tat.stamp_completed_at", new=AsyncMock()),
        patch("app.drive.media_cache.unlink_drive_source_cache"),
    ):
        await worker._run_video_index_job("drive-video-1")

    assert events.index("session.close") < events.index("gemini.delete:files/old-doc")
    assert events.index("session.close") < events.index("list_folder_files")
    assert events.index("list_folder_files") < events.index("process_video_file")
    assert events.count("session.open") >= 2
    worker._start_carousel_task.assert_called_once_with("drive-video-1")
    lock.release.assert_awaited()


def test_cached_drive_folder_listing_skips_cold_cache() -> None:
    cache = SimpleNamespace(is_warm=lambda: False, folder=None, files=[], truncated=False)
    with patch("app.drive.file_list_cache.get_file_list_cache", return_value=cache):
        assert cached_drive_folder_listing() is None


def test_process_video_file_commits_before_slow_work() -> None:
    """Idle-in-txn locks: must not hold INSERT media open across download/ffmpeg/VLM."""
    source = inspect.getsource(video_mod.process_video_file)
    assert "await session.commit()" in source
    # Commit lands after media flush and before cue/whisper work.
    media_pos = source.index("session.add(media)")
    flush_pos = source.index("await session.flush()", media_pos)
    commit_pos = source.index("await session.commit()", flush_pos)
    cues_pos = source.index("cues = await _load_vtt_cues", commit_pos)
    assert flush_pos < commit_pos < cues_pos


@pytest.mark.asyncio
async def test_duplicate_video_job_stops_when_execution_lock_is_held_elsewhere() -> None:
    worker = object.__new__(IndexingWorker)
    worker._video_tasks = {"video-1": MagicMock()}
    worker._video_started_at = {"video-1": 1.0}
    worker._schedule_video_refill = MagicMock()

    with (
        patch("app.workers.indexer.get_gemini_service", return_value=MagicMock()),
        patch(
            "app.db.advisory_locks.try_acquire_advisory_lock",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.workers.indexer.process_video_file",
            new=AsyncMock(side_effect=AssertionError("duplicate must not execute")),
        ) as process,
    ):
        await worker._run_video_index_job("video-1")

    process.assert_not_awaited()
    assert "video-1" not in worker._video_tasks
    worker._schedule_video_refill.assert_called_once()


@pytest.mark.asyncio
async def test_pending_claim_does_not_reclaim_processing_or_error_rows() -> None:
    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: ["video-1"])
    )

    assert await bulk_claim_files(session, ["video-1"]) == 1

    statement = session.execute.await_args.args[0]
    status_values = next(
        value
        for value in statement.compile().params.values()
        if isinstance(value, (list, tuple)) and value and isinstance(value[0], DriveFileStatus)
    )
    assert list(status_values) == [DriveFileStatus.PENDING]


def test_disk_preflight_raises_clear_retryable_error(tmp_path) -> None:
    usage = SimpleNamespace(total=100, used=90, free=10)
    with patch("app.storage.shutil.disk_usage", return_value=usage):
        with pytest.raises(RetryableDiskSpaceError, match="retryable_disk_full"):
            ensure_disk_space(tmp_path, payload_bytes=11, reserve_bytes=0)


@pytest.mark.asyncio
async def test_stream_disk_full_removes_partial_temp_file(tmp_path) -> None:
    class Response:
        async def aiter_bytes(self, chunk_size: int):
            del chunk_size
            yield b"payload"

    class Client:
        @asynccontextmanager
        async def stream_file_content(self, file_id: str):
            del file_id
            yield Response()

    settings = Settings(temp_dir=str(tmp_path))
    disk_error = RetryableDiskSpaceError(tmp_path, required_bytes=100, free_bytes=0)
    with patch.object(common_mod, "ensure_disk_space", side_effect=[None, disk_error]):
        with pytest.raises(RetryableDiskSpaceError):
            async with common_mod.download_to_temp_file(
                Client(), "drive-file", settings, suffix=".mp4"
            ):
                pass

    assert list(tmp_path.iterdir()) == []


def _stalled_drive_file(file_id: str = "upload:live") -> SimpleNamespace:
    return SimpleNamespace(
        id=file_id,
        mime_type="video/mp4",
        name="clip.mp4",
        last_synced_at=datetime.now(timezone.utc) - timedelta(seconds=120),
        status=DriveFileStatus.PROCESSING,
        error_message=None,
    )


def _stall_worker(drive_file: SimpleNamespace) -> IndexingWorker:
    worker = object.__new__(IndexingWorker)
    worker._settings = Settings(video_index_stall_seconds=60)
    worker._video_started_at = {}
    worker._video_tasks = {}
    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: [drive_file])
    )

    @asynccontextmanager
    async def factory():
        yield session

    worker._session_factory = factory
    worker._stall_session = session
    return worker


@pytest.mark.asyncio
async def test_release_stalled_does_not_cancel_live_video_task() -> None:
    drive_file = _stalled_drive_file()
    worker = _stall_worker(drive_file)
    live_task: asyncio.Future = asyncio.get_running_loop().create_future()
    worker._video_tasks = {drive_file.id: live_task}
    worker._video_started_at = {drive_file.id: asyncio.get_event_loop().time() - 120}

    try:
        with patch(
            "app.db.advisory_locks.try_acquire_advisory_lock",
            new=AsyncMock(side_effect=AssertionError("live job must not be probed")),
        ):
            released = await worker.release_stalled_processing()
        assert released == 0
        assert not live_task.done()
        assert drive_file.status == DriveFileStatus.PROCESSING
        assert drive_file.error_message is None
        worker._stall_session.commit.assert_not_awaited()
    finally:
        live_task.cancel()


@pytest.mark.asyncio
async def test_release_stalled_marks_orphan_index_stall() -> None:
    drive_file = _stalled_drive_file("upload:orphan")
    worker = _stall_worker(drive_file)
    lock = AsyncMock()

    with patch(
        "app.db.advisory_locks.try_acquire_advisory_lock",
        new=AsyncMock(return_value=lock),
    ):
        released = await worker.release_stalled_processing()

    assert released == 1
    assert drive_file.status == DriveFileStatus.ERROR
    assert "index_stall" in (drive_file.error_message or "")
    lock.release.assert_awaited()
    worker._stall_session.commit.assert_awaited()


def test_transcribe_audio_async_reuses_cached_whisper_engine() -> None:
    source = inspect.getsource(whisper_engine_mod.transcribe_audio_async)
    assert "get_whisper_engine()" in source
    assert "WhisperEngine(settings)" not in source
