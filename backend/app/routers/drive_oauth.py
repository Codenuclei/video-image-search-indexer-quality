"""
Google Drive OAuth endpoints — stateless Authorization Code flow (no PKCE).

Routes:
  GET  /auth/google            → redirect to Google consent screen
  GET  /auth/google/callback   → exchange code, store tokens, redirect to frontend
  GET  /api/session            → connection status
  GET  /api/drive-token        → access token + API key for Google Picker
  POST /api/save-folder        → save selected folder
  POST /api/logout             → disconnect / clear stored tokens
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import DriveFile, DriveFileStatus, DriveUser
from app.db.session import get_db
from app.dependencies import get_indexing_worker
from app.drive.google_client import DriveDirectError, _do_token_refresh, resolve_folder_for_indexing
from app.runtime_settings import get_runtime_settings
from app.workers.indexer import IndexingWorker

logger = logging.getLogger(__name__)

router = APIRouter(tags=["drive-oauth"])

_SCOPES = " ".join([
    "https://www.googleapis.com/auth/drive.readonly",
    "openid",
    "email",
    "profile",
])
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
_USERINFO_URI = "https://www.googleapis.com/oauth2/v2/userinfo"


# ── OAuth flow ────────────────────────────────────────────────────────────────

@router.get("/auth/google")
async def auth_google(settings: Settings = Depends(get_settings)):
    """Start Google OAuth — redirects to Google's consent screen."""
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(
            status_code=501,
            detail="Google OAuth not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.",
        )
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": _SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    }
    return RedirectResponse(f"{_AUTH_URI}?{urlencode(params)}")


