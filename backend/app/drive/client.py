from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import Settings, get_settings
from app.drive.schemas import ConnectorFolderListing

logger = logging.getLogger(__name__)


class DriveConnectorError(RuntimeError):
    """Raised when the existing Drive Connector API returns an unexpected response."""


class DriveConnectorClient:
    """
    Thin HTTP client for the existing Node/Express Drive Connector
    (see `drive connector/README.md`). Authenticates with the auto-provisioned
    connector API key via `Authorization: Bearer <key>` and never re-implements
    Google OAuth or Drive API access directly.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        if not self._settings.drive_connector_api_key:
            logger.warning(
                "DRIVE_CONNECTOR_API_KEY is not set — calls to the Drive Connector will fail with 401."
            )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._settings.drive_connector_api_key}"}

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    )
    async def list_folder_files(self) -> ConnectorFolderListing:
        """Live folder listing for indexer sync (never served from UI cache)."""
        url = f"{self._settings.drive_connector_base_url}/api/folder/files?fresh=1"
        async with httpx.AsyncClient(timeout=self._settings.drive_connector_timeout_seconds) as client:
            response = await client.get(url, headers=self._headers())
            self._raise_for_status(response)
            return ConnectorFolderListing.model_validate(response.json())

    @asynccontextmanager
    async def stream_file_content(self, file_id: str) -> AsyncIterator[httpx.Response]:
        """
        Streams a file's content without loading it fully into memory. Caller should
        iterate `response.aiter_bytes()` and write chunks straight to disk/decoder.
        """
        url = f"{self._settings.drive_connector_base_url}/api/files/{file_id}/content"
        async with httpx.AsyncClient(timeout=self._settings.drive_connector_timeout_seconds) as client:
            async with client.stream("GET", url, headers=self._headers()) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    self._raise_for_status(response, body_preview=body[:300].decode(errors="replace"))
                yield response

    @staticmethod
    def _raise_for_status(response: httpx.Response, body_preview: str | None = None) -> None:
        if response.status_code == 401:
            raise DriveConnectorError(
                "Drive Connector rejected the request (401). Check DRIVE_CONNECTOR_API_KEY."
            )
        if response.status_code >= 400:
            preview = body_preview if body_preview is not None else response.text[:300]
            raise DriveConnectorError(f"Drive Connector request failed: {response.status_code} {preview}")
