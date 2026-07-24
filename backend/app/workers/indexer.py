from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import Settings, get_settings
from app.db.models import DriveFile, DriveFileStatus
from app.drive.client import DriveConnectorClient, DriveConnectorError
from app.drive.google_client import DriveDirectError
from app.drive.cleanup import remove_drive_file
from app.drive.schemas import ConnectorFile
from app.gemini.service import GeminiFileSearchService, get_gemini_service
from app.gemini.tags import person_names_for_drive_file
from app.pipelines.common import (
    INDEXABLE_IMAGE_TYPES,
    INDEXABLE_VIDEO_TYPES,
    download_image_for_upload,
    download_to_temp_file,
    file_has_media,
    infer_image_mime,
    is_image_mime,
    is_indexable_mime,
    is_video_mime,
)
from app.pipelines.image import process_image_file
from app.pipelines.video import process_video_file
from app.pipelines.decode_recovery import apply_decode_failure, decode_max_attempts, is_decode_failure_error
from app.drive.traverse import FOLDER_MIME, SHORTCUT_MIME
from app.drive.indexing_pause import (
    is_file_indexing_paused,
    load_paused_folder_paths,
)
from app.drive.indexing_pause import file_under_folder, normalize_folder_path
from app.db.deadlock import is_deadlock_error, is_transient_db_error, retry_on_deadlock
from app.runtime_settings import get_runtime_settings
from app.workers.index_errors import friendly_index_error_message
from app.workers.requeue_failed import requeue_failed_files
from app.workers.claim_order import claim_window, pending_order_by

logger = logging.getLogger(__name__)


def _log_skip(drive_file: DriveFile, reason: str) -> None:
    logger.info(
        "index_skip reason=%s file_id=%s mime=%s size=%s name=%s",
        reason,
        drive_file.id,
        drive_file.mime_type,
        drive_file.size,
        drive_file.name,
    )


def _record_index_failure(drive_file: DriveFile, exc: Exception) -> None:
    raw = str(exc)[:2000]
    msg = friendly_index_error_message(exc, max_len=500)
    if is_decode_failure_error(raw):
        apply_decode_failure(drive_file, raw)
    elif is_transient_db_error(exc):
        # Don't leave files stuck in ERROR forever for lock/abort fallout —
        # auto-index will pick them up again.
        drive_file.status = DriveFileStatus.PENDING
        drive_file.error_message = None
        logger.warning(
            "index_requeue_transient file_id=%s mime=%s err=%s",
            drive_file.id,
            drive_file.mime_type,
            raw[:200],
        )
    else:
        drive_file.status = DriveFileStatus.ERROR
        drive_file.error_message = msg
        logger.warning(
            "index_error file_id=%s mime=%s size=%s err=%s",
            drive_file.id,
            drive_file.mime_type,
            drive_file.size,
            raw[:200],
        )

