"""In-process image embed queue: batchEmbedContents 5 × N parallel, then status flush.

Face jobs push file_ids here after Media/faces commit. Embeddings use media cache
(no Drive re-download on the happy path). StatusWrite(PROCESSED) is enqueued after
embed so IndexStatusBatcher can flush every 100 rows in one UPDATE.
"""
from __future__ import annotations

import asyncio
import logging

import cv2

from app.config import Settings, get_settings
from app.db.models import DriveFile, DriveFileStatus
from app.db.session import get_session_factory
from app.drive.media_cache import ensure_media_cached, read_cached_bytes, resolve_cache_path
from app.pipelines.async_cpu import run_cpu_bound
from app.pipelines.common import decode_image_bgr
from app.search.images import index_image_embeddings_batch
from app.workers.index_batch import IndexStatusBatcher, StatusWrite

logger = logging.getLogger(__name__)


class ImageEmbedQueue:
    """Collect file ids → batch embed → status enqueue."""

    def __init__(
        self,
        *,
        status_batcher: IndexStatusBatcher,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._status_batcher = status_batcher
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._pending: set[str] = set()
        self._lock = asyncio.Lock()
        self._workers: list[asyncio.Task] = []
        self._started = False

    @property
    def pending_ids(self) -> set[str]:
        return set(self._pending)

    def ensure_started(self) -> None:
        if self._started:
            return
        self._started = True
        # Match backfill parallel (default 20 → ~50 img/s with batch_size=5).
        n = max(1, self._settings.image_embed_backfill_parallel)
        for i in range(n):
            self._workers.append(
                asyncio.create_task(self._worker_loop(), name=f"image-embed-q-{i}")
            )
        logger.info(
            "ImageEmbedQueue started workers=%d batch_size=%d",
            n,
            max(1, self._settings.image_embed_batch_size),
        )

    async def push(self, file_id: str) -> None:
        if not file_id:
            return
        self.ensure_started()
        async with self._lock:
            if file_id in self._pending:
                return
            self._pending.add(file_id)
        await self._queue.put(file_id)

    async def stop(self) -> None:
        for _ in self._workers:
            await self._queue.put(None)
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._started = False

    async def _worker_loop(self) -> None:
        batch_size = max(1, self._settings.image_embed_batch_size)
        while True:
            first = await self._queue.get()
            if first is None:
                return
            batch = [first]
            while len(batch) < batch_size:
                try:
                    item = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is None:
                    await self._embed_and_finalize(batch)
                    return
                batch.append(item)
            await self._embed_and_finalize(batch)

    async def _embed_and_finalize(self, file_ids: list[str]) -> None:
        # Lazy import avoids circular: dependencies → indexer → embed_queue.
        from app.dependencies import get_drive_client

        settings = self._settings
        client = get_drive_client()
        session_factory = get_session_factory()
        prepared: list[tuple[str, bytes]] = []
        finalize_ids: list[str] = []

        for fid in file_ids:
            try:
                async with session_factory() as session:
                    row = await session.get(DriveFile, fid)
                    if row is None:
                        continue
                    file_name = row.name or ""
                    cache_path = resolve_cache_path(settings, row)
                    if cache_path is None:
                        cache_path = await ensure_media_cached(client, row, settings)
                        await session.commit()
                raw = await run_cpu_bound(read_cached_bytes, cache_path)
                image_bgr = await run_cpu_bound(decode_image_bgr, raw, file_name=file_name)
                ok, buf = cv2.imencode(".jpg", image_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                if not ok:
                    raise RuntimeError("jpeg encode failed")
                prepared.append((fid, buf.tobytes()))
                finalize_ids.append(fid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("EmbedQueue prepare failed file_id=%s: %s", fid, exc)
                # Faces/media exist; mark PROCESSED and let maintenance backfill embed.
                finalize_ids.append(fid)
            finally:
                async with self._lock:
                    self._pending.discard(fid)

        if prepared:
            try:
                n = await index_image_embeddings_batch(prepared)
                logger.info("EmbedQueue batch upserted=%d of %d", n, len(prepared))
            except Exception:  # noqa: BLE001
                logger.exception("EmbedQueue batchEmbedContents failed count=%d", len(prepared))

        for fid in finalize_ids:
            await self._status_batcher.enqueue(
                StatusWrite(
                    file_id=fid,
                    status=DriveFileStatus.PROCESSED,
                    error_message=None,
                    clear_gemini_document=True,
                    bump_synced_at=True,
                    unlink_drive_cache=True,
                )
            )
