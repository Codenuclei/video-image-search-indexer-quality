from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile
from app.gemini.service import GeminiFileSearchService


async def remove_drive_file(
    session: AsyncSession,
    drive_file: DriveFile,
    *,
    gemini: GeminiFileSearchService | None = None,
) -> None:
    """Remove a tracked file and its Gemini document if present."""
    document_name = drive_file.gemini_document_name
    file_id = drive_file.id
    if drive_file.media is not None:
        await session.delete(drive_file.media)
    await session.delete(drive_file)
    await session.flush()
    if document_name and gemini is not None:
        await asyncio.to_thread(gemini.delete_document, document_name)
    # Remove the image vector from Qdrant (best-effort)
    try:
        from app.qdrant.images import delete_image_sync
        await asyncio.to_thread(delete_image_sync, file_id)
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.qdrant.image_captions import delete_caption_sync
        await asyncio.to_thread(delete_caption_sync, file_id)
    except Exception:  # noqa: BLE001
        pass
