"""Per-browser Drive session tokens.

Studio historically exposed a single global ``DriveUser`` to every visitor
(``select(DriveUser).limit(1)``). That leaked the connected email and access
token to anyone with the Studio URL.

After OAuth, the callback issues a signed token bound to that Google user id.
``/api/session`` and ``/api/drive-token`` only report/use Drive when the browser
presents a matching token. Background indexing still reads ``DriveUser`` from
Postgres without a browser token.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from fastapi import Request

from app.config import Settings

COOKIE_NAME = "carousel_drive_session"
HEADER_NAME = "x-carousel-drive-session"
# Query param used once on OAuth return; frontend stores it then strips the URL.
QUERY_PARAM = "ds"
MAX_AGE_SEC = 90 * 24 * 60 * 60


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _secret_bytes(settings: Settings) -> bytes:
    material = (
        (settings.carousel_google_client_secret or "").strip()
        or (settings.google_client_secret or "").strip()
        or (settings.webhook_secret or "").strip()
        or "dev-drive-browser-session"
    )
    return hashlib.sha256(f"carousel-drive-browser:{material}".encode("utf-8")).digest()


def seal_drive_browser_session(user_id: str, settings: Settings, *, max_age_sec: int = MAX_AGE_SEC) -> str:
    uid = (user_id or "").strip()
    if not uid:
        raise ValueError("user_id required")
    payload = {
        "uid": uid,
        "exp": int(time.time()) + int(max_age_sec),
    }
    body = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = _b64url_encode(
        hmac.new(_secret_bytes(settings), body.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{body}.{sig}"


def unseal_drive_browser_session(token: str | None, settings: Settings) -> str | None:
    raw = (token or "").strip()
    if not raw or "." not in raw:
        return None
    body, sig = raw.split(".", 1)
    if not body or not sig:
        return None
    expected = _b64url_encode(
        hmac.new(_secret_bytes(settings), body.encode("ascii"), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        payload: dict[str, Any] = json.loads(_b64url_decode(body).decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    uid = str(payload.get("uid") or "").strip()
    exp = payload.get("exp")
    if not uid or not isinstance(exp, int):
        return None
    if exp < int(time.time()):
        return None
    return uid


def read_drive_browser_user_id(request: Request, settings: Settings) -> str | None:
    """Accept header (preferred) or cookie (direct API calls)."""
    header = (request.headers.get(HEADER_NAME) or "").strip()
    if header:
        return unseal_drive_browser_session(header, settings)
    cookie = (request.cookies.get(COOKIE_NAME) or "").strip()
    if cookie:
        return unseal_drive_browser_session(cookie, settings)
    return None
