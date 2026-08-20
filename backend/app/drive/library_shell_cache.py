"""In-process cache for GET /drive/library/shell (shared across users)."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class LibraryShellCache:
    """Process-wide shell JSON keyed by library revision."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Revision paired with payload (must never change without rebuilding payload).
    revision: str | None = None
    payload: dict[str, Any] | None = None
    # Independently cached latest SQL revision.
    latest_revision: str | None = None
    revision_cached_mono: float = 0.0

    def get(self, revision: str) -> dict[str, Any] | None:
        with self._lock:
            if self.revision == revision and self.payload is not None:
                return self.payload
            return None

    def put(self, revision: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self.revision = revision
            self.payload = payload
            self.latest_revision = revision
            self.revision_cached_mono = time.monotonic()

    def get_recent_revision(self, max_age_seconds: float) -> str | None:
        with self._lock:
            if (
                self.latest_revision
                and self.revision_cached_mono
                and time.monotonic() - self.revision_cached_mono <= max_age_seconds
            ):
                return self.latest_revision
            return None

    def put_revision(self, revision: str) -> None:
        with self._lock:
            self.latest_revision = revision
            self.revision_cached_mono = time.monotonic()

    def invalidate(self) -> None:
        with self._lock:
            if self.revision is not None:
                logger.debug("Library shell cache invalidated (was rev=%s)", self.revision)
            self.revision = None
            self.payload = None
            self.latest_revision = None
            self.revision_cached_mono = 0.0


_cache = LibraryShellCache()


def get_library_shell_cache() -> LibraryShellCache:
    return _cache


async def compute_library_revision_sql(session, *, max_age_seconds: float = 60.0) -> str:
    """Cheap revision with a short process cache to avoid DB-pool queueing.

    The frontend polls every 20 seconds. A one-minute cache keeps that polling
    off a saturated DB pool while indexing; explicit sync/pause changes
    invalidate the cache immediately in the worker handling the mutation.
    """
    from sqlalchemy import func, select

    from app.db.models import DriveFile

    cache = get_library_shell_cache()
    cached = cache.get_recent_revision(max_age_seconds)
    if cached is not None:
        return cached

    count = int(
        (await session.execute(select(func.count()).select_from(DriveFile))).scalar_one() or 0
    )
    max_synced = (await session.execute(select(func.max(DriveFile.last_synced_at)))).scalar_one()
    hist_rows = (
        await session.execute(select(DriveFile.status, func.count()).group_by(DriveFile.status))
    ).all()
    status_hist: dict[str, int] = {}
    for status, n in hist_rows:
        key = status.value if hasattr(status, "value") else str(status)
        status_hist[key] = int(n)
    hist_part = ",".join(f"{k}:{status_hist[k]}" for k in sorted(status_hist.keys()))
    revision = f"{count}:{max_synced.isoformat() if max_synced else ''}:{hist_part}"
    cache.put_revision(revision)
    return revision
