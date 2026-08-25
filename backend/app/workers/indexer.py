from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import and_, case, exists, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db.models import CarouselGenerationSave, DriveFile, DriveFileStatus, Media, VideoSegment
from app.drive.client import DriveConnectorClient, DriveConnectorError
from app.drive.google_client import DriveDirectError
from app.drive.cleanup import (
    remove_drive_file,
    restore_archived_when_index_complete,
    restore_processed_when_media_exists,
)
from app.drive.content_hash import APPLEDOUBLE_SKIP_PREFIX, is_macos_junk_name
from app.drive.schemas import ConnectorFile
from app.drive.video_limits import apply_video_too_large_skip, is_video_too_large, video_too_large_message
from app.gemini.service import GeminiFileSearchService, get_gemini_service
from app.pipelines.common import (
    INDEXABLE_IMAGE_TYPES,
    INDEXABLE_VIDEO_TYPES,
    file_has_media,
    infer_image_mime,
    is_drive_media_candidate,
    is_image_mime,
    is_indexable_mime,
    is_video_mime,
)
from app.pipelines.image import process_image_file
from app.pipelines.video import process_video_file
from app.video.youtube_registry import is_youtube_source
from app.pipelines.decode_recovery import apply_decode_failure, decode_max_attempts, is_decode_failure_error
from app.drive.traverse import FOLDER_MIME, SHORTCUT_MIME
from app.drive.indexing_pause import (
    is_file_indexing_paused,
    load_paused_folder_paths,
)
from app.drive.indexing_pause import file_under_folder, normalize_folder_path
from app.db.deadlock import is_deadlock_error, is_transient_db_error, retry_on_deadlock
from app.runtime_settings import get_runtime_settings
from app.workers.index_batch import (
    IndexStatusBatcher,
    StatusWrite,
    bulk_claim_file_ids,
    bulk_claim_files,
)
from app.workers.embed_queue import ImageEmbedQueue
from app.workers.index_errors import friendly_index_error_message, is_transient_network_error
from app.workers.requeue_failed import requeue_failed_files
from app.workers.claim_order import claim_window, pending_order_by

logger = logging.getLogger(__name__)


def _database_safe_payload(value):
    """Strip characters unsupported by legacy SQL_ASCII local databases."""
    if isinstance(value, str):
        return value.encode("ascii", "ignore").decode("ascii")
    if isinstance(value, list):
        return [_database_safe_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: _database_safe_payload(item) for key, item in value.items()}
    return value


def _log_skip(drive_file: DriveFile, reason: str) -> None:
    logger.info(
        "index_skip reason=%s file_id=%s mime=%s size=%s name=%s",
        reason,
        drive_file.id,
        drive_file.mime_type,
        drive_file.size,
        drive_file.name,
    )


def _cached_video_recovery_eligible(cue_count: int, frames_dir: Path) -> bool:
    """Return whether surviving transcript/frame assets can support recovery."""
    return cue_count >= 2 and any(frames_dir.glob("*.jpg"))


def needs_drive_folder_listing(drive_file: DriveFile) -> bool:
    """Drive listing is only for Drive-sourced videos (sidecar VTT lookup).

    Local uploads already have bytes on the volume; requiring a selected Drive
    folder blocked Whisper captions for /search/carousel/upload.
    """
    source = (getattr(drive_file, "source", None) or "drive").strip().lower()
    fid = str(getattr(drive_file, "id", "") or "")
    if source in {"upload", "youtube"} or fid.startswith(("upload:", "yt:")):
        return False
    return not is_youtube_source(drive_file)


# Background carousel generation drains the captioned backlog a couple of
# videos at a time; these bound how hard a repeatedly failing video is retried.
CAROUSEL_MAX_ATTEMPTS = 3
CAROUSEL_DRAIN_DELAY_SEC = 5.0
CAROUSEL_LOCK_STALE_SEC = 900.0
# A job that never returns would hold its concurrency slot forever and stall the
# whole backlog, so generation is bounded and a hang lands on `error` instead.
CAROUSEL_JOB_TIMEOUT_SEC = 1800.0


def _captioned_predicate():
    """Correlated EXISTS for "captioned": at least one non-empty transcript cue.

    Same definition the carousel video pickers use, so what the UI lists and
    what the background worker builds can never drift apart.
    """
    return exists(
        select(1)
        .select_from(Media)
        .join(
            VideoSegment,
            and_(VideoSegment.media_id == Media.id, VideoSegment.text != ""),
        )
        .where(Media.drive_file_id == DriveFile.id)
    )


async def _video_is_captioned(session: AsyncSession, file_id: str) -> bool:
    cue_count = await session.scalar(
        select(func.count(VideoSegment.id))
        .select_from(Media)
        .join(
            VideoSegment,
            and_(VideoSegment.media_id == Media.id, VideoSegment.text != ""),
        )
        .where(Media.drive_file_id == file_id)
    )
    return int(cue_count or 0) >= 2


def _record_index_failure(drive_file: DriveFile, exc: Exception) -> None:
    raw = str(exc)[:2000]
    msg = friendly_index_error_message(exc, max_len=500)
    if is_decode_failure_error(raw):
        apply_decode_failure(drive_file, raw)
    elif is_transient_db_error(exc) or is_transient_network_error(exc):
        # Don't leave files stuck in ERROR forever for lock/abort / flaky download
        # fallout — auto-index will pick them up again.
        drive_file.status = DriveFileStatus.PENDING
        drive_file.error_message = None
        logger.warning(
            "index_requeue_transient file_id=%s mime=%s err=%s",
            drive_file.id,
            drive_file.mime_type,
            raw[:200] or type(exc).__name__,
        )
    else:
        drive_file.status = DriveFileStatus.ERROR
        drive_file.error_message = msg
        logger.warning(
            "index_error file_id=%s mime=%s size=%s err=%s",
            drive_file.id,
            drive_file.mime_type,
            drive_file.size,
            raw[:200] or type(exc).__name__,
        )