class IndexingWorker:
    """Syncs Drive files and uploads supported media into a Gemini File Search store."""

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
        """Only in-flight image tasks count as occupied.

        Orphaned PROCESSING rows (no live task, e.g. after restart) must not block
        new claims — they are adopted or re-queued in ensure_parallel_image_indexing.
        """
        del session  # signature kept for call-site symmetry with video path
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
        self._prune_image_tasks()
        max_parallel = self._image_max_parallel()
        started = 0

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
            for drive_file in processing:
                if not is_image_mime(drive_file.mime_type, drive_file.name):
                    continue
                if drive_file.id in self._image_tasks:
                    continue
                if drive_file.id in go_owned:
                    continue
                if is_file_indexing_paused(drive_file.path, paused_paths):
                    requeue_ids.append(drive_file.id)
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
            slots = max_parallel - len(occupied)
            if slots <= 0:
                return started

            paused_paths = await load_paused_folder_paths(session)

            # Filter image MIME in SQL so a large non-image backlog cannot starve
            # the claim window (size-ordered PENDING scan).
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
                        .limit(claim_window(self._settings, slots))
                    )
                ).scalars().all()
            )

            for drive_file in pending:
                if len(claimed_ids) >= slots:
                    break
                if not is_image_mime(drive_file.mime_type, drive_file.name):
                    continue
                if (drive_file.decode_attempts or 0) >= decode_max_attempts():
                    continue
                if is_file_indexing_paused(drive_file.path, paused_paths):
                    continue
                if drive_file.id in occupied:
                    continue
                drive_file.status = DriveFileStatus.PROCESSING
                drive_file.last_synced_at = datetime.now(timezone.utc)
                claimed_ids.append(drive_file.id)
                occupied.add(drive_file.id)

            if claimed_ids:
                await session.commit()

        for file_id in claimed_ids:
            self._start_image_task(file_id)
            logger.info("Started parallel image index for %s", file_id)

        return started + len(claimed_ids)

    async def index_claimed_image(self, file_id: str) -> None:
        """Complete indexing for a file already claimed (e.g. by the Go canary)."""
        await self._run_image_index_job(file_id)

    async def cancel_indexing_under_folder(self, folder_path: str) -> int:
        """Cancel in-flight index jobs for files under a folder path."""
        norm = normalize_folder_path(folder_path)
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

    async def _run_image_index_job(self, file_id: str) -> None:
        gemini = get_gemini_service()
        file_name = file_id

        async def _attempt() -> None:
            nonlocal file_name
            async with self._session_factory() as session:
                drive_file = await session.get(DriveFile, file_id)
                if drive_file is None:
                    return
                file_name = drive_file.name
                if drive_file.status != DriveFileStatus.PROCESSING:
                    drive_file.status = DriveFileStatus.PROCESSING
                await self._index_non_video_file(session, drive_file, gemini)
                await session.commit()
                logger.info("Image index complete: %s (%s)", file_name, file_id)

        try:
            await retry_on_deadlock(_attempt, label=f"image index {file_id[:8]}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Parallel image index failed for %s (%s)", file_id, file_name)
            async with self._session_factory() as error_session:
                failed = await error_session.get(DriveFile, file_id)
                if failed is not None:
                    is_missing = (
                        isinstance(exc, (DriveConnectorError, DriveDirectError))
                        and "404" in str(exc)
                    )
                    if is_missing:
                        await remove_drive_file(error_session, failed, gemini=gemini)
                    elif is_transient_db_error(exc):
                        failed.status = DriveFileStatus.PENDING
                        failed.error_message = None
                        logger.warning("Re-queued %s after transient DB error", file_id)
                    else:
                        _record_index_failure(failed, exc)
                    await error_session.commit()
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

    async def ensure_parallel_video_indexing(self) -> int:
        """
        Start background index jobs for pending videos up to video_index_max_parallel.
        Adopts orphaned PROCESSING videos, then claims other PENDING videos.
        """
        self._prune_video_tasks()
        await self.release_stalled_processing()
        if not self._settings.video_indexing_enabled:
            return 0

        max_parallel = max(1, self._settings.video_index_max_parallel)
        started = 0

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

            for drive_file in pending:
                if len(claimed_ids) >= slots:
                    break
                if not is_video_mime(drive_file.mime_type):
                    continue
                if is_file_indexing_paused(drive_file.path, paused_paths):
                    continue
                if drive_file.id in occupied:
                    continue
                drive_file.status = DriveFileStatus.PROCESSING
                drive_file.last_synced_at = datetime.now(timezone.utc)
                claimed_ids.append(drive_file.id)
                occupied.add(drive_file.id)

            if claimed_ids:
                await session.commit()

        for file_id in claimed_ids:
            self._start_video_task(file_id)
            logger.info("Started parallel video index for %s", file_id)

        return started + len(claimed_ids)

    async def _run_video_index_job(self, file_id: str) -> None:
        """Same video pipeline as sequential indexing, isolated session per file."""
        gemini = get_gemini_service()
        file_name = file_id
        try:
            async with self._session_factory() as session:
                drive_file = await session.get(DriveFile, file_id)
                if drive_file is None:
                    return
                file_name = drive_file.name

                old_document = drive_file.gemini_document_name
                if old_document:
                    await asyncio.to_thread(gemini.delete_document, old_document)
                    drive_file.gemini_document_name = None

                from app.video.youtube_registry import is_youtube_source

                listing = None
                if not is_youtube_source(drive_file):
                    listing = await self._client.list_folder_files()
                result = await process_video_file(
                    session,
                    drive_file,
                    self._client,
                    self._settings,
                    listing=listing,
                    gemini=gemini,
                )
                drive_file.status = DriveFileStatus.PROCESSED
                drive_file.error_message = None
                drive_file.gemini_document_name = result.gemini_document_name
                drive_file.last_synced_at = datetime.now(timezone.utc)
                await session.commit()
                logger.info("Video index complete: %s (%s)", file_name, file_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Parallel video index failed for %s (%s)", file_id, file_name)
            from app.video.youtube_registry import is_youtube_source

            async with self._session_factory() as error_session:
                failed = await error_session.get(DriveFile, file_id)
                if failed is not None:
                    is_missing = (
                        isinstance(exc, (DriveConnectorError, DriveDirectError))
                        and "404" in str(exc)
                        and not is_youtube_source(failed)
                    )
                    if is_missing:
                        await remove_drive_file(error_session, failed, gemini=gemini)
                    elif is_transient_db_error(exc):
                        failed.status = DriveFileStatus.PENDING
                        failed.error_message = None
                        logger.warning("Re-queued video %s after transient DB error", file_id)
                    else:
                        failed.status = DriveFileStatus.ERROR
                        failed.error_message = friendly_index_error_message(exc, max_len=500)
                    await error_session.commit()
        finally:
            self._video_tasks.pop(file_id, None)
            self._video_started_at.pop(file_id, None)
            self._schedule_video_refill()

    async def release_stalled_processing(self) -> int:
        """Mark long-stuck PROCESSING videos as ERROR so slots free up."""
        stall_sec = max(60, int(self._settings.video_index_stall_seconds or 900))
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
                if task and not task.done():
                    task.cancel()
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

        person_names = await person_names_for_drive_file(session, file_id)

        document_name = None
        if is_image_mime(drive_file.mime_type, drive_file.name):
            if self._settings.gemini_file_search_images_enabled:
                async with download_image_for_upload(
                    self._client,
                    file_id,
                    self._settings,
                    mime_type=drive_file.mime_type,
                    file_name=file_name,
                ) as (local_path, upload_mime):
                    document_name = await asyncio.to_thread(
                        gemini.upload_file,
                        local_path=local_path,
                        display_name=file_name,
                        drive_file_id=file_id,
                        drive_path=drive_file.path,
                        mime_type=upload_mime,
                        person_names=person_names,
                    )
        else:
            suffix = ""
            if "." in file_name:
                suffix = file_name[file_name.rindex(".") :]
            async with download_to_temp_file(
                self._client, file_id, self._settings, suffix=suffix
            ) as local_path:
                document_name = await asyncio.to_thread(
                    gemini.upload_file,
                    local_path=local_path,
                    display_name=file_name,
                    drive_file_id=file_id,
                    drive_path=drive_file.path,
                    mime_type=drive_file.mime_type,
                    person_names=person_names,
                )

        if old_document and old_document != document_name:
            await asyncio.to_thread(gemini.delete_document, old_document)

        drive_file.status = DriveFileStatus.PROCESSED
        drive_file.error_message = None
        drive_file.decode_attempts = 0
        drive_file.gemini_document_name = document_name
        drive_file.last_synced_at = datetime.now(timezone.utc)

    @property
    def is_running(self) -> bool:
        return self._running

    async def sync_file_list(self) -> int:
        listing = await self._client.list_folder_files()
        seen = 0
        new_pending = 0
        removed = 0
        live_ids: set[str] = set()
        async with self._session_factory() as session:
            paused_paths = await load_paused_folder_paths(session)
            for entry in listing.files:
                if entry.mime_type == SHORTCUT_MIME and not entry.is_folder:
                    continue
                live_ids.add(entry.id)
                if entry.is_folder or entry.mime_type == FOLDER_MIME:
                    await self._upsert_folder_placeholder(session, entry)
                    continue
                seen += 1
                was_new = await self._upsert_drive_file(session, entry, paused_paths=paused_paths)
                if was_new:
                    new_pending += 1

            if not listing.truncated and live_ids:
                gemini = get_gemini_service()
                stale = list(
                    (
                        await session.execute(
                            select(DriveFile)
                            .where(
                                DriveFile.id.not_in(live_ids),
                                DriveFile.source == "drive",
                            )
                            .options(selectinload(DriveFile.media))
                        )
                    ).scalars().all()
                )
                for drive_file in stale:
                    await remove_drive_file(session, drive_file, gemini=gemini)
                    removed += 1

            await session.commit()
        logger.info(
            "Drive sync: folder=%s files=%d new_pending=%d removed=%d truncated=%s",
            listing.folder.name,
            seen,
            new_pending,
            removed,
            listing.truncated,
        )
        return seen

    async def _upsert_folder_placeholder(self, session: AsyncSession, entry: ConnectorFile) -> None:
        """Persist Drive folders (and folder-shortcut markers) so Library shows empty dirs."""
        existing = await session.get(DriveFile, entry.id)
        folder_path = entry.path if entry.path.startswith("/") else f"/{entry.path}" if entry.path else "/"
        # Store as a folder marker: path is the folder itself (no trailing file name).
        if existing is None:
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
                )
            )
            # Not an indexing skip — structural placeholder for Library only.
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
    ) -> bool:
        from app.drive.indexing_pause import INDEXING_PAUSED_PREFIX

        existing = await session.get(DriveFile, entry.id)
        inferred_mime = infer_image_mime(entry.mime_type, entry.name)
        paused = is_file_indexing_paused(entry.path, paused_paths or [])
        if existing is None:
            if paused:
                status = DriveFileStatus.SKIPPED
                error_message = f"{INDEXING_PAUSED_PREFIX} indexing stopped for parent folder"
            else:
                status = DriveFileStatus.PENDING
                error_message = None
            session.add(
                DriveFile(
                    id=entry.id,
                    name=entry.name,
                    mime_type=inferred_mime or entry.mime_type,
                    path=entry.path,
                    modified_time=entry.modified_time,
                    size=entry.size_bytes,
                    status=status,
                    error_message=error_message,
                )
            )
            return True

        changed = existing.modified_time != entry.modified_time or existing.name != entry.name
        existing.name = entry.name
        existing.mime_type = infer_image_mime(entry.mime_type, entry.name) or entry.mime_type
        existing.path = entry.path
        existing.modified_time = entry.modified_time
        existing.size = entry.size_bytes
        if paused and existing.status in (DriveFileStatus.PENDING, DriveFileStatus.ERROR):
            existing.status = DriveFileStatus.SKIPPED
            existing.error_message = f"{INDEXING_PAUSED_PREFIX} indexing stopped for parent folder"
        if changed:
            existing.decode_attempts = 0
            if existing.status == DriveFileStatus.PROCESSED and not paused:
                existing.status = DriveFileStatus.PENDING
                existing.gemini_document_name = None
        return False

    async def process_pending(self, limit: int | None = None) -> dict[str, int]:
        summary = {"processed": 0, "skipped": 0, "errored": 0, "deferred": 0, "videos_started": 0, "images_started": 0}
        gemini = get_gemini_service()
        processed_count = 0

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
                                await remove_drive_file(error_session, failed, gemini=gemini)
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
            self._running = False
