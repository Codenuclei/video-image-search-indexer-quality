"""App login: verify Google ID token and resolve Admin allowlist from Postgres."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import AppAdmin
from app.db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["app-auth"])

ALLOWED_DOMAIN = "mastersunion.org"


class GoogleIdTokenBody(BaseModel):
    credential: str = Field(min_length=20)


class AppAuthSessionOut(BaseModel):
    email: str
    is_admin: bool


async def is_app_admin_email(session: AsyncSession, email: str) -> bool:
    key = (email or "").strip().lower()
    if not key:
        return False
    row = await session.scalar(
        select(AppAdmin.email).where(func.lower(AppAdmin.email) == key).limit(1)
    )
    return row is not None


def _verify_google_id_token(credential: str, settings: Settings) -> dict[str, Any]:
    client_id = (settings.google_client_id or "").strip()
    if not client_id:
        raise HTTPException(status_code=503, detail="Google client id is not configured")
    try:
        payload = google_id_token.verify_oauth2_token(
            credential,
            google_requests.Request(),
            client_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Google ID token verify failed: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid Google sign-in token") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=401, detail="Invalid Google sign-in token")
    return payload


@router.post("/auth/google-id-token", response_model=AppAuthSessionOut)
async def exchange_google_id_token(
    body: GoogleIdTokenBody,
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AppAuthSessionOut:
    """Verify GIS credential; return email + whether it is in ``app_admins``."""
    payload = _verify_google_id_token(body.credential, settings)
    email = str(payload.get("email") or "").strip().lower()
    hd = str(payload.get("hd") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="Google token missing email")
    if hd != ALLOWED_DOMAIN and not email.endswith(f"@{ALLOWED_DOMAIN}"):
        raise HTTPException(
            status_code=403,
            detail=f"Access is restricted to @{ALLOWED_DOMAIN} accounts only.",
        )
    admin = await is_app_admin_email(session, email)
    return AppAuthSessionOut(email=email, is_admin=admin)


@router.get("/auth/is-admin")
async def check_is_admin(
    email: str,
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Lookup-only helper (email must already be session-authenticated on the frontend)."""
    key = (email or "").strip().lower()
    if not key or "@" not in key:
        raise HTTPException(status_code=400, detail="email required")
    return {"email": key, "is_admin": await is_app_admin_email(session, key)}
