"""In-memory Drive file-list cache for fast poll responses.

The frontend keeps polling existing endpoints (`/drive/files`, etc.). This
cache backs `GET /api/cache/files` and is refreshed by Drive push notifications
(or a rare fallback sync), so the backend no longer tree-walks Drive on a timer.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.drive.schemas import ConnectorFile, ConnectorFolder, ConnectorFolderListing

logger = logging.getLogger(__name__)


@dataclass
class DriveFileListCache:
    """Process-wide snapshot of the connected Drive folder listing."""

    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    folder: ConnectorFolder | None = None
    files: list[ConnectorFile] = field(default_factory=list)
    truncated: bool = False
    cached_at: datetime | None = None
    cached_mono: float = 0.0
    source: str = "empty"  # empty | startup | webhook | fallback | manual
    last_error: str | None = None
    refresh_in_flight: bool = False

    def snapshot(self) -> dict[str, Any]:
        """Serialize for JSON responses (no I/O)."""
        files_out = [
            f.model_dump(by_alias=True, mode="json") for f in self.files
        ]
        return {
            "folder": (
                self.folder.model_dump(mode="json") if self.folder else None
            ),
            "files": files_out,
            "truncated": self.truncated,
            "count": len(self.files),
            "file_count": sum(1 for f in self.files if not f.is_folder),
            "folder_count": sum(1 for f in self.files if f.is_folder),
            "cached_at": self.cached_at.isoformat() if self.cached_at else None,
            "age_seconds": (
                round(time.monotonic() - self.cached_mono, 3)
                if self.cached_mono
                else None
            ),
            "source": self.source,
            "from_memory": True,
            "last_error": self.last_error,
            "refresh_in_flight": self.refresh_in_flight,
        }

    def is_warm(self) -> bool:
        return self.cached_at is not None and self.folder is not None

    async def replace(
        self,
        listing: ConnectorFolderListing,
        *,
        source: str,
    ) -> None:
        async with self._lock:
            self.folder = listing.folder
            self.files = list(listing.files)
            self.truncated = listing.truncated
            self.cached_at = datetime.now(tz=timezone.utc)
            self.cached_mono = time.monotonic()
            self.source = source
            self.last_error = None
            self.refresh_in_flight = False
            logger.info(
                "Drive file-list cache updated source=%s files=%d truncated=%s",
                source,
                len(self.files),
                listing.truncated,
            )

    async def mark_refresh_start(self) -> bool:
        """Return False if a refresh is already running (caller should skip)."""
        async with self._lock:
            if self.refresh_in_flight:
                return False
            self.refresh_in_flight = True
            return True

    async def mark_refresh_done(self, *, error: str | None = None) -> None:
        async with self._lock:
            self.refresh_in_flight = False
            if error:
                self.last_error = error[:240]


_cache = DriveFileListCache()


def get_file_list_cache() -> DriveFileListCache:
    return _cache
