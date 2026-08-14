"""Drive OAuth disconnect must not block permanent-library / cached media paths."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.db.models import DriveFileStatus
from app.drive.google_client import DriveDirectError
from app.routers.carousel_script import (
    CarouselPrioritizeRequest,
    prioritize_drive_videos_for_carousel,
)
from app.routers.drive import _live_drive_http_status, download_drive_file
from app.routers.media import _extract_frame_on_demand


def test_live_drive_http_status_maps_reconnect_to_401() -> None:
    assert (
        _live_drive_http_status(
            DriveDirectError("No Google Drive account connected. Open the DFI frontend")
        )
        == 401
    )
    assert _live_drive_http_status(DriveDirectError("upstream timeout")) == 502


@pytest.mark.asyncio
async def test_frame_on_demand_uses_local_drive_cache_without_oauth(tmp_path) -> None:
    cache = tmp_path / "videos"
    cache.mkdir()
    media = cache / "drive-cached.mp4"
    media.write_bytes(b"fake-mp4")
    out = tmp_path / "frame.jpg"

    drive_file = SimpleNamespace(
        id="drive-cached",
        name="clip.mp4",
        mime_type="video/mp4",
        source="drive",
    )
    session = AsyncMock()
    session.get.return_value = drive_file
    settings = SimpleNamespace(
        video_cache_dir=str(cache),
        google_client_id="x",
        google_client_secret="y",
    )

    with patch("app.routers.media.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0, stderr=b"")
        ok = await _extract_frame_on_demand(
            "drive-cached", 1.5, out, settings, session
        )

    assert ok is True
    run.assert_called_once()
    cmd = run.call_args.args[0]
    assert str(media) in cmd
    # Must not fall through to DriveUser / live stream.
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_frame_on_demand_skips_oauth_when_uncached_and_disconnected(
    tmp_path,
) -> None:
    cache = tmp_path / "videos"
    cache.mkdir()
    drive_file = SimpleNamespace(
        id="drive-miss",
        name="clip.mp4",
        mime_type="video/mp4",
        source="drive",
    )
    session = AsyncMock()
    session.get.return_value = drive_file
    session.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: None)
    )
    settings = SimpleNamespace(
        video_cache_dir=str(cache),
        google_client_id="x",
        google_client_secret="y",
    )

    with patch("app.routers.media.subprocess.run") as run:
        ok = await _extract_frame_on_demand(
            "drive-miss", 1.5, tmp_path / "frame.jpg", settings, session
        )

    assert ok is False
    run.assert_not_called()


@pytest.mark.asyncio
async def test_download_drive_file_serves_local_cache_without_oauth(tmp_path) -> None:
    cache = tmp_path / "videos"
    cache.mkdir()
    media = cache / "drive-dl.mp4"
    media.write_bytes(b"cached-bytes")

    drive_file = SimpleNamespace(
        id="drive-dl",
        name="clip.mp4",
        mime_type="video/mp4",
        source="drive",
    )
    session = AsyncMock()
    session.get.return_value = drive_file
    settings = SimpleNamespace(video_cache_dir=str(cache))

    with (
        patch("app.config.get_settings", return_value=settings),
        patch("app.routers.drive.DriveDirectClient") as client_cls,
    ):
        response = await download_drive_file("drive-dl", session)

    assert Path(response.path) == media
    client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_prioritize_allows_processed_when_drive_disconnected() -> None:
    processed = SimpleNamespace(
        id="proc-1",
        name="done.mp4",
        mime_type="video/mp4",
        status=DriveFileStatus.PROCESSED,
        source="drive",
        error_message=None,
    )
    pending = SimpleNamespace(
        id="pend-1",
        name="need.mp4",
        mime_type="video/mp4",
        status=DriveFileStatus.PENDING,
        source="drive",
        error_message=None,
    )

    async def _get(_model, fid: str):
        return {"proc-1": processed, "pend-1": pending}.get(fid)

    session = AsyncMock()
    session.get.side_effect = _get
    session.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: None)
    )
    session.commit = AsyncMock()
    settings = SimpleNamespace(video_cache_dir="/tmp/no-cache-here")
    bg = MagicMock()

    with (
        patch("app.routers.carousel_script.get_settings", return_value=settings),
        patch(
            "app.video.youtube_cache.video_cache_path",
            return_value=SimpleNamespace(is_file=lambda: False),
        ),
        patch(
            "app.routers.carousel_script._load_video_cues",
            new=AsyncMock(return_value=(processed, [])),
        ),
    ):
        result = await prioritize_drive_videos_for_carousel(
            CarouselPrioritizeRequest(drive_file_ids=["proc-1", "pend-1"]),
            bg,
            session,
        )

    by_id = {it["drive_file_id"]: it for it in result["items"]}
    assert by_id["proc-1"]["ok"] is True
    assert by_id["proc-1"]["queued"] is False
    assert by_id["proc-1"]["cue_count"] == 0
    assert by_id["proc-1"]["has_captions"] is False
    assert by_id["pend-1"]["ok"] is False
    assert by_id["pend-1"]["error"] == "drive_not_connected"
    assert result["queued"] == 0
    bg.add_task.assert_not_called()


@pytest.mark.asyncio
async def test_download_uncached_drive_without_oauth_raises_reconnect() -> None:
    drive_file = SimpleNamespace(
        id="drive-live",
        name="clip.mp4",
        mime_type="video/mp4",
        source="drive",
    )
    session = AsyncMock()
    session.get.return_value = drive_file
    settings = SimpleNamespace(video_cache_dir="/tmp/empty-cache-dir-xyz")

    client = MagicMock()

    async def _fail(*_a, **_k):
        raise DriveDirectError(
            "No Google Drive account connected. Open the DFI frontend → Folders "
            "and click 'Connect Google Drive'."
        )

    with (
        patch("app.config.get_settings", return_value=settings),
        patch("app.db.session.get_session_factory", return_value=MagicMock()),
        patch("app.routers.drive.DriveDirectClient", return_value=client),
        patch("app.routers.drive.download_to_memory", side_effect=_fail),
    ):
        with pytest.raises(HTTPException) as ei:
            await download_drive_file("drive-live", session)

    assert ei.value.status_code == 401
    assert "no google drive account" in str(ei.value.detail).lower()
