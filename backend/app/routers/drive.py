from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from pydantic import BaseModel

from app.db.models import DriveFile, DriveFileStatus
from app.db.session import get_db
from app.dependencies import get_indexing_worker
from app.drive.client import DriveConnectorError
from app.drive.google_client import DriveDirectClient, DriveDirectError
from app.drive.cleanup import remove_drive_file
from app.drive.indexing_pause import (
    load_paused_folder_paths,
    pause_folder_indexing,
    resume_folder_indexing,
    skip_corrupt_files,
)
from app.drive.library_tree import build_library_tree, folder_node_to_dict
from app.gemini.service import get_gemini_service
from app.pipelines.common import download_to_memory
from app.schemas import DriveFileOut
from app.workers.indexer import IndexingWorker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/drive", tags=["drive"])


class FolderIndexingAction(BaseModel):
    folder_path: str


@router.get("/files", response_model=list[DriveFileOut])
async def list_drive_files(
    status: str | None = None,
    limit: int = 200,
    session: AsyncSession = Depends(get_db),
) -> list[DriveFile]:
    """Lists files as currently tracked from the connected Drive folder."""
    stmt = select(DriveFile).order_by(DriveFile.path).limit(limit)
    if status:
        stmt = stmt.where(DriveFile.status == status)
    return list((await session.execute(stmt)).scalars().all())


@router.get("/library")
async def drive_library(session: AsyncSession = Depends(get_db)) -> dict[str, object]:
    """Folder-wise library tree with caption/embed/index status per file."""
    from app.qdrant.image_captions import get_captions_by_ids_sync, valid_caption_ids_sync
    from app.qdrant.images import existing_image_ids_sync
    from app.workers.maintenance import maintenance_status

    rows = list(
        (await session.execute(select(DriveFile).order_by(DriveFile.path))).scalars().all()
    )
    image_ids = [df.id for df in rows if df.mime_type.startswith("image/")]

    captioned_ids: set[str] = set()
    embedded_ids: set[str] = set()
    caption_texts: dict[str, str] = {}

    if image_ids:
        valid_ids, embedded_ids, caption_texts = await asyncio.gather(
            asyncio.to_thread(valid_caption_ids_sync, image_ids),
            asyncio.to_thread(existing_image_ids_sync, image_ids),
            asyncio.to_thread(get_captions_by_ids_sync, image_ids),
        )
        captioned_ids = valid_ids

    paused_paths = await load_paused_folder_paths(session)

    root, _all_files, summary = build_library_tree(
        rows,
        captioned_ids=captioned_ids,
        embedded_ids=embedded_ids,
        caption_texts=caption_texts,
        paused_folder_paths=paused_paths,
    )

    return {
        "tree": folder_node_to_dict(root),
        "summary": summary,
        "maintenance": maintenance_status(),
        "paused_folders": paused_paths,
    }


@router.post("/sync")
async def sync_drive_files(
    background_tasks: BackgroundTasks,
    worker: IndexingWorker = Depends(get_indexing_worker),
) -> dict[str, str | int | bool]:
    """Fetch the latest Drive folder listing into the database."""
    if worker.is_running:
        raise HTTPException(status_code=409, detail="An indexing run is already in progress")

    async def _sync() -> None:
        try:
            seen = await worker.sync_file_list()
            logger.info("Manual Drive file-list sync: %d file(s)", seen)
        except Exception:  # noqa: BLE001
            logger.exception("Manual Drive file-list sync failed")

    background_tasks.add_task(_sync)
    return {"ok": True, "scheduled": True}


@router.post("/library/folders/pause")
async def pause_folder(
    body: FolderIndexingAction,
    session: AsyncSession = Depends(get_db),
    worker: IndexingWorker = Depends(get_indexing_worker),
) -> dict[str, object]:
    """Stop indexing all files under a library folder."""
    stopped = await pause_folder_indexing(session, body.folder_path)
    await session.commit()
    cancelled = await worker.cancel_indexing_under_folder(body.folder_path)
    return {"ok": True, "folder_path": body.folder_path, "stopped": stopped, "cancelled": cancelled}


