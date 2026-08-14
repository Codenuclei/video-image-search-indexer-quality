from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.routers.carousel_script import (
    CarouselGenerateRequest,
    CarouselPrerunRequest,
    PipelineThemeSlice,
    TimedPick,
    _carousel_selection_hash,
)
from app.routers.drive import preview_drive_file
from scripts.cache_audit_cleanup import (
    DELETE_POLICY,
    CacheDbState,
    classify_cache_path,
    duplicate_case_groups,
)
from starlette.requests import Request


def _request(range_header: str | None = None) -> Request:
    headers = []
    if range_header:
        headers.append((b"range", range_header.encode()))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


@pytest.mark.asyncio
async def test_uncached_drive_preview_forwards_range_and_streams_206(tmp_path) -> None:
    drive_file = SimpleNamespace(
        id="drive-1",
        name="clip.mp4",
        mime_type="video/mp4",
        source="drive",
    )
    session = AsyncMock()
    session.get.return_value = drive_file
    upstream = MagicMock()
    upstream.status_code = 206
    upstream.headers = {
        "content-range": "bytes 10-12/100",
        "content-length": "3",
        "accept-ranges": "bytes",
    }

    async def chunks():
        yield b"abc"

    upstream.aiter_raw = chunks

    class StreamContext:
        async def __aenter__(self):
            return upstream

        async def __aexit__(self, *_args):
            return None

    client = MagicMock()
    client.stream_file_content.return_value = StreamContext()
    settings = SimpleNamespace(video_cache_dir=str(tmp_path))

    with (
        patch("app.config.get_settings", return_value=settings),
        patch("app.routers.drive.DriveDirectClient", return_value=client),
    ):
        response = await preview_drive_file("drive-1", _request("bytes=10-12"), session)

    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 10-12/100"
    assert response.headers["accept-ranges"] == "bytes"
    assert b"".join([chunk async for chunk in response.body_iterator]) == b"abc"
    client.stream_file_content.assert_called_once_with(
        "drive-1", range_header="bytes=10-12"
    )
    assert response.background is not None
    await response.background()


def test_cache_cleanup_refuses_protected_sources_and_active_rows(tmp_path) -> None:
    path = tmp_path / "same.mp4"
    path.write_bytes(b"x")

    def state(
        source: str, status: str = "processed", carousel: str = "idle", media=True
    ):
        return CacheDbState("same", source, status, carousel, media)

    assert classify_cache_path(path, state("upload")).policy == "keep_upload"
    assert classify_cache_path(path, state("youtube")).policy == "keep_youtube"
    assert classify_cache_path(path, None).policy == "keep_unknown"
    assert (
        classify_cache_path(path, state("drive", "processing")).policy == "keep_active"
    )
    assert (
        classify_cache_path(path, state("drive", carousel="processing")).policy
        == "keep_active"
    )
    assert classify_cache_path(path, state("drive", "error", media=False)).policy == (
        "keep_incomplete_drive"
    )
    assert classify_cache_path(path, state("drive")).policy == DELETE_POLICY


def test_duplicate_case_paths_are_reported(tmp_path) -> None:
    upper = tmp_path / "clip.MOV"
    lower = tmp_path / "clip.mov"
    assert duplicate_case_groups([upper, lower]) == [[upper, lower]]


def test_run_config_schema_and_cache_partitioning() -> None:
    body = CarouselPrerunRequest(
        drive_file_ids=["v1"],
        llm_provider="claude",
        llm_model="claude-test",
    )
    assert body.llm_provider == "claude"
    assert body.llm_model == "claude-test"

    request = CarouselGenerateRequest(
        drive_file_id="v1",
        hooks=[TimedPick(text="A useful selected hook")],
        themes=[
            PipelineThemeSlice(theme_id="t1", title="Theme", start_sec=1, end_sec=4)
        ],
        intent="Explain the point",
        force=True,
    )
    common = {
        "drive_file_id": request.drive_file_id,
        "hooks": request.hooks,
        "topics": request.topics,
        "themes": request.themes,
        "intent": request.intent,
        "min_slides": request.min_slides,
        "max_slides": request.max_slides,
        "select_images": request.select_images,
        "polish_copy": request.polish_copy,
    }
    claude = _carousel_selection_hash(**common, llm_cache_id="claude:model-a")
    other_model = _carousel_selection_hash(**common, llm_cache_id="claude:model-b")
    other_theme = _carousel_selection_hash(
        **{**common, "themes": [PipelineThemeSlice(theme_id="t2", title="Other")]},
        llm_cache_id="claude:model-a",
    )
    assert request.force is True
    assert claude != other_model
    assert claude != other_theme
