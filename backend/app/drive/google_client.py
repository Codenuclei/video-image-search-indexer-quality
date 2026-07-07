"""
Direct Google Drive API client — replaces the external Node.js Drive Connector.

Implements the same interface as DriveConnectorClient so the rest of the codebase
(IndexingWorker, pipelines, preview endpoint) does not need changes.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import select

from app.drive.schemas import ConnectorFile, ConnectorFolder, ConnectorFolderListing

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
    from app.config import Settings

logger = logging.getLogger(__name__)

FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_EXPORT_MIME: dict[str, str] = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.drawing": "image/png",
}
MAX_ENTRIES = 5_000
_DRIVE_BASE = "https://www.googleapis.com/drive/v3"


class DriveDirectError(RuntimeError):
    """Raised when a Drive API call fails."""


def _do_token_refresh(refresh_token: str, client_id: str, client_secret: str) -> tuple[str, datetime | None]:
    """Synchronous token refresh — run in a thread."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
    )
    creds.refresh(Request())
    expiry = creds.expiry.replace(tzinfo=timezone.utc) if creds.expiry else None
    return creds.token, expiry  # type: ignore[return-value]


class DriveDirectClient:
    """
    Drop-in replacement for DriveConnectorClient.
    Talks directly to Google Drive API using tokens stored in the DriveUser table.
    """

    def __init__(
        self,
        session_factory: "async_sessionmaker[AsyncSession]",
        settings: "Settings",
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings

    # ── token management ──────────────────────────────────────────────────────

    async def _get_auth(self) -> tuple[str, str]:
        """Return (access_token, folder_id), refreshing the token if needed."""
        from app.db.models import DriveUser

        async with self._session_factory() as session:
            user: DriveUser | None = (
                await session.execute(select(DriveUser).limit(1))
            ).scalar_one_or_none()

            if user is None:
                raise DriveDirectError(
                    "No Google Drive account connected. "
                    "Open the DFI frontend → Folders and click 'Connect Google Drive'."
                )
            if not user.selected_folder_id:
                raise DriveDirectError(
                    "No Drive folder selected. "
                    "Open the DFI frontend → Folders and choose a folder."
                )

            now = datetime.now(tz=timezone.utc)
            needs_refresh = user.token_expiry is None or (
                user.token_expiry - timedelta(minutes=5) <= now
            )

            if needs_refresh:
                if not user.refresh_token:
                    raise DriveDirectError(
                        "Access token expired and no refresh token is stored. "
                        "Please reconnect Google Drive."
                    )
                logger.info("Refreshing Drive access token for %s", user.email)
                new_token, new_expiry = await asyncio.to_thread(
                    _do_token_refresh,
                    user.refresh_token,
                    self._settings.google_client_id,
                    self._settings.google_client_secret,
                )
                user.access_token = new_token
                user.token_expiry = new_expiry
                await session.commit()

            return user.access_token, user.selected_folder_id

    # ── folder listing ────────────────────────────────────────────────────────

    async def list_folder_files(self) -> ConnectorFolderListing:
        """Recursively list all files inside the connected folder."""
        access_token, root_folder_id = await self._get_auth()

        # Look up the folder name
        async with httpx.AsyncClient(timeout=30) as client:
            meta_resp = await client.get(
                f"{_DRIVE_BASE}/files/{root_folder_id}",
                params={"fields": "id,name", "supportsAllDrives": "true"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if meta_resp.status_code == 404:
                raise DriveDirectError(
                    "Selected Drive folder not found or no longer accessible. "
                    "Open Folders and choose the folder again."
                )
            _raise_for_status(meta_resp)
            meta = meta_resp.json()

        root_folder = ConnectorFolder(id=root_folder_id, name=meta["name"])

        results: list[ConnectorFile] = []
        truncated = False

        queue: list[tuple[str, list[str]]] = [(root_folder_id, [])]

        async with httpx.AsyncClient(timeout=60) as client:
            while queue:
                folder_id, path_parts = queue.pop(0)
                children = await _list_children(client, folder_id, access_token)

                for child in children:
                    if len(results) >= MAX_ENTRIES:
                        truncated = True
                        break
                    is_folder = child["mimeType"] == FOLDER_MIME
                    child_path = "/".join([*path_parts, child["name"]])
                    results.append(
                        ConnectorFile.model_validate(
                            {
                                "id": child["id"],
                                "name": child["name"],
                                "mimeType": child["mimeType"],
                                "isFolder": is_folder,
                                "size": child.get("size"),
                                "modifiedTime": child.get("modifiedTime"),
                                "parentId": folder_id,
                                "path": child_path,
                            }
                        )
                    )
                    if is_folder:
                        queue.append((child["id"], [*path_parts, child["name"]]))

                if truncated:
                    break

        return ConnectorFolderListing(folder=root_folder, files=results, truncated=truncated)

    # ── file streaming ────────────────────────────────────────────────────────

    @asynccontextmanager
    async def stream_file_content(self, file_id: str) -> AsyncIterator[httpx.Response]:
        """
        Stream a file's bytes from Drive.
        Google-native files (Docs/Sheets/Slides) are exported to a plain format.
        The yielded httpx.Response has .aiter_bytes() just like DriveConnectorClient.
        """
        access_token, _ = await self._get_auth()
        headers = {"Authorization": f"Bearer {access_token}"}

        async with httpx.AsyncClient(timeout=self._settings.drive_connector_timeout_seconds) as client:
            # Fetch metadata to check if it's a Google-native format
            meta_resp = await client.get(
                f"{_DRIVE_BASE}/files/{file_id}",
                params={"fields": "id,name,mimeType", "supportsAllDrives": "true"},
                headers=headers,
            )
            _raise_for_status(meta_resp)
            mime = meta_resp.json().get("mimeType", "")

            export_mime = GOOGLE_EXPORT_MIME.get(mime)
            if export_mime:
                url = f"{_DRIVE_BASE}/files/{file_id}/export"
                params: dict[str, str] = {"mimeType": export_mime, "supportsAllDrives": "true"}
            else:
                url = f"{_DRIVE_BASE}/files/{file_id}"
                params = {"alt": "media", "supportsAllDrives": "true"}

            async with client.stream("GET", url, params=params, headers=headers) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    raise DriveDirectError(
                        f"Drive API error {response.status_code} for file {file_id}: "
                        f"{body[:300].decode(errors='replace')}"
                    )
                yield response


# ── helpers ───────────────────────────────────────────────────────────────────

async def _list_children(
    client: httpx.AsyncClient,
    folder_id: str,
    access_token: str,
) -> list[dict]:
    """Fetch all children of a folder, handling pagination."""
    results: list[dict] = []
    page_token: str | None = None

    while True:
        params: dict[str, str | int] = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime)",
            "pageSize": 200,
            "spaces": "drive",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if page_token:
            params["pageToken"] = page_token

        resp = await client.get(
            f"{_DRIVE_BASE}/files",
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        _raise_for_status(resp)
        data = resp.json()
        results.extend(data.get("files", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return results


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code >= 400:
        preview = (response.text or "")[:300]
        raise DriveDirectError(
            f"Drive API returned {response.status_code}: {preview}"
        )
