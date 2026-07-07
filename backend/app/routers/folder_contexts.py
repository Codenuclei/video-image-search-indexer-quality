"""
app/routers/folder_contexts.py
===============================
CRUD for per-folder context descriptions.

GET  /folder-contexts              — list all folder contexts
GET  /folder-contexts/{folder_id}  — get one by folder_path (URL-safe base64)
PUT  /folder-contexts/{folder_id}  — create/update (embeds description)
DELETE /folder-contexts/{folder_id} — remove context
"""
from __future__ import annotations

import asyncio
import base64
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FolderContext
from app.db.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/folder-contexts", tags=["folder-contexts"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class FolderContextIn(BaseModel):
    folder_path: str
    description: str


class FolderContextOut(BaseModel):
    id: int
    folder_path: str
    description: str

    model_config = {"from_attributes": True}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _encode_path(folder_path: str) -> str:
    return base64.urlsafe_b64encode(folder_path.encode()).decode()


def _decode_path(encoded: str) -> str:
    try:
        # Re-add padding stripped by the frontend (btoa output cleaned of '=')
        padded = encoded + "=" * (4 - len(encoded) % 4)
        return base64.urlsafe_b64decode(padded.encode()).decode()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid folder path encoding")


async def _embed_and_store(folder_path: str, description: str) -> None:
    """Embed description with Gemini Embedding 2 and upsert into Qdrant (best-effort)."""
    from app.config import get_settings
    from app.gemini.video_embeddings import embed_text_sync
    from app.qdrant.folder_context import upsert_folder_context_sync

    settings = get_settings()
    if not settings.gemini_api_key or not description.strip():
        return
    try:
        vec = await asyncio.to_thread(embed_text_sync, description)
        await asyncio.to_thread(upsert_folder_context_sync, folder_path, description, vec)
    except Exception as exc:
        logger.warning("Could not embed folder context for %s: %s", folder_path, exc)


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("", response_model=list[FolderContextOut])
async def list_folder_contexts(session: AsyncSession = Depends(get_db)):
    rows = (await session.execute(select(FolderContext).order_by(FolderContext.folder_path))).scalars().all()
    return rows


@router.put("", response_model=FolderContextOut)
async def upsert_folder_context(body: FolderContextIn, session: AsyncSession = Depends(get_db)):
    existing = (
        await session.execute(
            select(FolderContext).where(FolderContext.folder_path == body.folder_path)
        )
    ).scalar_one_or_none()

    if existing:
        existing.description = body.description
        await session.flush()
        row = existing
    else:
        row = FolderContext(folder_path=body.folder_path, description=body.description)
        session.add(row)
        await session.flush()

    await session.commit()
    await session.refresh(row)

    # Embed in background — don't block the response
    asyncio.create_task(_embed_and_store(body.folder_path, body.description))

    return row


@router.delete("/{encoded_path}", status_code=204)
async def delete_folder_context(encoded_path: str, session: AsyncSession = Depends(get_db)):
    folder_path = _decode_path(encoded_path)
    row = (
        await session.execute(
            select(FolderContext).where(FolderContext.folder_path == folder_path)
        )
    ).scalar_one_or_none()
    if row:
        await session.delete(row)
        await session.commit()
        try:
            from app.qdrant.folder_context import delete_folder_context_sync
            await asyncio.to_thread(delete_folder_context_sync, folder_path)
        except Exception:
            pass