@router.get("/auth/google/callback")
async def auth_google_callback(
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    worker: IndexingWorker = Depends(get_indexing_worker),
    code: str | None = None,
    error: str | None = None,
    state: str | None = None,
):
    """Exchange OAuth code for tokens, store them, redirect to frontend."""
    frontend_url = settings.frontend_url.rstrip("/")

    if error:
        logger.warning("OAuth error from Google: %s", error)
        return RedirectResponse(f"{frontend_url}/folders?error={error}")

    if not code:
        return RedirectResponse(f"{frontend_url}/folders?error=missing_code")

    try:
        # Exchange code for tokens (plain Authorization Code — no PKCE)
        async with httpx.AsyncClient(timeout=15) as client:
            token_resp = await client.post(
                _TOKEN_URI,
                data={
                    "code": code,
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "redirect_uri": settings.google_redirect_uri,
                    "grant_type": "authorization_code",
                },
                headers={"Accept": "application/json"},
            )
            token_resp.raise_for_status()
            tokens = token_resp.json()

        access_token: str = tokens["access_token"]
        refresh_token: str | None = tokens.get("refresh_token")
        expires_in: int = tokens.get("expires_in", 3600)
        token_expiry = datetime.now(tz=timezone.utc) + timedelta(seconds=expires_in)

        # Fetch user profile
        async with httpx.AsyncClient(timeout=10) as client:
            profile_resp = await client.get(
                _USERINFO_URI,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            profile_resp.raise_for_status()
            profile = profile_resp.json()

        user_id: str = profile["id"]
        email: str = profile.get("email", "")

        # Upsert DriveUser
        existing = await session.get(DriveUser, user_id)
        if existing is None:
            existing = DriveUser(id=user_id, email=email, access_token=access_token)
            session.add(existing)

        existing.email = email
        existing.access_token = access_token
        if refresh_token:
            existing.refresh_token = refresh_token
        existing.token_expiry = token_expiry
        had_folder = bool(existing.selected_folder_id)
        await session.commit()

        logger.info("Google Drive connected for %s", email)
        if had_folder and not worker.is_running:
            background_tasks.add_task(_sync_after_folder_change, worker)
        return RedirectResponse(f"{frontend_url}/folders?connected=1")

    except Exception as exc:
        logger.exception("OAuth callback failed: %s", exc)
        safe = str(exc)[:120].replace("&", "%26")
        return RedirectResponse(f"{frontend_url}/folders?error=oauth_failed&detail={safe}")


# ── Session / status ──────────────────────────────────────────────────────────

@router.get("/api/session")
async def get_session(session: AsyncSession = Depends(get_db)):
    """Return Drive connection status."""
    user = (await session.execute(select(DriveUser).limit(1))).scalar_one_or_none()
    if user is None:
        return {"connected": False}
    return {
        "connected": True,
        "email": user.email,
        "selected_folder": (
            {"id": user.selected_folder_id, "name": user.selected_folder_name}
            if user.selected_folder_id
            else None
        ),
    }


@router.get("/api/drive-token")
async def get_drive_token(
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Return a fresh Drive access token + API key for the Google Picker widget."""
    from app.drive.google_client import _do_token_refresh

    user = (await session.execute(select(DriveUser).limit(1))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="No Google Drive account connected.")

    now = datetime.now(tz=timezone.utc)
    if user.token_expiry is None or user.token_expiry - timedelta(minutes=5) <= now:
        if not user.refresh_token:
            raise HTTPException(
                status_code=401,
                detail="Token expired — please reconnect Google Drive.",
            )
        new_token, new_expiry = await asyncio.to_thread(
            _do_token_refresh,
            user.refresh_token,
            settings.google_client_id,
            settings.google_client_secret,
        )
        user.access_token = new_token
        user.token_expiry = new_expiry
        await session.commit()

    return {
        "accessToken": user.access_token,
        "apiKey": settings.google_api_key,
        "appId": _google_app_id_from_client_id(settings.google_client_id),
    }


def _google_app_id_from_client_id(client_id: str) -> str | None:
    """Project number from OAuth client id (required by Google Picker setAppId)."""
    if not client_id:
        return None
    match = re.match(r"^(\d+)-", client_id)
    return match.group(1) if match else None


# ── Folder selection ──────────────────────────────────────────────────────────

class SaveFolderBody(BaseModel):
    id: str
    name: str


async def _requeue_folder_selection_errors(session: AsyncSession) -> int:
    """Re-queue files that failed only because no Drive folder was selected."""
    from sqlalchemy import or_

    rows = list(
        (
            await session.execute(
                select(DriveFile).where(
                    DriveFile.status == DriveFileStatus.ERROR,
                    or_(
                        DriveFile.error_message.ilike("%No Drive folder selected%"),
                        DriveFile.error_message.ilike("%No Google Drive account connected%"),
                    ),
                )
            )
        ).scalars().all()
    )
    for drive_file in rows:
        drive_file.status = DriveFileStatus.PENDING
        drive_file.error_message = None
    if rows:
        await session.flush()
        logger.info("Re-queued %d file(s) after Drive folder selection restored", len(rows))
    return len(rows)


async def _sync_after_folder_change(worker: IndexingWorker) -> None:
    """Pull the latest Drive listing after connect/folder change."""
    try:
        seen = await worker.sync_file_list()
        logger.info("Drive folder sync complete: %d file(s)", seen)
        if get_runtime_settings().auto_index_enabled:
            summary = await worker.process_pending()
            logger.info("Post folder-save auto-index: %s", summary)
    except DriveDirectError as exc:
        logger.info("Drive folder sync skipped: %s", exc)
    except Exception:  # noqa: BLE001
        logger.exception("Drive folder sync failed")


@router.post("/api/save-folder")
async def save_folder(
    body: SaveFolderBody,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    worker: IndexingWorker = Depends(get_indexing_worker),
):
    """Save the user's chosen Drive folder."""
    user = (await session.execute(select(DriveUser).limit(1))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="No Google Drive account connected.")

    access_token = user.access_token
    now = datetime.now(tz=timezone.utc)
    if user.token_expiry is None or user.token_expiry - timedelta(minutes=5) <= now:
        if not user.refresh_token:
            raise HTTPException(status_code=401, detail="Token expired — please reconnect Google Drive.")
        access_token, new_expiry = await asyncio.to_thread(
            _do_token_refresh,
            user.refresh_token,
            settings.google_client_id,
            settings.google_client_secret,
        )
        user.access_token = access_token
        user.token_expiry = new_expiry

    try:
        folder_id, folder_name = await resolve_folder_for_indexing(
            access_token, body.id, body.name
        )
    except DriveDirectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    user.selected_folder_id = folder_id
    user.selected_folder_name = folder_name
    requeued = await _requeue_folder_selection_errors(session)
    await session.commit()
    logger.info("Drive folder saved: %s (%s), requeued=%d", folder_name, folder_id, requeued)
    if not worker.is_running:
        background_tasks.add_task(_sync_after_folder_change, worker)
    return {
        "ok": True,
        "folder": {"id": folder_id, "name": folder_name},
        "requeued": requeued,
    }


# ── Logout ────────────────────────────────────────────────────────────────────

@router.post("/api/logout")
async def logout(session: AsyncSession = Depends(get_db)):
    """Disconnect Google Drive (delete stored tokens)."""
    user = (await session.execute(select(DriveUser).limit(1))).scalar_one_or_none()
    if user is not None:
        await session.delete(user)
        await session.commit()
        logger.info("Drive account disconnected")
    return {"ok": True}
