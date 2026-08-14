from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import Settings
from app.db.models import DriveFileStatus
from app.pipelines import common as common_mod
from app.pipelines import video as video_mod
from app.storage import RetryableDiskSpaceError, ensure_disk_space
from app.workers.index_batch import bulk_claim_files
from app.workers.indexer import IndexingWorker


def test_new_video_creation_uses_module_media_model() -> None:
    """A branch-local Media import made every new-video Media(...) call unbound."""
    source = inspect.getsource(video_mod.process_video_file)
    assert "from app.db.models import Media" not in source
    assert "media = Media(" in source


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
