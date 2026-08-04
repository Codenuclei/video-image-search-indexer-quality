from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.drive.cache_refresh import refresh_drive_file_list_cache
from app.drive.push_channels import ensure_channel_token, get_push_channel_state
from app.workers.triggers import trigger_index_cycle

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

# Debounce bursty Google change notifications.
_last_google_push_at: datetime | None = None
_GOOGLE_PUSH_DEBOUNCE_SEC = 3.0


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


@router.post("/webhooks/drive-changed")
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


def _google_push_headers(request: Request) -> dict[str, str | None]:
    return {
        "channel_id": request.headers.get("X-Goog-Channel-ID")
        or request.headers.get("X-Goog-Channel-Id"),
        "channel_token": request.headers.get("X-Goog-Channel-Token"),
        "resource_id": request.headers.get("X-Goog-Resource-ID")
        or request.headers.get("X-Goog-Resource-Id"),
        "resource_state": (
            request.headers.get("X-Goog-Resource-State") or ""
        ).lower()
        or None,
        "resource_uri": request.headers.get("X-Goog-Resource-URI")
        or request.headers.get("X-Goog-Resource-Uri"),
        "message_number": request.headers.get("X-Goog-Message-Number"),
        "changed": request.headers.get("X-Goog-Changed"),
        "expiration": request.headers.get("X-Goog-Channel-Expiration"),
    }


async def _handle_google_drive_push(request: Request, settings: Settings) -> Response:
    """
    Google Drive push receiver (changes.watch / files.watch).

    Must acknowledge quickly (2xx). Heavy work runs in a background task.
    Docs: https://developers.google.com/workspace/drive/api/guides/push
    """
    global _last_google_push_at

    headers = _google_push_headers(request)
    channel_id = headers["channel_id"]
    resource_state = headers["resource_state"] or ""
    channel_token = headers["channel_token"]

    # Ensure token is loaded from settings for verification.
    ensure_channel_token(settings)
    state = get_push_channel_state()

    # Allow simulated local POSTs when DRIVE_WEBHOOK_ALLOW_UNVERIFIED=true
    # (dev only). Production must present matching channel id/token.
    unverified_ok = settings.drive_webhook_allow_unverified
    if channel_id or channel_token or state.channel_id:
        if not state.verify_notification(
            channel_id=channel_id,
            channel_token=channel_token,
        ):
            if not unverified_ok:
                logger.warning(
                    "Rejected Drive push: channel_id=%s token_match=%s",
                    channel_id,
                    bool(channel_token),
                )
                raise HTTPException(status_code=403, detail="Invalid Drive channel")
            logger.warning(
                "Accepting unverified Drive push (DRIVE_WEBHOOK_ALLOW_UNVERIFIED)"
            )

    logger.info(
        "Drive push: state=%s channel=%s msg=%s changed=%s",
        resource_state or "(none)",
        channel_id,
        headers["message_number"],
        headers["changed"],
    )

    # sync = channel handshake; acknowledge without a full tree walk.
    if resource_state == "sync":
        return Response(status_code=204)

    now = datetime.now(timezone.utc)
    if (
        _last_google_push_at
        and (now - _last_google_push_at).total_seconds() < _GOOGLE_PUSH_DEBOUNCE_SEC
    ):
        return Response(status_code=204)
    _last_google_push_at = now

    reason = f"google_push:{resource_state or 'change'}"
    asyncio.create_task(
        refresh_drive_file_list_cache(source=reason, sync_db=True)
    )
    return Response(status_code=204)


@router.post("/api/webhooks/drive")
@router.post("/webhooks/drive")
async def google_drive_push_webhook(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> Response:
    """Google Drive API push notifications (Option 1)."""
    return await _handle_google_drive_push(request, settings)