class IndexingWorker:
    """Syncs Drive files and indexes media into local Qdrant RAG (+ faces in Postgres)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: "DriveConnectorClient | None" = None,
        settings: Settings | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._client = client or DriveConnectorClient()
        self._settings = settings or get_settings()
        self._running = False
        self.last_run_summary: dict[str, int] | None = None
        self.last_run_at: datetime | None = None
        self._video_tasks: dict[str, asyncio.Task] = {}
        self._image_tasks: dict[str, asyncio.Task] = {}
        self._video_started_at: dict[str, float] = {}
        self._image_started_at: dict[str, float] = {}
        self._image_refill_lock = asyncio.Lock()
        self._video_refill_lock = asyncio.Lock()
        self._carousel_tasks: dict[str, asyncio.Task] = {}
        self._carousel_started_at: dict[str, float] = {}
        self._carousel_semaphore = asyncio.Semaphore(2)
        self._carousel_drain_lock = asyncio.Lock()
        # Cap concurrent DB sessions held during download/face/embed work.
        self._db_sem = asyncio.Semaphore(max(1, self._settings.index_db_max_concurrent))
        # Buffer PROCESSED/ERROR/PENDING finals → one UPDATE every N files.
        self._status_batcher = IndexStatusBatcher(
            session_factory,
            batch_size=max(1, self._settings.index_status_batch_size),
        )
        self._embed_queue = ImageEmbedQueue(
            status_batcher=self._status_batcher,
            settings=self._settings,
        )
        # One Drive list→DB upsert at a time (HTTP sync + startup seed + SSH must not race).
        self._sync_file_list_lock = asyncio.Lock()
    @property
    def active_video_count(self) -> int:
        self._prune_video_tasks()
        return len(self._video_tasks)

    def _prune_video_tasks(self) -> None:
        done = [fid for fid, task in self._video_tasks.items() if task.done()]
        for fid in done:
            task = self._video_tasks.pop(fid)
            if task.cancelled():
                continue
            exc = task.exception()
            if exc:
                logger.error("Background video index task %s failed: %s", fid, exc)

    async def _occupied_video_ids(self, session: AsyncSession) -> set[str]:
        """Only in-flight video tasks count as occupied (orphans are adopted separately)."""
        del session
        return set(self._video_tasks.keys())

    def _prune_image_tasks(self) -> None:
        done = [fid for fid, task in self._image_tasks.items() if task.done()]
        for fid in done:
            task = self._image_tasks.pop(fid)
            if task.cancelled():
                continue
            exc = task.exception()
            if exc:
                logger.error("Background image index task %s failed: %s", fid, exc)

    @property
    def active_image_count(self) -> int:
        self._prune_image_tasks()
        return len(self._image_tasks)

    def _image_max_parallel(self) -> int:
        max_parallel = max(1, self._settings.image_index_max_parallel)
        runtime = get_runtime_settings()
        from app.workers.go_indexer_state import go_is_alive

        # Reserve slots for the Go canary when it is toggled on and heartbeating.
        if runtime.go_indexer_enabled and go_is_alive(
            max_age_seconds=self._settings.go_indexer_heartbeat_seconds
        ):
            reserved = max(0, min(self._settings.go_indexer_max_parallel, max_parallel - 1))
            max_parallel = max(1, max_parallel - reserved)
        return max_parallel

    async def _occupied_image_ids(self, session: AsyncSession) -> set[str]:
        """In-flight image tasks on this worker.

        Claim is serialized with LOCK_IMAGE_INDEX_CLAIM, so local tasks are the
        right occupancy signal. Unflushed status-batch rows stay PROCESSING in
        DB until the 100-row write — they must not block new claims.
        """
        del session
        return set(self._image_tasks.keys())

    def _start_image_task(self, file_id: str) -> None:
        self._image_started_at[file_id] = asyncio.get_event_loop().time()
        self._image_tasks[file_id] = asyncio.create_task(
            self._run_image_index_job(file_id),
            name=f"image-index-{file_id[:8]}",
        )

    def _schedule_image_refill(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._refill_image_slots(), name="image-slot-refill")

    def _schedule_video_refill(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._refill_video_slots(), name="video-slot-refill")

    async def _refill_image_slots(self) -> None:
        async with self._image_refill_lock:
            try:
                await self.ensure_parallel_image_indexing()
            except Exception:  # noqa: BLE001
                logger.exception("Image slot refill failed")

    async def _refill_video_slots(self) -> None:
        async with self._video_refill_lock:
            try:
                await self.ensure_parallel_video_indexing()
            except Exception:  # noqa: BLE001
                logger.exception("Video slot refill failed")

    async def ensure_parallel_image_indexing(self) -> int:
        """Start background image index jobs up to image_index_max_parallel."""
        from app.db.advisory_locks import LOCK_IMAGE_INDEX_CLAIM, advisory_lock
        from app.storage import indexing_disk_ready

        self._prune_image_tasks()
        # Phase 0: do not admit new downloads when the volume is near full.
        if not indexing_disk_ready(
            self._settings.media_cache_dir,
            high_water_bytes=self._settings.index_disk_high_water_bytes,
        ):
            logger.warning(
                "image_index_paused_disk_low path=%s high_water_bytes=%s",
                self._settings.media_cache_dir,
                self._settings.index_disk_high_water_bytes,
            )
            return 0

        max_parallel = self._image_max_parallel()

        async with advisory_lock(
            LOCK_IMAGE_INDEX_CLAIM, name="image_index_claim", blocking=False
        ) as got:
            if not got:
                return 0
            return await self._ensure_parallel_image_indexing_locked(
                max_parallel=max_parallel, started=0
            )

    async def _ensure_parallel_image_indexing_locked(
        self, *, max_parallel: int, started: int
    ) -> int:
        # Adopt orphaned PROCESSING images (restart / lost task) before claiming PENDING.
        # Never steal files the Go canary currently owns (fresh heartbeat + open claims).
        from app.workers.go_indexer_state import go_claimed_ids, go_is_alive

        go_owned: set[str] = set()
        if get_runtime_settings().go_indexer_enabled and go_is_alive(
            max_age_seconds=self._settings.go_indexer_heartbeat_seconds
        ):
            go_owned = go_claimed_ids()

        async with self._session_factory() as session:
            paused_paths = await load_paused_folder_paths(session)
            processing = list(
                (
                    await session.execute(
                        select(DriveFile).where(DriveFile.status == DriveFileStatus.PROCESSING)
                    )
                ).scalars().all()
            )
            adopt_ids: list[str] = []
            requeue_ids: list[str] = []
            now = datetime.now(timezone.utc)
            # Short grace only — long stalls left slots empty after deploys/restarts
            # while orphans sat PROCESSING and weren't in _image_tasks.
            stall_sec = 45.0
            awaiting_flush = self._status_batcher.pending_ids | self._embed_queue.pending_ids
            for drive_file in processing:
                if not is_image_mime(drive_file.mime_type, drive_file.name):
                    continue
                if drive_file.id in self._image_tasks:
                    continue
                if drive_file.id in awaiting_flush:
                    # Finished work; status will land in the next 100-row write.
                    continue
                if drive_file.id in go_owned:
                    continue
                if is_file_indexing_paused(drive_file.path, paused_paths):
                    requeue_ids.append(drive_file.id)
                    continue
                # Only adopt truly stalled orphans — avoid racing a just-finished
                # job whose task map entry was already popped.
                synced = drive_file.last_synced_at
                if synced is not None:
                    age = (now - synced).total_seconds()
                    if age < stall_sec:
                        continue
                if len(self._image_tasks) + len(adopt_ids) >= max_parallel:
                    requeue_ids.append(drive_file.id)
                    continue
                adopt_ids.append(drive_file.id)

            for file_id in requeue_ids:
                row = next((f for f in processing if f.id == file_id), None)
                if row is not None:
                    row.status = DriveFileStatus.PENDING
                    row.error_message = None
            if requeue_ids:
                await session.commit()
                logger.info(
                    "Re-queued %d orphaned PROCESSING image(s) that exceeded image slots",
                    len(requeue_ids),
                )

        for file_id in adopt_ids:
            if file_id in self._image_tasks:
                continue
            self._start_image_task(file_id)
            started += 1
            logger.info("Adopted orphaned PROCESSING image index for %s", file_id)

        claimed_ids: list[str] = []

        async with self._session_factory() as session:
            occupied = await self._occupied_image_ids(session)
            # Also count tasks we just adopted that may not be committed yet.
            occupied |= set(self._image_tasks.keys())
            slots = max_parallel - len(occupied)
            if slots <= 0:
                return started

            paused_paths = await load_paused_folder_paths(session)

            # Filter image MIME in SQL so a large non-image backlog cannot starve
            # the claim window (size-ordered PENDING scan).
            # Widen the window so known content/name conflicts can be skipped in-SQL
            # without exhausting free Active slots on downloads that will only autoskip.
            scan_limit = max(
                claim_window(self._settings, slots),
                min(5000, max(200, slots * 250)),
            )
            pending = list(
                (
                    await session.execute(
                        select(DriveFile)
                        .where(
                            DriveFile.status == DriveFileStatus.PENDING,
                            or_(
                                DriveFile.mime_type.in_(tuple(INDEXABLE_IMAGE_TYPES)),
                                DriveFile.mime_type.like("image/%"),
                            ),
                        )
                        .order_by(*pending_order_by(self._settings))
                        .limit(scan_limit)
                    )
                ).scalars().all()
            )

            dirty = False
            for drive_file in pending:
                if len(claimed_ids) >= slots:
                    break
                if not is_image_mime(drive_file.mime_type, drive_file.name):
                    continue
                if is_macos_junk_name(drive_file.name):
                    await session.delete(drive_file)
                    dirty = True
                    continue
                if (drive_file.decode_attempts or 0) >= decode_max_attempts():
                    continue
                if is_file_indexing_paused(drive_file.path, paused_paths):
                    continue
                if drive_file.id in occupied:
                    continue
                # Skip known content/name conflicts without occupying an Active slot.
                if drive_file.content_hash and drive_file.content_hash_algo:
                    from app.drive.conflicts import (
                        apply_dedupe_on_upsert,
                        find_older_pending_same_hash,
                    )

                    skip_key = await apply_dedupe_on_upsert(
                        session,
                        drive_file,
                        algo=drive_file.content_hash_algo,
                        digest=drive_file.content_hash,
                    )
                    if skip_key:
                        _log_skip(drive_file, skip_key)
                        dirty = True
                        continue
                    # Only the oldest PENDING per content hash should run the pipeline.
                    older = await find_older_pending_same_hash(
                        session,
                        algo=drive_file.content_hash_algo,
                        digest=drive_file.content_hash,
                        exclude_id=drive_file.id,
                        created_at=drive_file.created_at,
                    )
                    if older is not None:
                        continue
                claimed_ids.append(drive_file.id)
                occupied.add(drive_file.id)

            # ONE write: claim every selected id (up to slots) as PROCESSING.
            if claimed_ids:
                claimed_ids = await bulk_claim_file_ids(session, claimed_ids)
                n = len(claimed_ids)
                dirty = True
                logger.info("bulk_claim_images count=%d rowcount=%d", len(claimed_ids), n)
            if dirty:
                await session.commit()

        for file_id in claimed_ids:
            self._start_image_task(file_id)
            logger.info("Started parallel image index for %s", file_id)

        return started + len(claimed_ids)

    async def index_claimed_image(self, file_id: str) -> None:
        """Complete indexing for a file already claimed (e.g. by the Go canary)."""
        await self._run_image_index_job(file_id)

    async def pause_video_indexing_lane(self, *, requeue: bool = True) -> int:
        """Cancel local video tasks and optionally requeue PROCESSING videos to PENDING."""
        self._prune_video_tasks()
        cancelled = 0
        video_ids = list(self._video_tasks.keys())
        for fid in video_ids:
            task = self._video_tasks.get(fid)
            if task is None or task.done():
                continue
            task.cancel()
            cancelled += 1
        if video_ids:
            await asyncio.gather(
                *[t for t in self._video_tasks.values() if not t.done()],
                return_exceptions=True,
            )
        self._prune_video_tasks()

        requeued = 0
        if requeue:
            async with self._session_factory() as session:
                rows = list(
                    (
                        await session.execute(
                            select(DriveFile).where(DriveFile.status == DriveFileStatus.PROCESSING)
                        )
                    ).scalars().all()
                )
                for row in rows:
                    if not is_video_mime(row.mime_type):
                        continue
                    row.status = DriveFileStatus.PENDING
                    row.error_message = None
                    requeued += 1
                if requeued:
                    await session.commit()
        if cancelled or requeued:
            logger.info(
                "video_lane_paused cancelled_tasks=%d requeued_processing=%d",
                cancelled,
                requeued,
            )
        return cancelled + requeued

    async def cancel_indexing_under_folder(self, folder_path: str) -> int:
        """Cancel in-flight index jobs for files under a folder path."""
        norm = normalize_folder_path(folder_path)
        if norm == "/":
            return await self.cancel_all_indexing_tasks()
        cancelled = 0
        async with self._session_factory() as session:
            for fid, task in list(self._image_tasks.items()):
                if task.done():
                    continue
                drive_file = await session.get(DriveFile, fid)
                if drive_file and file_under_folder(drive_file.path, norm):
                    task.cancel()
                    cancelled += 1
            for fid, task in list(self._video_tasks.items()):
                if task.done():
                    continue
                drive_file = await session.get(DriveFile, fid)
                if drive_file and file_under_folder(drive_file.path, norm):
                    task.cancel()
                    cancelled += 1
        if cancelled:
            logger.info("Cancelled %d in-flight index job(s) under %s", cancelled, norm)
        return cancelled

    async def cancel_all_indexing_tasks(self) -> int:
        """Cancel local image/video jobs without querying or changing DriveFile rows."""
        tasks = [
            task
            for task in (*self._image_tasks.values(), *self._video_tasks.values())
            if not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.info("Cancelled %d in-flight indexing job(s)", len(tasks))
        self._prune_image_tasks()
        self._prune_video_tasks()
        return len(tasks)

    async def _run_image_index_job(self, file_id: str) -> None:
        gemini = get_gemini_service()
        file_name = file_id

        async def _attempt() -> None:
            nonlocal file_name
            final_status = DriveFileStatus.PROCESSED
            final_error: str | None = None
            # Cap concurrent DB sessions — never open one session per parallel job.
            async with self._db_sem:
                async with self._session_factory() as session:
                    drive_file = await session.get(DriveFile, file_id)
                    if drive_file is None:
                        return
                    file_name = drive_file.name
                    if drive_file.status != DriveFileStatus.PROCESSING:
                        drive_file.status = DriveFileStatus.PROCESSING
                    await self._index_non_video_file(session, drive_file, gemini)
                    final_status = drive_file.status
                    final_error = drive_file.error_message
                    # Faces/media commit now; drive_files.status waits for the
                    # 100-row status batcher (one UPDATE, not per-file).
                    if final_status in (
                        DriveFileStatus.PROCESSED,
                        DriveFileStatus.SKIPPED,
                    ):
                        drive_file.status = DriveFileStatus.PROCESSING
                        drive_file.error_message = None
                    await session.commit()

            # Faces/media done → batch embed (cache-first), then 100-row status write.
            # When face_jobs_enabled, InsightFace is async on dfi-face-worker; do not
            # embed/finalize yet (face worker pushes embed after detect).
            from app.drive.content_hash import DUPLICATE_CONTENT_PREFIX

            if final_status == DriveFileStatus.SKIPPED:
                await self._status_batcher.enqueue(
                    StatusWrite(
                        file_id=file_id,
                        status=DriveFileStatus.SKIPPED,
                        error_message=final_error,
                        clear_gemini_document=True,
                        bump_synced_at=True,
                    )
                )
            elif (
                final_status == DriveFileStatus.PROCESSED
                and (final_error or "").startswith(DUPLICATE_CONTENT_PREFIX)
            ):
                # Content twin already search-ready — finalize complete, skip embed.
                await self._status_batcher.enqueue(
                    StatusWrite(
                        file_id=file_id,
                        status=DriveFileStatus.PROCESSED,
                        error_message=final_error,
                        clear_gemini_document=True,
                        bump_synced_at=True,
                    )
                )
            elif final_status == DriveFileStatus.ERROR:
                await self._status_batcher.enqueue(
                    StatusWrite(
                        file_id=file_id,
                        status=DriveFileStatus.ERROR,
                        error_message=final_error,
                        clear_gemini_document=True,
                        bump_synced_at=True,
                        unlink_drive_cache=True,
                    )
                )
            elif self._settings.face_jobs_enabled and final_status == DriveFileStatus.PROCESSED:
                logger.info(
                    "Image prepare complete (face job queued): %s (%s)",
                    file_name,
                    file_id,
                )
            else:
                if self._settings.gemini_api_key:
                    await self._embed_queue.push(file_id)
                else:
                    await self._status_batcher.enqueue(
                        StatusWrite(
                            file_id=file_id,
                            status=DriveFileStatus.PROCESSED,
                            error_message=None,
                            clear_gemini_document=True,
                            bump_synced_at=True,
                            unlink_drive_cache=True,
                        )
                    )
            logger.info("Image index complete: %s (%s)", file_name, file_id)

        try:
            await retry_on_deadlock(_attempt, label=f"image index {file_id[:8]}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Parallel image index failed for %s (%s)", file_id, file_name)
            is_missing = (
                isinstance(exc, (DriveConnectorError, DriveDirectError))
                and "404" in str(exc)
            )
            if is_missing:
                async with self._db_sem:
                    async with self._session_factory() as error_session:
                        failed = await error_session.get(DriveFile, file_id)
                        if failed is not None:
                            await remove_drive_file(
                                error_session,
                                failed,
                                gemini=gemini,
                                reason="Drive 404 during image index",
                            )
                            await error_session.commit()
            elif is_transient_db_error(exc) or is_transient_network_error(exc):
                await self._status_batcher.enqueue(
                    StatusWrite(
                        file_id=file_id,
                        status=DriveFileStatus.PENDING,
                        error_message=None,
                    )
                )
                logger.warning(
                    "Re-queued %s after transient error (%s)",
                    file_id,
                    type(exc).__name__,
                )
            else:
                await self._status_batcher.enqueue(
                    StatusWrite(
                        file_id=file_id,
                        status=DriveFileStatus.ERROR,
                        error_message=friendly_index_error_message(exc, max_len=500),
                        unlink_drive_cache=True,
                    )
                )
        finally:
            self._image_tasks.pop(file_id, None)
            self._image_started_at.pop(file_id, None)
            self._schedule_image_refill()

    def _start_video_task(self, file_id: str) -> None:
        self._video_started_at[file_id] = asyncio.get_event_loop().time()
        self._video_tasks[file_id] = asyncio.create_task(
            self._run_video_index_job(file_id),
            name=f"video-index-{file_id[:8]}",
        )

    async def prioritize_video_index(self, file_id: str) -> bool:
        """Claim and start one PENDING/PROCESSING video immediately (carousel fast path).

        Used when /test or /carousel uploads or indexes a video that must finish
        before the user can continue — bypasses size-ordered backlog starvation.
        May briefly exceed video_index_max_parallel.
        """
        fid = (file_id or "").strip()
        if not fid:
            return False
        if not self._settings.video_indexing_enabled:
            return False
        from app.db.advisory_locks import (
            LOCK_VIDEO_INDEX_CLAIM,
            try_acquire_advisory_lock,
        )

        claim_lock = await try_acquire_advisory_lock(
            LOCK_VIDEO_INDEX_CLAIM, name="video_index_claim"
        )
        if claim_lock is None:
            return fid in self._video_tasks
        try:
            return await self._prioritize_video_index_locked(fid)
        finally:
            await claim_lock.release()

    async def _prioritize_video_index_locked(self, fid: str) -> bool:
        """Schedule a priority video while the global video-claim lock is held."""
        self._prune_video_tasks()
        if fid in self._video_tasks:
            return True

        async with self._session_factory() as session:
            drive_file = await session.get(DriveFile, fid)
            if drive_file is None:
                return False
            if not is_video_mime(drive_file.mime_type):
                return False
            if drive_file.status == DriveFileStatus.PROCESSED:
                return True
            if drive_file.status == DriveFileStatus.PROCESSING:
                await session.commit()
                self._start_video_task(fid)
                logger.info("Prioritized adopt PROCESSING video index for %s", fid)
                return True
            if drive_file.status != DriveFileStatus.PENDING:
                return False
            if drive_file.content_hash and drive_file.content_hash_algo:
                from app.drive.conflicts import apply_dedupe_on_upsert

                skip_key = await apply_dedupe_on_upsert(
                    session,
                    drive_file,
                    algo=drive_file.content_hash_algo,
                    digest=drive_file.content_hash,
                )
                if skip_key:
                    _log_skip(drive_file, skip_key)
                    await session.commit()
                    return True
            n = await bulk_claim_files(session, [fid])
            await session.commit()
            if not n:
                # Race: another worker claimed it — adopt if now PROCESSING.
                refreshed = drive_file
                await session.refresh(refreshed)
                if refreshed is None or refreshed.status not in (
                    DriveFileStatus.PROCESSING,
                    DriveFileStatus.PROCESSED,
                ):
                    return False
                if refreshed.status == DriveFileStatus.PROCESSED:
                    return True

        self._start_video_task(fid)
        logger.info("Prioritized carousel video index for %s", fid)
        return True

    async def ensure_parallel_video_indexing(self) -> int:
        """
        Start background index jobs for pending videos up to video_index_max_parallel.
        Adopts orphaned PROCESSING videos, then claims other PENDING videos.
        """
        from app.db.advisory_locks import LOCK_VIDEO_INDEX_CLAIM, advisory_lock
        from app.storage import indexing_disk_ready

        if not indexing_disk_ready(
            self._settings.video_cache_dir,
            high_water_bytes=self._settings.index_disk_high_water_bytes,
        ):
            logger.warning(
                "video_index_paused_disk_low path=%s high_water_bytes=%s",
                self._settings.video_cache_dir,
                self._settings.index_disk_high_water_bytes,
            )
            return 0

        async with advisory_lock(
            LOCK_VIDEO_INDEX_CLAIM, name="video_index_claim", blocking=False
        ) as got:
            if not got:
                return 0
            return await self._ensure_parallel_video_indexing_locked()

    async def _ensure_parallel_video_indexing_locked(self) -> int:
        """Fill video slots while the cross-process claim lock is held."""
        self._prune_video_tasks()
        await self.release_stalled_processing()
        if not self._settings.video_indexing_enabled:
            # Pause lane: stop in-flight videos and free slots for images.
            await self.pause_video_indexing_lane(requeue=True)
            return 0

        max_parallel = max(0, self._settings.video_index_max_parallel)
        if max_parallel <= 0:
            await self.pause_video_indexing_lane(requeue=True)
            return 0

        started = await self.recover_cached_video_indexes()

        async with self._session_factory() as session:
            paused_paths = await load_paused_folder_paths(session)
            processing = list(
                (
                    await session.execute(
                        select(DriveFile).where(DriveFile.status == DriveFileStatus.PROCESSING)
                    )
                ).scalars().all()
            )
            adopt_ids: list[str] = []
            for drive_file in processing:
                if not is_video_mime(drive_file.mime_type):
                    continue
                if drive_file.id in self._video_tasks:
                    continue
                if is_file_indexing_paused(drive_file.path, paused_paths):
                    continue
                if len(self._video_tasks) + len(adopt_ids) >= max_parallel:
                    continue
                adopt_ids.append(drive_file.id)

        for file_id in adopt_ids:
            if file_id in self._video_tasks:
                continue
            self._start_video_task(file_id)
            started += 1
            logger.info("Adopted orphaned PROCESSING video index for %s", file_id)

        claimed_ids: list[str] = []

        async with self._session_factory() as session:
            occupied = await self._occupied_video_ids(session)
            slots = max_parallel - len(occupied)
            if slots <= 0:
                now = asyncio.get_event_loop().time()
                for fid in occupied:
                    started_at = self._video_started_at.get(fid)
                    elapsed = (now - started_at) if started_at is not None else -1
                    logger.info("video_slot_busy file_id=%s elapsed_sec=%.0f", fid, elapsed)
                return started

            paused_paths = await load_paused_folder_paths(session)

            # Filter video MIME in SQL so a large image backlog cannot starve
            # video claims (INDEX_PREFER_SMALL_FILES puts videos at the back).
            pending = list(
                (
                    await session.execute(
                        select(DriveFile)
                        .where(
                            DriveFile.status == DriveFileStatus.PENDING,
                            or_(
                                DriveFile.mime_type.in_(tuple(INDEXABLE_VIDEO_TYPES)),
                                DriveFile.mime_type.like("video/%"),
                            ),
                        )
                        .order_by(*pending_order_by(self._settings))
                        .limit(claim_window(self._settings, slots))
                    )
                ).scalars().all()
            )

            dirty = False
            for drive_file in pending:
                if len(claimed_ids) >= slots:
                    break
                if not is_video_mime(drive_file.mime_type):
                    continue
                if is_macos_junk_name(drive_file.name):
                    await session.delete(drive_file)
                    dirty = True
                    continue
                if apply_video_too_large_skip(drive_file):
                    _log_skip(drive_file, "video_too_large")
                    dirty = True
                    continue
                if is_file_indexing_paused(drive_file.path, paused_paths):
                    continue
                if drive_file.id in occupied:
                    continue
                # Skip known content/name conflicts without occupying a video slot.
                if drive_file.content_hash and drive_file.content_hash_algo:
                    from app.drive.conflicts import (
                        apply_dedupe_on_upsert,
                        find_older_pending_same_hash,
                    )

                    skip_key = await apply_dedupe_on_upsert(
                        session,
                        drive_file,
                        algo=drive_file.content_hash_algo,
                        digest=drive_file.content_hash,
                    )
                    if skip_key:
                        _log_skip(drive_file, skip_key)
                        dirty = True
                        continue
                    older = await find_older_pending_same_hash(
                        session,
                        algo=drive_file.content_hash_algo,
                        digest=drive_file.content_hash,
                        exclude_id=drive_file.id,
                        created_at=drive_file.created_at,
                    )
                    if older is not None:
                        continue
                claimed_ids.append(drive_file.id)
                occupied.add(drive_file.id)

            if claimed_ids:
                claimed_ids = await bulk_claim_file_ids(session, claimed_ids)
                n = len(claimed_ids)
                dirty = True
                logger.info("bulk_claim_videos count=%d rowcount=%d", len(claimed_ids), n)
            # Commit junk/dedupe skips + bulk claim in one transaction.
            if dirty or claimed_ids:
                await session.commit()

        for file_id in claimed_ids:
            self._start_video_task(file_id)
            logger.info("Started parallel video index for %s", file_id)

        return started + len(claimed_ids)

    async def recover_cached_video_indexes(self) -> int:
        """Recover YouTube rows whose DB transcript and local frames survive.

        This conservative path handles a stalled download without discarding
        usable indexed data. It requires at least two non-empty transcript
        segments and at least one cached JPEG, then enqueues the normal
        post-index carousel worker after committing the status transition.
        """
        recovered: list[str] = []
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(DriveFile).where(
                            DriveFile.status == DriveFileStatus.ERROR,
                            DriveFile.source == "youtube",
                            DriveFile.mime_type.like("video/%"),
                        )
                    )
                ).scalars().all()
            )
            for drive_file in rows:
                media = await session.scalar(
                    select(Media).where(Media.drive_file_id == drive_file.id)
                )
                if media is None:
                    continue
                cue_count = await session.scalar(
                    select(func.count(VideoSegment.id)).where(
                        VideoSegment.media_id == media.id,
                        VideoSegment.text != "",
                    )
                )
                frames_dir = Path(self._settings.thumbnail_dir) / "video" / drive_file.id
                if not _cached_video_recovery_eligible(int(cue_count or 0), frames_dir):
                    continue
                drive_file.status = DriveFileStatus.PROCESSED
                drive_file.error_message = None
                drive_file.last_synced_at = datetime.now(timezone.utc)
                recovered.append(drive_file.id)
            if recovered:
                await session.commit()

        for file_id in recovered:
            self._start_carousel_task(file_id)
            logger.info("Recovered cached YouTube index from transcript+frames: %s", file_id)
        return len(recovered)

    async def _run_video_index_job(self, file_id: str) -> None:
        """Same video pipeline as sequential indexing, isolated session per file."""
        from app.db.advisory_locks import try_acquire_advisory_lock, video_index_lock_key

        gemini = get_gemini_service()
        file_name = file_id
        execution_lock = await try_acquire_advisory_lock(
            video_index_lock_key(file_id),
            name=f"video_index:{file_id[:32]}",
        )
        if execution_lock is None:
            logger.info("Video index already executing elsewhere; skipped duplicate: %s", file_id)
            self._video_tasks.pop(file_id, None)
            self._video_started_at.pop(file_id, None)
            self._schedule_video_refill()
            return
        try:
            async with self._session_factory() as session:
                drive_file = await session.get(DriveFile, file_id)
                if drive_file is None:
                    return
                file_name = drive_file.name
                if apply_video_too_large_skip(drive_file):
                    _log_skip(drive_file, "video_too_large")
                    await session.commit()
                    return

                old_document = drive_file.gemini_document_name
                if old_document:
                    await asyncio.to_thread(gemini.delete_document, old_document)
                    drive_file.gemini_document_name = None

                listing = None
                if needs_drive_folder_listing(drive_file):
                    listing = await self._client.list_folder_files()
                result = await process_video_file(
                    session,
                    drive_file,
                    self._client,
                    self._settings,
                    listing=listing,
                    gemini=gemini,
                )
                from app.drive.conflicts import is_duplicate_content_complete

                # Twin already indexed — keep PROCESSED + twin pointer; no download/carousel.
                if result is None or is_duplicate_content_complete(drive_file):
                    drive_file.status = DriveFileStatus.PROCESSED
                    drive_file.last_synced_at = datetime.now(timezone.utc)
                    try:
                        from app.workers.index_tat import stamp_completed_at

                        await stamp_completed_at(
                            session, [file_id], now=drive_file.last_synced_at, reason="processed"
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception("index_tat_complete_stamp_failed file_id=%s", file_id[:12])
                    await session.commit()
                    logger.info(
                        "Video index skipped duplicate_content: %s (%s)",
                        file_name,
                        file_id,
                    )
                    return

                if self._settings.face_jobs_enabled and result.face_job_queued:
                    drive_file.status = DriveFileStatus.PROCESSING
                    drive_file.error_message = None
                    await session.commit()
                    logger.info(
                        "Video prepare complete (face job queued): %s (%s)",
                        file_name,
                        file_id,
                    )
                    return

                drive_file.status = DriveFileStatus.PROCESSED
                drive_file.error_message = None
                drive_file.gemini_document_name = result.gemini_document_name
                drive_file.last_synced_at = datetime.now(timezone.utc)
                from app.drive.media_cache import unlink_drive_source_cache

                unlink_drive_source_cache(drive_file, self._settings)
                try:
                    from app.workers.index_tat import stamp_completed_at

                    await stamp_completed_at(
                        session, [file_id], now=drive_file.last_synced_at, reason="processed"
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("index_tat_complete_stamp_failed file_id=%s", file_id[:12])
                await session.commit()
                # Carousel generation is deliberately after the index commit and
                # uses its own session so a slow/failed artifact can never hold
                # the indexing transaction open.
                self._start_carousel_task(file_id)
                logger.info("Video index complete: %s (%s)", file_name, file_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Parallel video index failed for %s (%s)", file_id, file_name)

            async with self._session_factory() as error_session:
                failed = await error_session.get(DriveFile, file_id)
                if failed is not None:
                    is_missing = (
                        isinstance(exc, (DriveConnectorError, DriveDirectError))
                        and "404" in str(exc)
                        and not is_youtube_source(failed)
                    )
                    if is_missing:
                        await remove_drive_file(
                            error_session,
                            failed,
                            gemini=gemini,
                            reason="Drive 404 during video index",
                        )
                    elif is_transient_db_error(exc) or is_transient_network_error(exc):
                        failed.status = DriveFileStatus.PENDING
                        failed.error_message = None
                        logger.warning(
                            "Re-queued video %s after transient error (%s)",
                            file_id,
                            type(exc).__name__,
                        )
                    else:
                        failed.status = DriveFileStatus.ERROR
                        failed.error_message = friendly_index_error_message(exc, max_len=500)
                        failed.last_synced_at = datetime.now(timezone.utc)
                        from app.drive.media_cache import unlink_drive_source_cache

                        unlink_drive_source_cache(failed, self._settings)
                        try:
                            from app.workers.index_tat import stamp_completed_at

                            await stamp_completed_at(
                                error_session,
                                [file_id],
                                now=failed.last_synced_at,
                                reason="error",
                            )
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                "index_tat_complete_stamp_failed file_id=%s", file_id[:12]
                            )
                    await error_session.commit()
        finally:
            await execution_lock.release()
            self._video_tasks.pop(file_id, None)
            self._video_started_at.pop(file_id, None)
            self._schedule_video_refill()

    def _start_carousel_task(self, file_id: str) -> None:
        existing = self._carousel_tasks.get(file_id)
        if existing and not existing.done():
            return
        self._carousel_started_at[file_id] = asyncio.get_event_loop().time()
        self._carousel_tasks[file_id] = asyncio.create_task(
            self._run_carousel_generation(file_id),
            name=f"carousel:{file_id}",
        )

    def _schedule_carousel_drain(self) -> None:
        """Keep the captioned backlog moving without a wide startup fan-out."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._drain_carousel_backlog(), name="carousel-drain")

    async def _drain_carousel_backlog(self) -> None:
        async with self._carousel_drain_lock:
            await asyncio.sleep(CAROUSEL_DRAIN_DELAY_SEC)
            try:
                await self.reclaim_stale_carousel_locks()
                await self.resume_carousel_generation()
            except Exception:  # noqa: BLE001
                logger.exception("Carousel backlog drain failed")

    def _cancel_overdue_carousel_tasks(self, live: set[str]) -> set[str]:
        """Cancel jobs that outlived the stale window so their slot comes back.

        A task blocked on an unresponsive upstream call still counts as live, so
        without this the row would be skipped by every reclaim pass and the two
        concurrency slots would stay pinned indefinitely.
        """
        now = asyncio.get_event_loop().time()
        cancelled: set[str] = set()
        for file_id in live:
            started = self._carousel_started_at.get(file_id)
            if started is None or now - started < CAROUSEL_LOCK_STALE_SEC:
                continue
            task = self._carousel_tasks.get(file_id)
            if task is not None and not task.done():
                task.cancel()
            cancelled.add(file_id)
        if cancelled:
            logger.warning(
                "Cancelled %d carousel job(s) stuck past %.0fs", len(cancelled), CAROUSEL_LOCK_STALE_SEC
            )
        return cancelled

    async def reclaim_stale_carousel_locks(self, *, orphaned: bool = False) -> int:
        """Release `processing` locks no live task owns.

        A process killed mid-generation leaves the row locked forever, and the
        claim refuses rows already marked processing — so the backlog would
        stall. Being killed is not a failure, so the attempt counter is rolled
        back too; genuine failures land on `error` and keep their count.

        With ``orphaned`` set, every processing row is stale by definition (a
        freshly started process owns no carousel tasks). Otherwise only rows
        locked longer than the stale window are released.
        """
        live = {fid for fid, task in self._carousel_tasks.items() if not task.done()}
        if live and not orphaned:
            live -= self._cancel_overdue_carousel_tasks(live)
        conditions = [DriveFile.carousel_status == "processing"]
        if live:
            conditions.append(DriveFile.id.notin_(live))
        if not orphaned:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=CAROUSEL_LOCK_STALE_SEC)
            conditions.append(
                or_(DriveFile.carousel_locked_at.is_(None), DriveFile.carousel_locked_at < cutoff)
            )
        async with self._session_factory() as session:
            result = await session.execute(
                update(DriveFile)
                .where(*conditions)
                .values(
                    carousel_status="idle",
                    carousel_lock_token=None,
                    carousel_lock_input_hash=None,
                    carousel_locked_at=None,
                    carousel_attempts=0,
                )
            )
            await session.commit()
        released = int(result.rowcount or 0)
        if released:
            logger.info("Released %d stale carousel lock(s)", released)
        return released

    async def resume_carousel_generation(self, *, limit: int = 2) -> int:
        """Resume a small batch of missing carousel artifacts.

        Only captioned videos qualify (same definition as the carousel video
        pickers: at least one non-empty transcript cue) — an uncaptioned video
        has nothing to build slides from. Starting every PROCESSED video at
        once saturated the event loop and DB pool, so concurrency is capped.
        """
        active = sum(1 for t in self._carousel_tasks.values() if not t.done())
        slots = max(0, int(limit) - active)
        if slots <= 0:
            return 0
        has_cues = _captioned_predicate()
        async with self._session_factory() as session:
            ready_ids = (
                await session.execute(
                    select(CarouselGenerationSave.drive_file_id).where(
                        CarouselGenerationSave.kind == "carousel",
                        CarouselGenerationSave.status == "ready",
                    )
                )
            ).scalars().all()
            ready_set = {str(x) for x in ready_ids}
            ids = list(
                (
                    await session.execute(
                        select(DriveFile.id).where(
                            DriveFile.status == DriveFileStatus.PROCESSED,
                            DriveFile.mime_type.like("video/%"),
                            has_cues,
                            # A video that has already failed repeatedly must not
                            # be retried forever by the drain loop.
                            DriveFile.carousel_attempts < CAROUSEL_MAX_ATTEMPTS,
                        )
                        .order_by(
                            case((DriveFile.source == "youtube", 0), else_=1),
                            DriveFile.last_synced_at.desc().nulls_last(),
                        )
                        .limit(max(slots * 8, slots))
                    )
                ).scalars().all()
            )
        started = 0
        for file_id in ids:
            if started >= slots:
                break
            if str(file_id) in ready_set:
                continue
            before = self._carousel_tasks.get(file_id)
            self._start_carousel_task(file_id)
            after = self._carousel_tasks.get(file_id)
            if after is not None and after is not before:
                started += 1
        if started:
            logger.info(
                "Resumed carousel generation for %d video(s) (cap=%d)",
                started,
                limit,
            )
        return started

    async def _run_carousel_generation(self, file_id: str) -> None:
        """Claim the carousel pipeline before doing background work."""
        token = uuid.uuid4().hex
        async with self._session_factory() as claim_session:
            # Uncaptioned videos have no transcript to build slides from, so
            # running the pipeline could only ever fail. Skip before claiming
            # and leave the row idle rather than recording a bogus error.
            if not await _video_is_captioned(claim_session, file_id):
                self._carousel_tasks.pop(file_id, None)
                return
            # A ready artifact may be a user edit. Never replace it with a
            # later default generated by an indexing completion callback.
            ready = await claim_session.scalar(
                select(CarouselGenerationSave).where(
                    CarouselGenerationSave.drive_file_id == file_id,
                    CarouselGenerationSave.kind == "carousel",
                    CarouselGenerationSave.status == "ready",
                ).limit(1)
            )
            if ready is not None:
                payload = ready.payload or {}
                incomplete_auto_save = (
                    ready.source in {"background", "generate"}
                    and (not payload.get("themes") or not payload.get("layouts"))
                )
                if not incomplete_auto_save:
                    await claim_session.execute(
                        update(DriveFile)
                        .where(DriveFile.id == file_id)
                        .values(carousel_status="ready", carousel_error=None)
                    )
                    await claim_session.commit()
                    self._carousel_tasks.pop(file_id, None)
                    self._schedule_carousel_drain()
                    return
            claimed = await claim_session.execute(
                update(DriveFile)
                .where(
                    DriveFile.id == file_id,
                    DriveFile.status == DriveFileStatus.PROCESSED,
                    DriveFile.carousel_status != "processing",
                )
                .values(
                    carousel_status="processing",
                    carousel_lock_token=token,
                    carousel_locked_at=datetime.now(timezone.utc),
                    carousel_error=None,
                    carousel_attempts=DriveFile.carousel_attempts + 1,
                )
            )
            if not claimed.rowcount:
                self._carousel_tasks.pop(file_id, None)
                return
            await claim_session.commit()
        try:
            await self._run_carousel_generation_impl(file_id)
        except Exception as exc:  # noqa: BLE001
            # Keep the failure visible and retryable; the indexing job is
            # already committed and must not be rolled back or blocked.
            async with self._session_factory() as error_session:
                await error_session.execute(
                    update(DriveFile)
                    .where(DriveFile.id == file_id, DriveFile.carousel_lock_token == token)
                    .values(carousel_status="error", carousel_error=str(exc)[:2000])
                )
                await error_session.commit()
            logger.exception("Background carousel generation failed for %s", file_id)
        finally:
            async with self._session_factory() as release_session:
                await release_session.execute(
                    update(DriveFile)
                    .where(
                        DriveFile.id == file_id,
                        DriveFile.carousel_lock_token == token,
                    )
                    .values(
                        carousel_lock_token=None,
                        carousel_lock_input_hash=None,
                        carousel_locked_at=None,
                    )
                )
                await release_session.commit()
            self._carousel_tasks.pop(file_id, None)
            self._carousel_started_at.pop(file_id, None)
            self._schedule_carousel_drain()

    async def _run_carousel_generation_impl(self, file_id: str) -> None:
        """Run the generation job under the concurrency cap, bounded in time."""
        async with self._carousel_semaphore:
            try:
                await asyncio.wait_for(
                    self._run_carousel_generation_job(file_id),
                    timeout=CAROUSEL_JOB_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    f"carousel generation exceeded {CAROUSEL_JOB_TIMEOUT_SEC:.0f}s"
                ) from exc
        self._carousel_tasks.pop(file_id, None)

    async def _run_carousel_generation_job(self, file_id: str) -> None:
        """Run themes -> hooks/topics -> exact copy -> images -> ready artifact."""
        async with self._session_factory() as session:
            from app.routers.carousel_script import (
                CarouselGenerateRequest,
                PipelineThemeSlice,
                TimedPick,
                _carousel_pipeline_generate_impl,
                _load_video_cues,
            )
            from app.llm.carousel_llm import resolve_carousel_llm
            from app.search.carousel_pipeline import (
                build_harmonized_themes,
                extract_hooks_and_topics_async,
            )

            row, cues = await _load_video_cues(session, file_id)
            if row.status != DriveFileStatus.PROCESSED or len(cues) < 2:
                raise RuntimeError("processed video has no usable transcript cues")
            llm = resolve_carousel_llm()
            themes, _source, _warning = await build_harmonized_themes(
                cues=cues,
                video_name=row.name,
                search_entity=None,
                api_key=llm["api_key"],
                model=llm["model"],
                claude_api_key=llm["claude_api_key"],
                claude_model=llm["claude_model"],
                provider=llm["provider"],
                openrouter_api_key=llm["openrouter_api_key"],
                openrouter_model=llm["openrouter_model"],
                openrouter_base_url=llm["openrouter_base_url"],
            )
            if not themes:
                raise RuntimeError("could not derive default carousel themes")
            theme_slices = [
                PipelineThemeSlice(
                    theme_id=str(t.get("theme_id") or f"theme_{i}"),
                    title=str(t.get("title") or "Theme"),
                    start_sec=float(t.get("start_sec") or t.get("start_timestamp") or 0),
                    end_sec=t.get("end_sec") if t.get("end_sec") is not None else t.get("end_timestamp"),
                    summary=str(t.get("summary") or ""),
                )
                for i, t in enumerate(themes[:8])
            ]
            extracted = await extract_hooks_and_topics_async(
                cues,
                start_sec=theme_slices[0].start_sec,
                end_sec=None,
                theme_title=" -> ".join(t.title for t in theme_slices[:4]),
                theme_summary=" ".join(t.summary for t in theme_slices[:4]),
                search_entity=None,
                api_key=llm["api_key"],
                model=llm["model"],
                claude_api_key=llm["claude_api_key"],
                claude_model=llm["claude_model"],
                provider=llm["provider"],
                openrouter_api_key=llm["openrouter_api_key"],
                openrouter_model=llm["openrouter_model"],
                openrouter_base_url=llm["openrouter_base_url"],
                english_cues=None,
            )
            hooks_raw = list(extracted.get("hooks") or [])
            topics_raw = list(extracted.get("topics") or [])
            if not hooks_raw or not topics_raw:
                raise RuntimeError("could not derive default carousel hooks and topics")

            def timed(item: dict, prefix: str, index: int) -> TimedPick:
                return TimedPick(
                    id=str(item.get("id") or f"{prefix}_{index}"),
                    text=str(item.get("text") or ""),
                    start_sec=float(item.get("start_sec") or 0),
                    end_sec=item.get("end_sec"),
                    theme_id=item.get("theme_id"),
                    topic_id=item.get("topic_id"),
                    topic_text=item.get("topic_text"),
                    original_text=item.get("original_text"),
                )

            hooks = [timed(h, "hook", i) for i, h in enumerate(hooks_raw[:8]) if h.get("text")]
            topics = [timed(t, "topic", i) for i, t in enumerate(topics_raw[:8]) if t.get("text")]
            request = CarouselGenerateRequest(
                drive_file_id=file_id,
                video_name=row.name,
                themes=theme_slices,
                hooks=hooks,
                topics=topics,
                min_slides=6,
                max_slides=8,
                select_images=True,
            )
            result = await _carousel_pipeline_generate_impl(request, session)
            save = await session.get(CarouselGenerationSave, result.get("save_id"))
            if save is None or save.status != "ready":
                raise RuntimeError("full carousel generator did not persist READY output")
            payload = dict(save.payload or {})
            payload.update({
                "themes": themes,
                "topics": topics_raw,
                "hooks": hooks_raw,
                "topic_tree": extracted.get("topic_tree") or [],
                "layouts": payload.get("layouts") or {
                    "single_1": {
                        "layout_mode": "single_1",
                        "carousels": payload.get("carousels") or [],
                    },
                    "split_2": {
                        "layout_mode": "split_2",
                        "carousels": payload.get("carousels") or [],
                    },
                },
                "layout_modes": ["single_1", "split_2"],
                "images_ready": True,
                "frames_prewarmed": True,
            })
            save.payload = _database_safe_payload(payload)
            save.layout_mode = "single_1"
            save.source = "background"
            await session.commit()
            await session.execute(
                update(DriveFile)
                .where(DriveFile.id == file_id)
                .values(carousel_status="ready", carousel_error=None)
            )
            await session.commit()

    async def release_stalled_processing(self) -> int:
        """Mark long-stuck PROCESSING videos as ERROR so slots free up."""
        stall_sec = max(60, int(self._settings.video_index_stall_seconds or 3600))
        now = datetime.now(timezone.utc)
        loop_now = asyncio.get_event_loop().time()
        released = 0

        async with self._session_factory() as session:
            processing = list(
                (
                    await session.execute(
                        select(DriveFile).where(DriveFile.status == DriveFileStatus.PROCESSING)
                    )
                ).scalars().all()
            )
            for drive_file in processing:
                if not is_video_mime(drive_file.mime_type):
                    continue
                started_mono = self._video_started_at.get(drive_file.id)
                if started_mono is not None:
                    elapsed = loop_now - started_mono
                elif drive_file.last_synced_at is not None:
                    elapsed = (now - drive_file.last_synced_at).total_seconds()
                else:
                    elapsed = stall_sec + 1
                if elapsed < stall_sec:
                    logger.info(
                        "video_slot_active file_id=%s elapsed_sec=%.0f name=%s",
                        drive_file.id,
                        elapsed,
                        drive_file.name,
                    )
                    continue
                task = self._video_tasks.get(drive_file.id)
                if task is not None and not task.done():
                    logger.info(
                        "video_slot_active file_id=%s elapsed_sec=%.0f name=%s",
                        drive_file.id,
                        elapsed,
                        drive_file.name,
                    )
                    continue
                from app.db.advisory_locks import (
                    try_acquire_advisory_lock,
                    video_index_lock_key,
                )

                probe_lock = await try_acquire_advisory_lock(
                    video_index_lock_key(drive_file.id),
                    name=f"video_stall_probe:{drive_file.id[:24]}",
                )
                if probe_lock is None:
                    logger.info(
                        "video_slot_remote_active file_id=%s elapsed_sec=%.0f",
                        drive_file.id,
                        elapsed,
                    )
                    continue
                await probe_lock.release()
                drive_file.status = DriveFileStatus.ERROR
                drive_file.error_message = (
                    f"index_stall: processing exceeded {stall_sec}s without completion"
                )[:2000]
                released += 1
                logger.warning(
                    "video_slot_stalled file_id=%s elapsed_sec=%.0f name=%s",
                    drive_file.id,
                    elapsed,
                    drive_file.name,
                )
            if released:
                await session.commit()
        return released

    async def _index_non_video_file(
        self,
        session: AsyncSession,
        drive_file: DriveFile,
        gemini: GeminiFileSearchService,
    ) -> None:
        file_id = drive_file.id
        file_name = drive_file.name
        old_document = drive_file.gemini_document_name
        if old_document:
            await asyncio.to_thread(gemini.delete_document, old_document)
            drive_file.gemini_document_name = None

        if is_image_mime(drive_file.mime_type, drive_file.name) and not await file_has_media(session, file_id):
            await process_image_file(
                session,
                drive_file,
                self._client,
                self._settings,
            )
            if drive_file.status == DriveFileStatus.SKIPPED:
                return
            from app.drive.conflicts import is_duplicate_content_complete

            # Twin already indexed — keep PROCESSED + twin pointer; do not clear or re-embed.
            if is_duplicate_content_complete(drive_file):
                drive_file.status = DriveFileStatus.PROCESSED
                drive_file.decode_attempts = 0
                drive_file.last_synced_at = datetime.now(timezone.utc)
                return

        # Images: process_image_file already wrote faces + Qdrant image/caption vectors.
        # Non-image/non-video: skipped upstream as unsupported_mime.
        # Never upload into Google File Search — local Qdrant RAG only.
        if old_document:
            # Best-effort clear of legacy Google doc id; delete is a no-op when disabled.
            await asyncio.to_thread(gemini.delete_document, old_document)

        drive_file.status = DriveFileStatus.PROCESSED
        drive_file.error_message = None
        drive_file.decode_attempts = 0
        drive_file.gemini_document_name = None
        drive_file.last_synced_at = datetime.now(timezone.utc)

    @property
    def is_running(self) -> bool:
        return self._running

    async def sync_file_list(self, *, cache_source: str = "manual") -> int:
        async with self._sync_file_list_lock:
            from app.db.session import get_engine

            # Hold one connection for the advisory lock for the whole sync (session-scoped).
            async with get_engine().connect() as lock_conn:
                got = (
                    await lock_conn.execute(text("SELECT pg_try_advisory_lock(87231455)"))
                ).scalar()
                if not got:
                    logger.warning(
                        "sync_file_list skipped — another sync holds pg_advisory_lock(87231455)"
                    )
                    return 0
                try:
                    return await self._sync_file_list_body(cache_source=cache_source)
                finally:
                    await lock_conn.execute(text("SELECT pg_advisory_unlock(87231455)"))

    async def _sync_file_list_body(self, *, cache_source: str = "manual") -> int:
        async with self._session_factory() as migrate_session:
            try:
                from app.drive.path_prefix_migrate import migrate_active_root_path_prefix

                result = await migrate_active_root_path_prefix(migrate_session)
                if result.get("rewritten_files"):
                    await migrate_session.commit()
                else:
                    await migrate_session.rollback()
            except Exception:  # noqa: BLE001
                logger.exception("path_prefix_migrate_failed")
                await migrate_session.rollback()

        listing = await self._client.list_folder_files()
        try:
            from app.drive.file_list_cache import get_file_list_cache

            await get_file_list_cache().replace(listing, source=cache_source)
        except Exception:  # noqa: BLE001 — cache must never block DB sync
            logger.exception("Drive file-list cache update failed after listing")
        seen = 0
        new_pending = 0
        removed = 0
        live_ids: set[str] = set()
        touched_folders: set[str] = set()
        new_file_ids: list[str] = []
        root_folder_id = listing.folder.id if listing.folder else None
        # Dedupe by Drive id (last wins) — duplicate folder rows in one listing
        # used to UniqueViolation mid-sync and abort the whole AI Summit upsert.
        entries_by_id: dict[str, ConnectorFile] = {}
        for entry in listing.files:
            if entry.id:
                entries_by_id[entry.id] = entry
        async with self._session_factory() as session:
            paused_paths = await load_paused_folder_paths(session)
            for entry in entries_by_id.values():
                if entry.mime_type == SHORTCUT_MIME and not entry.is_folder:
                    continue
                live_ids.add(entry.id)
                if entry.is_folder or entry.mime_type == FOLDER_MIME:
                    # Folder markers stay in DB for empty-dir tree nodes only —
                    # never counted as files, never queued (see library_filters).
                    await self._upsert_folder_placeholder(session, entry)
                    continue
                # AppleDouble / .DS_Store: never insert, never queue, never count.
                if is_macos_junk_name(entry.name):
                    existing_junk = await session.get(DriveFile, entry.id)
                    if existing_junk is not None:
                        await session.delete(existing_junk)
                    continue
                # XML / WAV / docs / etc.: never media — do not sync, download, or skip-noise.
                if not is_drive_media_candidate(entry.mime_type, entry.name):
                    existing_noise = await session.get(DriveFile, entry.id)
                    if existing_noise is not None and existing_noise.status in (
                        DriveFileStatus.PENDING,
                        DriveFileStatus.SKIPPED,
                        DriveFileStatus.ERROR,
                    ):
                        # Drop queue noise only; never delete PROCESSED/PROCESSING media.
                        await session.delete(existing_noise)
                    continue
                seen += 1
                was_new = await self._upsert_drive_file(
                    session,
                    entry,
                    paused_paths=paused_paths,
                    root_folder_id=root_folder_id,
                )
                if was_new:
                    new_pending += 1
                    new_file_ids.append(entry.id)
                    try:
                        from app.drive.library_tree import file_folder_path

                        touched_folders.add(file_folder_path(entry.path or "/"))
                    except Exception:  # noqa: BLE001
                        touched_folders.add("/")

            if root_folder_id:
                try:
                    from app.drive.indexed_folders import touch_active_folder_file_count

                    await touch_active_folder_file_count(session, file_count=seen)
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to update indexed folder file count")

            # Commit upserts/dedupe first so a soft-archive failure cannot roll them back.
            restored = await restore_processed_when_media_exists(session)
            if restored:
                logger.info("sync_file_list restored %d media-backed PROCESSED row(s)", restored)
            saved = await restore_archived_when_index_complete(session)
            if saved:
                logger.info(
                    "sync_file_list restored %d archived file(s) that already qualify as PROCESSED",
                    saved,
                )
            # Drop legacy unsupported_mime skips (XML sidecars etc.) — not media, not downloadable.
            from sqlalchemy import delete

            purged = await session.execute(
                delete(DriveFile).where(
                    DriveFile.status == DriveFileStatus.SKIPPED,
                    DriveFile.error_message.like("Unsupported mime type%"),
                )
            )
            n_purged = int(purged.rowcount or 0)
            if n_purged:
                logger.info(
                    "sync_file_list purged %d unsupported_mime skip row(s) (non-media)",
                    n_purged,
                )
            await session.commit()

        # Permanent library: never soft-archive PROCESSED/SKIPPED rows when a
        # listing omits them (folder switch / partial sync). Only demote
        # incomplete queue rows (PENDING/PROCESSING/ERROR) that vanished.
        if not listing.truncated and live_ids and root_folder_id:
            try:
                async with self._session_factory() as session:
                    stale = list(
                        (
                            await session.execute(
                                select(DriveFile)
                                .where(
                                    DriveFile.id.not_in(live_ids),
                                    DriveFile.source == "drive",
                                    DriveFile.root_folder_id == root_folder_id,
                                    DriveFile.status.in_(
                                        (
                                            DriveFileStatus.PENDING,
                                            DriveFileStatus.PROCESSING,
                                            DriveFileStatus.ERROR,
                                        )
                                    ),
                                )
                            )
                        ).scalars().all()
                    )
                    for drive_file in stale:
                        await remove_drive_file(
                            session,
                            drive_file,
                            reason="stale incomplete under sync root (soft-archive)",
                        )
                        removed += 1
                    if removed:
                        await session.commit()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Soft-archive of stale Drive rows failed (upserts already committed)"
                )

        logger.info(
            "Drive sync: folder=%s files=%d new_pending=%d archived=%d truncated=%s",
            listing.folder.name,
            seen,
            new_pending,
            removed,
            listing.truncated,
        )
        try:
            from app.drive.library_folder_media_cache import invalidate_folder_media_cache
            from app.drive.library_shell_cache import get_library_shell_cache

            if touched_folders:
                for folder in touched_folders:
                    invalidate_folder_media_cache(folder, drive_file_ids=new_file_ids)
            else:
                get_library_shell_cache().invalidate()
        except Exception:  # noqa: BLE001
            pass
        return seen

    async def _upsert_folder_placeholder(self, session: AsyncSession, entry: ConnectorFile) -> None:
        """Persist Drive folders (and folder-shortcut markers) so Library shows empty dirs."""
        from app.drive.indexing_pause import normalize_folder_path

        folder_path = normalize_folder_path(entry.path or "/")
        # Store as a folder marker: path is the folder itself (no trailing file name).
        existing = await session.get(DriveFile, entry.id)
        if existing is None:
            try:
                async with session.begin_nested():
                    session.add(
                        DriveFile(
                            id=entry.id,
                            name=entry.name,
                            mime_type=FOLDER_MIME,
                            path=folder_path,
                            modified_time=entry.modified_time,
                            size=None,
                            status=DriveFileStatus.SKIPPED,
                            error_message="folder_marker",
                            source="drive",
                            root_folder_id=None,
                        )
                    )
                    await session.flush()
            except IntegrityError:
                # Concurrent sync already inserted this folder id — load and update.
                existing = await session.get(DriveFile, entry.id)
                if existing is None:
                    raise
        if existing is None:
            return
        existing.name = entry.name
        existing.mime_type = FOLDER_MIME
        existing.path = folder_path
        existing.modified_time = entry.modified_time
        existing.status = DriveFileStatus.SKIPPED
        existing.error_message = "folder_marker"

    async def _upsert_drive_file(
        self,
        session: AsyncSession,
        entry: ConnectorFile,
        *,
        paused_paths: list[str] | None = None,
        root_folder_id: str | None = None,
    ) -> bool:
        from app.drive.indexing_pause import INDEXING_PAUSED_PREFIX, normalize_file_path
        from app.drive.content_hash import hash_from_connector_entry
        from app.drive.conflicts import apply_dedupe_on_upsert

        existing = await session.get(DriveFile, entry.id)
        inferred_mime = infer_image_mime(entry.mime_type, entry.name)
        entry_path = normalize_file_path(entry.path or "/")
        paused = is_file_indexing_paused(entry_path, paused_paths or [])
        hash_info = hash_from_connector_entry(entry)
        algo = hash_info[0] if hash_info else None
        digest = hash_info[1] if hash_info else None
        junk = is_macos_junk_name(entry.name)
        # Defense in depth — sync_file_list should already drop Apple junk.
        if junk:
            if existing is not None:
                await session.delete(existing)
            return False

        if existing is None:
            if paused:
                status = DriveFileStatus.SKIPPED
                error_message = f"{INDEXING_PAUSED_PREFIX} indexing stopped for parent folder"
            elif is_video_mime(inferred_mime or entry.mime_type) and is_video_too_large(
                entry.size_bytes
            ):
                status = DriveFileStatus.SKIPPED
                error_message = video_too_large_message(entry.size_bytes)
            else:
                status = DriveFileStatus.PENDING
                error_message = None
            drive_file = DriveFile(
                id=entry.id,
                name=entry.name,
                mime_type=inferred_mime or entry.mime_type,
                path=entry_path,
                modified_time=entry.modified_time,
                size=entry.size_bytes,
                status=status,
                error_message=error_message,
                content_hash=digest,
                content_hash_algo=algo,
                root_folder_id=root_folder_id,
            )
            try:
                async with session.begin_nested():
                    session.add(drive_file)
                    await session.flush()
            except IntegrityError:
                existing = await session.get(DriveFile, entry.id)
                if existing is None:
                    raise
            else:
                if error_message and error_message.startswith("video_too_large"):
                    _log_skip(drive_file, "video_too_large")
                elif not paused:
                    skip_key = await apply_dedupe_on_upsert(
                        session, drive_file, algo=algo, digest=digest
                    )
                    if skip_key:
                        _log_skip(drive_file, skip_key)
                return True

        from app.drive.cleanup import restore_archived_drive_file

        changed = existing.modified_time != entry.modified_time or existing.name != entry.name
        prev_hash = existing.content_hash
        # First-time hash attach is NOT a content change (prev_hash was None).
        hash_changed = bool(prev_hash and digest and digest != prev_hash)
        newly_hashed = bool(digest and not prev_hash)
        existing.name = entry.name
        existing.mime_type = infer_image_mime(entry.mime_type, entry.name) or entry.mime_type
        existing.path = entry_path
        existing.modified_time = entry.modified_time
        existing.size = entry.size_bytes
        if root_folder_id:
            existing.root_folder_id = root_folder_id
        if digest:
            existing.content_hash = digest
            existing.content_hash_algo = algo
        restored = restore_archived_drive_file(existing)
        # Hard skip oversized videos — never leave them PENDING/PROCESSING.
        if (
            not paused
            and is_video_mime(existing.mime_type)
            and is_video_too_large(existing.size)
            and existing.status
            in (
                DriveFileStatus.PENDING,
                DriveFileStatus.PROCESSING,
                DriveFileStatus.ERROR,
            )
        ):
            existing.status = DriveFileStatus.SKIPPED
            existing.error_message = video_too_large_message(existing.size)
            _log_skip(existing, "video_too_large")
            return True
        if paused and existing.status in (DriveFileStatus.PENDING, DriveFileStatus.ERROR):
            existing.status = DriveFileStatus.SKIPPED
            existing.error_message = f"{INDEXING_PAUSED_PREFIX} indexing stopped for parent folder"
        elif not paused and restored and not (changed or hash_changed):
            # Re-attached unchanged archived file — keep restored PROCESSED/PENDING as-is.
            pass
        elif not paused and (changed or hash_changed or newly_hashed or restored):
            from app.drive.content_hash import DUPLICATE_CONTENT_PREFIX, NAME_CONFLICT_PREFIX

            prior_msg = existing.error_message or ""
            was_conflict_skip = prior_msg.startswith(DUPLICATE_CONTENT_PREFIX) or prior_msg.startswith(
                NAME_CONFLICT_PREFIX
            )
            # Permanent library: NEVER demote PROCESSED on Drive mtime/hash change.
            # Indexing is add-if-missing only; operator retry/backfill is explicit.
            content_changed = changed or hash_changed
            if content_changed and was_conflict_skip and existing.status != DriveFileStatus.PROCESSED:
                existing.status = DriveFileStatus.PENDING
                existing.error_message = None
                existing.gemini_document_name = None
                existing.decode_attempts = 0
            skip_key = await apply_dedupe_on_upsert(
                session,
                existing,
                algo=algo or existing.content_hash_algo,
                digest=digest or existing.content_hash,
            )
            if skip_key:
                _log_skip(existing, skip_key)
        return False

    async def process_pending(self, limit: int | None = None) -> dict[str, int]:
        summary = {"processed": 0, "skipped": 0, "errored": 0, "deferred": 0, "videos_started": 0, "images_started": 0}
        gemini = get_gemini_service()
        processed_count = 0

        async with self._session_factory() as session:
            restored = await restore_processed_when_media_exists(session)
            saved = await restore_archived_when_index_complete(session)
            if restored or saved:
                await session.commit()
                if restored:
                    logger.info("process_pending restored %d media-backed PROCESSED row(s)", restored)
                if saved:
                    logger.info(
                        "process_pending restored %d archived file(s) that already qualify as PROCESSED",
                        saved,
                    )

        summary["videos_started"] = await self.ensure_parallel_video_indexing()
        summary["images_started"] = await self.ensure_parallel_image_indexing()

        while limit is None or processed_count < limit:
            async with self._session_factory() as session:
                paused_paths = await load_paused_folder_paths(session)
                candidates = list(
                    (
                        await session.execute(
                            select(DriveFile)
                            .where(DriveFile.status == DriveFileStatus.PENDING)
                            .order_by(*pending_order_by(self._settings))
                            .limit(claim_window(self._settings, 1))
                        )
                    ).scalars().all()
                )
                drive_file = next(
                    (
                        f
                        for f in candidates
                        if not is_video_mime(f.mime_type)
                        and not is_image_mime(f.mime_type, f.name)
                        and not is_file_indexing_paused(f.path, paused_paths)
                    ),
                    None,
                )
                if drive_file is None:
                    break

                if not is_drive_media_candidate(drive_file.mime_type, drive_file.name):
                    # XML / wav / docs leftovers — never download; drop from queue.
                    await session.delete(drive_file)
                    await session.commit()
                    processed_count += 1
                    continue

                if not is_indexable_mime(drive_file.mime_type, drive_file.name):
                    drive_file.status = DriveFileStatus.SKIPPED
                    drive_file.error_message = (
                        f"Unsupported mime type for indexing: {drive_file.mime_type}"
                    )
                    _log_skip(drive_file, "unsupported_mime")
                    await session.commit()
                    summary["skipped"] += 1
                    processed_count += 1
                    continue

                drive_file.status = DriveFileStatus.PROCESSING
                await session.commit()

                file_id = drive_file.id
                file_name = drive_file.name
                try:
                    async def _attempt() -> None:
                        async with self._session_factory() as work_session:
                            df = await work_session.get(DriveFile, file_id)
                            if df is None:
                                return
                            if df.status != DriveFileStatus.PROCESSING:
                                df.status = DriveFileStatus.PROCESSING
                            await self._index_non_video_file(work_session, df, gemini)
                            await work_session.commit()

                    await retry_on_deadlock(_attempt, label=f"index {file_id[:8]}")
                    summary["processed"] += 1
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Failed to index drive file %s (%s)", file_id, file_name)
                    is_missing = (
                        isinstance(exc, (DriveConnectorError, DriveDirectError))
                        and "404" in str(exc)
                    )
                    async with self._session_factory() as error_session:
                        failed = await error_session.get(DriveFile, file_id)
                        if failed is not None:
                            if is_missing:
                                await remove_drive_file(
                                    error_session,
                                    failed,
                                    gemini=gemini,
                                    reason="Drive 404 during index",
                                )
                            elif is_transient_db_error(exc):
                                failed.status = DriveFileStatus.PENDING
                                failed.error_message = None
                                logger.warning("Re-queued %s after transient DB error", file_id)
                            else:
                                _record_index_failure(failed, exc)
                            await error_session.commit()
                    summary["skipped" if is_missing else "errored"] += 1

                processed_count += 1

        return summary

    async def run_cycle(self, limit: int | None = None) -> dict[str, int]:
        if self._running:
            raise RuntimeError("An indexing cycle is already running")
        self._running = True
        try:
            runtime = get_runtime_settings()
            if runtime.reindex_errored_files or runtime.reindex_skipped_files:
                await requeue_failed_files(
                    self._session_factory,
                    reindex_errored=runtime.reindex_errored_files,
                    reindex_skipped=runtime.reindex_skipped_files,
                )
            # Drain known pending work before the (possibly long) Drive listing sync.
            summary = await self.process_pending(limit=limit)
            seen = await self.sync_file_list()
            more = await self.process_pending(limit=limit)
            for key, value in more.items():
                summary[key] = summary.get(key, 0) + value
            summary["discovered"] = seen
            self.last_run_summary = summary
            self.last_run_at = datetime.now(timezone.utc)
            return summary
        finally:
            try:
                await self._status_batcher.flush()
            except Exception:  # noqa: BLE001
                logger.exception("Final status batch flush failed")
            self._running = False
            # Kick caption/embed backfill as soon as the cycle flag clears.
            try:
                from app.workers.maintenance import (
                    schedule_cache_cleanup_tick,
                    schedule_maintenance_tick,
                )

                schedule_maintenance_tick(self)
                # Flush already ran — clean only durable PROCESSED (+ caption/embed).
                schedule_cache_cleanup_tick()
            except Exception:  # noqa: BLE001
                logger.exception("Post-cycle maintenance schedule failed")
