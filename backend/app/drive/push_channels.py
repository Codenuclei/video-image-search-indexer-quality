"""Google Drive push notification channel registration and renewal.

See: https://developers.google.com/workspace/drive/api/guides/push

Uses ``changes.watch`` so any Drive change for the connected user notifies our
HTTPS webhook. Channel expiration is renewed by re-calling watch with a new id.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.drive.google_client import DriveDirectClient, DriveDirectError, _DRIVE_BASE

logger = logging.getLogger(__name__)

# Drive caps channel lifetime; request ~24h and renew early.
_DEFAULT_CHANNEL_TTL_MS = 24 * 60 * 60 * 1000
_RENEW_BEFORE_SEC = 2 * 60 * 60  # renew when <2h remain


@dataclass
class PushChannelState:
    channel_id: str | None = None
    resource_id: str | None = None
    page_token: str | None = None
    expiration_ms: int | None = None
    address: str | None = None
    token: str | None = None
    last_register_error: str | None = None
    registered_at_mono: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def status(self) -> dict[str, Any]:
        exp_iso = None
        seconds_left = None
        if self.expiration_ms:
            exp = datetime.fromtimestamp(self.expiration_ms / 1000.0, tz=timezone.utc)
            exp_iso = exp.isoformat()
            seconds_left = max(0, int(self.expiration_ms / 1000.0 - time.time()))
        return {
            "channel_id": self.channel_id,
            "resource_id": self.resource_id,
            "page_token": self.page_token,
            "address": self.address,
            "expiration": exp_iso,
            "seconds_until_expiry": seconds_left,
            "has_token": bool(self.token),
            "last_register_error": self.last_register_error,
            "active": bool(self.channel_id and self.resource_id),
        }

    def needs_renewal(self) -> bool:
        if not self.channel_id or not self.expiration_ms:
            return True
        return (self.expiration_ms / 1000.0 - time.time()) < _RENEW_BEFORE_SEC

    def verify_notification(
        self,
        *,
        channel_id: str | None,
        channel_token: str | None,
    ) -> bool:
        expected = (self.token or "").strip()
        provided = (channel_token or "").strip()
        if expected and provided != expected:
            return False
        # After registration, channel id must match. Before/during the race with
        # Google's sync message, accept when the token matches (or no token set).
        if self.channel_id and channel_id and channel_id != self.channel_id:
            return False
        if expected:
            return True
        return bool(self.channel_id and channel_id == self.channel_id)


_state = PushChannelState()


def get_push_channel_state() -> PushChannelState:
    return _state


def resolve_webhook_address(settings: Settings | None = None) -> str | None:
    """HTTPS webhook URL Google will POST to, or None if not publicly reachable."""
    settings = settings or get_settings()
    explicit = (settings.drive_webhook_url or "").strip().rstrip("/")
    if explicit:
        return explicit
    base = (settings.public_base_url or "").strip().rstrip("/")
    if base.startswith("https://"):
        return f"{base}/api/webhooks/drive"
    return None


def ensure_channel_token(settings: Settings | None = None) -> str:
    """Stable channel token for X-Goog-Channel-Token verification."""
    settings = settings or get_settings()
    configured = (settings.drive_webhook_channel_token or "").strip()
    if configured:
        _state.token = configured
        return configured
    if not _state.token:
        # Prefer WEBHOOK_SECRET when present so ops can set one shared secret.
        fallback = (settings.webhook_secret or "").strip()
        _state.token = fallback or secrets.token_urlsafe(24)
    return _state.token


async def _get_start_page_token(client: DriveDirectClient) -> str:
    access_token = await client._get_access_token()
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.get(
            f"{_DRIVE_BASE}/changes/startPageToken",
            params={"supportsAllDrives": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code >= 400:
            raise DriveDirectError(
                f"startPageToken failed {resp.status_code}: {(resp.text or '')[:200]}"
            )
        token = resp.json().get("startPageToken")
        if not token:
            raise DriveDirectError("Drive did not return startPageToken")
        return str(token)


async def stop_channel(client: DriveDirectClient) -> None:
    """Best-effort stop of the active channel."""
    async with _state._lock:
        channel_id = _state.channel_id
        resource_id = _state.resource_id
        if not channel_id or not resource_id:
            return
        try:
            access_token = await client._get_access_token()
            async with httpx.AsyncClient(timeout=30) as http:
                await http.post(
                    f"{_DRIVE_BASE}/channels/stop",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json",
                    },
                    json={"id": channel_id, "resourceId": resource_id},
                )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to stop Drive push channel %s", channel_id)
        _state.channel_id = None
        _state.resource_id = None
        _state.expiration_ms = None


async def register_or_renew_channel(
    client: DriveDirectClient,
    *,
    settings: Settings | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """
    Register ``changes.watch`` when a public HTTPS webhook URL is configured.

    Returns a status dict. Skips (without error) when no public URL is set —
    local hybrid mode seeds the cache on startup and waits for a tunnel URL.
    """
    settings = settings or get_settings()
    address = resolve_webhook_address(settings)
    if not address:
        msg = (
            "Drive push skipped — set DRIVE_WEBHOOK_URL or HTTPS PUBLIC_BASE_URL "
            "so Google can reach POST /api/webhooks/drive"
        )
        logger.info(msg)
        _state.last_register_error = None
        return {"ok": False, "skipped": True, "reason": "no_public_webhook_url", "message": msg}

    if not address.startswith("https://"):
        msg = f"Drive webhook address must be HTTPS (got {address!r})"
        logger.warning(msg)
        _state.last_register_error = msg
        return {"ok": False, "skipped": True, "reason": "webhook_not_https", "message": msg}

    token = ensure_channel_token(settings)

    async with _state._lock:
        if not force and _state.channel_id and not _state.needs_renewal():
            return {"ok": True, "renewed": False, **_state.status()}

        # Stop previous channel before creating a new unique id.
        old_id, old_res = _state.channel_id, _state.resource_id
        if old_id and old_res:
            try:
                access_token = await client._get_access_token()
                async with httpx.AsyncClient(timeout=30) as http:
                    await http.post(
                        f"{_DRIVE_BASE}/channels/stop",
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "Content-Type": "application/json",
                        },
                        json={"id": old_id, "resourceId": old_res},
                    )
            except Exception:  # noqa: BLE001
                logger.debug("Prior channel stop failed (may already be expired)", exc_info=True)

        try:
            page_token = _state.page_token or await _get_start_page_token(client)
            channel_id = str(uuid.uuid4())
            expiration = int(time.time() * 1000) + _DEFAULT_CHANNEL_TTL_MS
            access_token = await client._get_access_token()
            body = {
                "id": channel_id,
                "type": "web_hook",
                "address": address,
                "token": token,
                "expiration": expiration,
            }
            async with httpx.AsyncClient(timeout=30) as http:
                resp = await http.post(
                    f"{_DRIVE_BASE}/changes/watch",
                    params={
                        "pageToken": page_token,
                        "supportsAllDrives": "true",
                        "includeItemsFromAllDrives": "true",
                    },
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
            if resp.status_code >= 400:
                preview = (resp.text or "")[:300]
                raise DriveDirectError(
                    f"changes.watch failed {resp.status_code}: {preview}"
                )
            data = resp.json()
            _state.channel_id = data.get("id") or channel_id
            _state.resource_id = data.get("resourceId")
            _state.page_token = page_token
            _state.expiration_ms = int(data.get("expiration") or expiration)
            _state.address = address
            _state.token = token
            _state.registered_at_mono = time.monotonic()
            _state.last_register_error = None
            logger.info(
                "Drive push channel registered id=%s resource=%s exp_ms=%s address=%s",
                _state.channel_id,
                _state.resource_id,
                _state.expiration_ms,
                address,
            )
            return {"ok": True, "renewed": True, **_state.status()}
        except Exception as exc:  # noqa: BLE001
            _state.last_register_error = str(exc)[:240]
            logger.warning("Drive push channel registration failed: %s", exc)
            return {
                "ok": False,
                "skipped": False,
                "reason": "register_failed",
                "message": str(exc)[:240],
            }


async def advance_page_token(client: DriveDirectClient) -> str | None:
    """Consume pending changes and store the new start page token."""
    try:
        access_token = await client._get_access_token()
        page_token = _state.page_token
        if not page_token:
            page_token = await _get_start_page_token(client)
            _state.page_token = page_token
            return page_token

        new_token = page_token
        async with httpx.AsyncClient(timeout=60) as http:
            while True:
                resp = await http.get(
                    f"{_DRIVE_BASE}/changes",
                    params={
                        "pageToken": new_token,
                        "pageSize": 100,
                        "supportsAllDrives": "true",
                        "includeItemsFromAllDrives": "true",
                        "fields": "nextPageToken,newStartPageToken,changes(fileId,removed)",
                    },
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if resp.status_code >= 400:
                    raise DriveDirectError(
                        f"changes.list failed {resp.status_code}: {(resp.text or '')[:200]}"
                    )
                data = resp.json()
                if data.get("nextPageToken"):
                    new_token = data["nextPageToken"]
                    continue
                if data.get("newStartPageToken"):
                    new_token = data["newStartPageToken"]
                break
        _state.page_token = new_token
        return new_token
    except Exception:  # noqa: BLE001
        logger.exception("Failed to advance Drive changes page token")
        return None