@router.post("/library/folders/resume")
async def resume_folder(
    body: FolderIndexingAction,
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Re-enable indexing for a paused library folder."""
    resumed = await resume_folder_indexing(session, body.folder_path)
    await session.commit()
    return {"ok": True, "folder_path": body.folder_path, "resumed": resumed}


@router.post("/skip-corrupt")
async def skip_corrupt(
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Permanently skip corrupt/unreadable pending files so other folders keep indexing."""
    skipped = await skip_corrupt_files(session)
    await session.commit()
    return {"ok": True, "skipped": skipped}


@router.post("/files/{file_id}/retry", response_model=DriveFileOut)
async def retry_drive_file(
    file_id: str,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
    worker: IndexingWorker = Depends(get_indexing_worker),
) -> DriveFile:
    drive_file = await session.get(DriveFile, file_id)
    if drive_file is None:
        raise HTTPException(status_code=404, detail="File not found")

    if drive_file.gemini_document_name:
        gemini = get_gemini_service()
        gemini.delete_document(drive_file.gemini_document_name)

    drive_file.status = DriveFileStatus.PENDING
    drive_file.error_message = None
    drive_file.gemini_document_name = None
    await session.commit()
    await session.refresh(drive_file)

    if not worker.is_running:
        background_tasks.add_task(worker.run_cycle, 1)
    return drive_file


@router.delete("/files/{file_id}", status_code=204)
async def delete_drive_file(file_id: str, session: AsyncSession = Depends(get_db)) -> None:
    stmt = select(DriveFile).where(DriveFile.id == file_id).options(selectinload(DriveFile.media))
    drive_file = (await session.execute(stmt)).scalar_one_or_none()
    if drive_file is None:
        raise HTTPException(status_code=404, detail="File not found")
    gemini = get_gemini_service()
    await remove_drive_file(session, drive_file, gemini=gemini)
    await session.commit()


@router.get("/files/{file_id}/preview")
async def preview_drive_file(file_id: str, session: AsyncSession = Depends(get_db)) -> Response:
    """Return indexed file bytes for inline preview in the UI."""
    import os

    from app.config import get_settings
    from app.video.youtube_cache import video_cache_path
    from app.video.youtube_registry import is_youtube_source

    drive_file = await session.get(DriveFile, file_id)
    if drive_file is None:
        raise HTTPException(status_code=404, detail="File not found")

    if is_youtube_source(drive_file):
        settings = get_settings()
        local_path = video_cache_path(settings, drive_file)
        if local_path.is_file():
            media_type = drive_file.mime_type or "video/webm"
            return FileResponse(
                local_path,
                media_type=media_type,
                filename=drive_file.name,
                headers={"Content-Disposition": f'inline; filename="{drive_file.name}"'},
            )
        raise HTTPException(status_code=404, detail="YouTube local file not on volume yet")

    from app.db.session import get_session_factory
    from app.drive.google_client import DriveDirectClient

    client = DriveDirectClient(session_factory=get_session_factory(), settings=get_settings())
    try:
        content = await download_to_memory(client, file_id)
    except (DriveConnectorError, DriveDirectError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    media_type = drive_file.mime_type or "application/octet-stream"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{drive_file.name}"'},
    )


@router.get("/files/{file_id}/download")
async def download_drive_file(file_id: str, session: AsyncSession = Depends(get_db)) -> Response:
    """Download indexed file bytes as an attachment."""
    from app.config import get_settings
    from app.video.youtube_cache import video_cache_path
    from app.video.youtube_registry import is_youtube_source

    drive_file = await session.get(DriveFile, file_id)
    if drive_file is None:
        raise HTTPException(status_code=404, detail="File not found")

    if is_youtube_source(drive_file):
        settings = get_settings()
        local_path = video_cache_path(settings, drive_file)
        if local_path.is_file():
            media_type = drive_file.mime_type or "video/webm"
            return FileResponse(
                local_path,
                media_type=media_type,
                filename=drive_file.name,
                headers={"Content-Disposition": f'attachment; filename="{drive_file.name}"'},
            )
        raise HTTPException(status_code=404, detail="YouTube local file not on volume yet")

    from app.db.session import get_session_factory
    from app.drive.google_client import DriveDirectClient

    client = DriveDirectClient(session_factory=get_session_factory(), settings=get_settings())
    try:
        content = await download_to_memory(client, file_id)
    except (DriveConnectorError, DriveDirectError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    media_type = drive_file.mime_type or "application/octet-stream"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{drive_file.name}"'},
    )
