from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import DriveFile, DriveFileStatus
from app.db.session import get_db
from app.dependencies import get_indexing_worker
from app.drive.client import DriveConnectorError
from app.drive.google_client import DriveDirectClient, DriveDirectError
from app.drive.cleanup import remove_drive_file
from app.gemini.service import get_gemini_service
from app.pipelines.common import download_to_memory
from app.schemas import DriveFileOut
from app.workers.indexer import IndexingWorker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/drive", tags=["drive"])


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
    """Return indexed Drive file bytes for inline preview in the UI."""
    drive_file = await session.get(DriveFile, file_id)
    if drive_file is None:
        raise HTTPException(status_code=404, detail="File not found")

    from app.config import get_settings
    from app.db.session import get_session_factory
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
