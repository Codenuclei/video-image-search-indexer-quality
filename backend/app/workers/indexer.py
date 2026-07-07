from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import Settings, get_settings
from app.db.models import DriveFile, DriveFileStatus
from app.drive.client import DriveConnectorClient, DriveConnectorError
from app.drive.google_client import DriveDirectError
from app.drive.cleanup import remove_drive_file
from app.drive.schemas import ConnectorFile
from app.gemini.service import get_gemini_service
from app.gemini.tags import person_names_for_drive_file
from app.pipelines.common import (
    download_image_for_upload,
    download_to_temp_file,
    file_has_media,
    is_image_mime,
    is_indexable_mime,
    is_video_mime,
)
from app.pipelines.image import process_image_file
from app.pipelines.video import process_video_file

logger = logging.getLogger(__name__)


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
            for entry in listing.files:
                if entry.is_folder:
                    continue
                live_ids.add(entry.id)
                seen += 1
                was_new = await self._upsert_drive_file(session, entry)
                if was_new:
                    new_pending += 1

            if not listing.truncated and live_ids:
                gemini = get_gemini_service()
                stale = list(
                    (
                        await session.execute(
                            select(DriveFile)
                            .where(DriveFile.id.not_in(live_ids))
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

    async def _upsert_drive_file(self, session: AsyncSession, entry: ConnectorFile) -> bool:
        existing = await session.get(DriveFile, entry.id)
        if existing is None:
            session.add(
                DriveFile(
                    id=entry.id,
                    name=entry.name,
                    mime_type=entry.mime_type,
                    path=entry.path,
                    modified_time=entry.modified_time,
                    size=entry.size_bytes,
                    status=DriveFileStatus.PENDING,
                )
            )
            return True

        changed = existing.modified_time != entry.modified_time or existing.name != entry.name
        existing.name = entry.name
        existing.mime_type = entry.mime_type
        existing.path = entry.path
        existing.modified_time = entry.modified_time
        existing.size = entry.size_bytes
        if changed and existing.status == DriveFileStatus.PROCESSED:
            existing.status = DriveFileStatus.PENDING
            existing.gemini_document_name = None
        return False

    async def process_pending(self, limit: int | None = None) -> dict[str, int]:
        summary = {"processed": 0, "skipped": 0, "errored": 0, "deferred": 0}
        gemini = get_gemini_service()
        processed_count = 0

        while limit is None or processed_count < limit:
            async with self._session_factory() as session:
                drive_file = (
                    await session.execute(
                        select(DriveFile)
                        .where(DriveFile.status == DriveFileStatus.PENDING)
                        .order_by(DriveFile.modified_time.desc().nulls_last(), DriveFile.name)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if drive_file is None:
                    break

                if not is_indexable_mime(drive_file.mime_type):
                    drive_file.status = DriveFileStatus.SKIPPED
                    drive_file.error_message = f"Unsupported mime type for Gemini File Search: {drive_file.mime_type}"
                    await session.commit()
                    summary["skipped"] += 1
                    processed_count += 1
                    continue

                drive_file.status = DriveFileStatus.PROCESSING
                await session.commit()

                file_id = drive_file.id
                file_name = drive_file.name
                old_document = drive_file.gemini_document_name
                try:
                    if old_document:
                        await asyncio.to_thread(gemini.delete_document, old_document)
                        drive_file.gemini_document_name = None

                    if is_video_mime(drive_file.mime_type):
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
                    else:
                        if is_image_mime(drive_file.mime_type) and not await file_has_media(session, file_id):
                            await process_image_file(
                                session,
                                drive_file,
                                self._client,
                                self._settings,
                            )

                        person_names = await person_names_for_drive_file(session, file_id)

                        document_name = None
                        if is_image_mime(drive_file.mime_type):
                            # Images are searched via Qdrant vector embeddings
                            # (done in process_image_file). Uploading to the
                            # Gemini File Search store is redundant and eats the
                            # shared 10GB quota, so skip it unless explicitly on.
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
                        drive_file.gemini_document_name = document_name
                        drive_file.last_synced_at = datetime.now(timezone.utc)

                    await session.commit()
                    summary["processed"] += 1
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Failed to index drive file %s (%s)", file_id, file_name)
                    await session.rollback()
                    is_missing = (
                        isinstance(exc, (DriveConnectorError, DriveDirectError))
                        and "404" in str(exc)
                    )
                    async with self._session_factory() as error_session:
                        failed = await error_session.get(DriveFile, file_id)
                        if failed is not None:
                            if is_missing:
                                await remove_drive_file(error_session, failed, gemini=gemini)
                            else:
                                failed.status = DriveFileStatus.ERROR
                                failed.error_message = str(exc)[:2000]
                            await error_session.commit()
                    summary["skipped" if is_missing else "errored"] += 1

                processed_count += 1

        return summary

    async def run_cycle(self, limit: int | None = None) -> dict[str, int]:
        if self._running:
            raise RuntimeError("An indexing cycle is already running")
        self._running = True
        try:
            seen = await self.sync_file_list()
            summary = await self.process_pending(limit=limit)
            summary["discovered"] = seen
            self.last_run_summary = summary
            self.last_run_at = datetime.now(timezone.utc)
            return summary
        finally:
            self._running = False
