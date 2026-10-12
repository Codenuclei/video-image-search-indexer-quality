"""
Verify the Next.js sealed app session token (dfi_app_session).

Format matches frontend/src/lib/session-cookie.ts:
  base64url(JSON{email,isAdmin,exp}) + '.' + base64url(HMAC-SHA256(body, secret))

Requires APP_SESSION_SECRET (>= 16 chars) on frontend and backend. No public-client-id fallback.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings

logger = logging.getLogger(__name__)

ALLOWED_EMAIL_DOMAIN = "mastersunion.org"
APP_SESSION_MAX_AGE_SEC = 90 * 24 * 60 * 60
MIN_SECRET_LEN = 16

_missing_warned = False
_bearer = HTTPBearer(auto_error=False)


def configured_session_secret() -> str | None:
    """Return the configured secret, or None if missing/too short (fail closed)."""
    global _missing_warned
    settings = get_settings()
    explicit = (settings.app_session_secret or "").strip()
    if len(explicit) >= MIN_SECRET_LEN:
        return explicit
    if not _missing_warned:
        _missing_warned = True
        logger.warning(
            "APP_SESSION_SECRET is not configured (missing or shorter than %s chars). "
            "Session token verification is disabled until it is set identically on "
            "frontend and backend services.",
            MIN_SECRET_LEN,
        )
    return None


def session_secret() -> str:
    """Return the configured secret or raise (used by seal/tests; never falls back)."""
    secret = configured_session_secret()
    if not secret:
        raise RuntimeError("APP_SESSION_SECRET is not configured on the server")
    return secret


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data + pad)


def seal_app_session(
    email: str,
    *,
    is_admin: bool = False,
    exp: int | None = None,
) -> str:
    """Mint a token (tests / tooling). Same format as the Next.js sealer."""
    payload = {
        "email": email.strip().lower(),
        "isAdmin": bool(is_admin),
        "exp": int(exp if exp is not None else time.time() + APP_SESSION_MAX_AGE_SEC),
    }
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(session_secret().encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    return f"{body}.{_b64url_encode(sig)}"


def verify_app_session_token(token: str | None) -> str | None:
    """
    Return the lowercased email if the token is valid, else None.

    Returns None (fail closed) when APP_SESSION_SECRET is not configured.
    """
    secret = configured_session_secret()
    if secret is None:
        return None
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    if not body or not sig:
        return None
    try:
        expected = hmac.new(
            secret.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        actual = _b64url_decode(sig)
    except Exception:
        return None
    if not hmac.compare_digest(expected, actual):
        return None
    try:
        raw = _b64url_decode(body).decode("utf-8")
        parsed: dict[str, Any] = json.loads(raw)
    except Exception:
        return None
    email = str(parsed.get("email") or "").strip().lower()
    if not email or not email.endswith(f"@{ALLOWED_EMAIL_DOMAIN}"):
        return None
    exp = parsed.get("exp")
    if not isinstance(exp, (int, float)) or float(exp) < time.time():
        return None
    if "isAdmin" not in parsed or not isinstance(parsed["isAdmin"], bool):
        return None
    return email


async def current_user_email(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """FastAPI dependency: require a valid Authorization: Bearer session token."""
    if configured_session_secret() is None:
        raise HTTPException(
            status_code=503,
            detail="APP_SESSION_SECRET is not configured on the server",
        )
    if creds is None or (creds.scheme or "").lower() != "bearer" or not (creds.credentials or "").strip():
        raise HTTPException(status_code=401, detail="Authorization Bearer token required")
    email = verify_app_session_token(creds.credentials.strip())
    if not email:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return email
