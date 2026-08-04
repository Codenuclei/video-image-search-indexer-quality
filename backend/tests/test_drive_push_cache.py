"""Tests for Drive push webhook + in-memory file-list cache."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.drive.file_list_cache import DriveFileListCache, get_file_list_cache
from app.drive.schemas import ConnectorFile, ConnectorFolder, ConnectorFolderListing
from app.drive.push_channels import PushChannelState, ensure_channel_token
from app.config import get_settings


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    cache = get_file_list_cache()
    cache.folder = None
    cache.files = []
    cache.truncated = False
    cache.cached_at = None
    cache.cached_mono = 0.0
    cache.source = "empty"
    cache.last_error = None
    cache.refresh_in_flight = False
    yield


@pytest.mark.asyncio
async def test_file_list_cache_snapshot_from_memory():
    cache = get_file_list_cache()
    listing = ConnectorFolderListing(
        folder=ConnectorFolder(id="root", name="Root"),
        files=[
            ConnectorFile.model_validate(
                {
                    "id": "f1",
                    "name": "a.jpg",
                    "mimeType": "image/jpeg",
                    "isFolder": False,
                    "size": "10",
                    "modifiedTime": "2024-01-01T00:00:00Z",
                    "parentId": "root",
                    "path": "a.jpg",
                }
            )
        ],
        truncated=False,
    )
    await cache.replace(listing, source="test")
    snap = cache.snapshot()
    assert snap["from_memory"] is True
    assert snap["count"] == 1
    assert snap["source"] == "test"
    assert snap["files"][0]["id"] == "f1"
    assert snap["age_seconds"] is not None


def test_push_channel_token_verification():
    state = PushChannelState()
    state.channel_id = "ch-1"
    state.token = "secret-token"
    assert state.verify_notification(channel_id="ch-1", channel_token="secret-token")
    assert not state.verify_notification(channel_id="ch-1", channel_token="wrong")
    assert not state.verify_notification(channel_id="other", channel_token="secret-token")


def test_google_drive_push_sync_and_change(monkeypatch):
    """Simulated Google sync + change POSTs return quickly and schedule refresh."""
    monkeypatch.setenv("DRIVE_WEBHOOK_ALLOW_UNVERIFIED", "true")
    get_settings.cache_clear()

    scheduled: list[str] = []

    async def _fake_refresh(*, source: str, sync_db: bool = True, process_pending=None):
        scheduled.append(source)
        return {"ok": True, "source": source}

    monkeypatch.setattr(
        "app.routers.webhooks.refresh_drive_file_list_cache",
        _fake_refresh,
    )

    # Import app after env patch so settings pick up allow_unverified.
    from app.main import app

    client = TestClient(app)

    # sync handshake — no background refresh
    r = client.post(
        "/api/webhooks/drive",
        headers={
            "X-Goog-Channel-ID": "test-channel",
            "X-Goog-Resource-State": "sync",
            "X-Goog-Message-Number": "1",
            "X-Goog-Resource-ID": "res-1",
        },
    )
    assert r.status_code == 204
    assert scheduled == []

    r2 = client.post(
        "/api/webhooks/drive",
        headers={
            "X-Goog-Channel-ID": "test-channel",
            "X-Goog-Resource-State": "change",
            "X-Goog-Message-Number": "2",
            "X-Goog-Resource-ID": "res-1",
        },
    )
    assert r2.status_code == 204
    # Background task may run after response; give TestClient a moment.
    import time

    for _ in range(20):
        if scheduled:
            break
        time.sleep(0.05)
    assert any(s.startswith("google_push:") for s in scheduled)

    get_settings.cache_clear()
