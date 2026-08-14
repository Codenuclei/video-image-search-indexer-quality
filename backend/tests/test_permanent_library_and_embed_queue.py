"""Permanent library + embed queue unit tests (no Postgres)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import Settings
from app.db.models import DriveFileStatus
from app.workers.embed_queue import ImageEmbedQueue
from app.workers.index_batch import IndexStatusBatcher, StatusWrite


@pytest.mark.asyncio
async def test_status_batcher_flushes_at_100():
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.commit = AsyncMock()

    factory = MagicMock(return_value=session)
    batcher = IndexStatusBatcher(factory, batch_size=100)

    with patch(
        "app.workers.index_batch.bulk_apply_status_writes",
        new_callable=AsyncMock,
        return_value=100,
    ) as bulk:
        for i in range(100):
            await batcher.enqueue(
                StatusWrite(file_id=f"f{i}", status=DriveFileStatus.PROCESSED)
            )
        assert bulk.await_count == 1
        assert batcher.pending_count == 0


@pytest.mark.asyncio
async def test_embed_queue_finalize_enqueues_processed_status():
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.commit = AsyncMock()
    factory = MagicMock(return_value=session)
    batcher = IndexStatusBatcher(factory, batch_size=100)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        image_embed_batch_size=2,
        image_embed_backfill_parallel=1,
        gemini_api_key="x",
    )
    q = ImageEmbedQueue(status_batcher=batcher, settings=settings)

    with (
        patch("app.dependencies.get_drive_client"),
        patch("app.workers.embed_queue.get_session_factory", return_value=factory),
        patch(
            "app.workers.embed_queue.resolve_cache_path",
            return_value=None,
        ),
        patch(
            "app.workers.embed_queue.ensure_media_cached",
            new_callable=AsyncMock,
            side_effect=RuntimeError("no cache"),
        ),
    ):
        drive = MagicMock()
        drive.id = "a"
        drive.name = "a.jpg"
        session.get = AsyncMock(return_value=drive)
        await q._embed_and_finalize(["a", "b"])  # noqa: SLF001

    assert batcher.pending_count == 2
    assert {w.file_id for w in batcher._queue} == {"a", "b"}  # noqa: SLF001
    assert all(w.status == DriveFileStatus.PROCESSED for w in batcher._queue)


def test_defaults_aim_at_fifty_embed_mark():
    assert Settings.model_fields["image_embed_batch_size"].default == 5
    assert Settings.model_fields["image_embed_backfill_parallel"].default == 20
    assert Settings.model_fields["index_status_batch_size"].default == 100
    assert Settings.model_fields["run_indexer"].default is True
