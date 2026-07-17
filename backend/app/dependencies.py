from __future__ import annotations

from app.config import get_settings
from app.db.session import get_session_factory
from app.workers.indexer import IndexingWorker

_worker: IndexingWorker | None = None


def get_drive_client():
    """Direct Google Drive client used by prod (no external Drive Connector)."""
    from app.drive.google_client import DriveDirectClient

    settings = get_settings()
    return DriveDirectClient(session_factory=get_session_factory(), settings=settings)


def get_indexing_worker() -> IndexingWorker:
    """Process-wide singleton — indexing must run one file at a time, never concurrently."""
    global _worker
    if _worker is None:
        settings = get_settings()
        session_factory = get_session_factory()
        client = get_drive_client()
        _worker = IndexingWorker(session_factory=session_factory, client=client)
    return _worker
