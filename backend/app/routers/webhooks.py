from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.workers.triggers import trigger_index_cycle

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class DriveChangedPayload(BaseModel):
    source: str | None = None
    reason: str | None = None
    userId: str | None = None
    fileCount: int | None = None
    timestamp: str | None = None


def _verify_webhook_secret(request: Request, settings: Settings) -> None:
    expected = settings.webhook_secret.strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="WEBHOOK_SECRET is not configured on the indexer backend",
        )
    provided = request.headers.get("X-Webhook-Secret", "").strip()
    if provided != expected:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")


@router.post("/drive-changed")
async def drive_changed_webhook(
    request: Request,
    payload: DriveChangedPayload,
    settings: Settings = Depends(get_settings),
) -> dict[str, str | bool]:
    """Called by Drive Connector when the connected folder changes."""
    _verify_webhook_secret(request, settings)
    reason = payload.reason or "drive_changed"
    logger.info(
        "Drive webhook received: reason=%s files=%s user=%s",
        reason,
        payload.fileCount,
        payload.userId,
    )
    asyncio.create_task(trigger_index_cycle(reason=f"webhook:{reason}"))
    return {"ok": True, "scheduled": True, "reason": reason}
