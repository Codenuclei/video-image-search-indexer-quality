"""HMAC video pull: no Drive URLs, Range 206 for RunPod parallel GET."""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.config import Settings
from app.faces.video_pull import sign_face_video_pull
from app.routers.face_gpu_pull import _parse_byte_range, pull_face_gpu_video, range_file_response


def _request(range_header: str | None = None) -> Request:
    headers = []
    if range_header:
        headers.append((b"range", range_header.encode("ascii")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/internal/face-gpu-video/file-1",
        "raw_path": b"/internal/face-gpu-video/file-1",
        "query_string": b"",
        "headers": headers,
        "client": ("test", 0),
        "server": ("test", 80),
    }
    return Request(scope)


async def _body(response) -> bytes:
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else bytes(chunk))
    return b"".join(chunks)


def test_parse_byte_range_first_byte_and_suffix() -> None:
    assert _parse_byte_range(None, 100) is None
    assert _parse_byte_range("bytes=0-0", 100) == (0, 0)
    assert _parse_byte_range("bytes=10-19", 100) == (10, 19)
    assert _parse_byte_range("bytes=90-", 100) == (90, 99)
    assert _parse_byte_range("bytes=-10", 100) == (90, 99)


def test_parse_byte_range_rejects_past_eof() -> None:
    with pytest.raises(HTTPException) as exc:
        _parse_byte_range("bytes=100-101", 100)
    assert exc.value.status_code == 416


@pytest.mark.asyncio
async def test_range_file_response_returns_206(tmp_path) -> None:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"abcdefghij")
    response = range_file_response(path, _request("bytes=2-5"))
    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert response.headers["accept-ranges"] == "bytes"
    assert await _body(response) == b"cdef"


@pytest.mark.asyncio
async def test_pull_requires_valid_signature(tmp_path, monkeypatch) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        runpod_api_key="rp_secret",
        public_base_url="https://api.165.245.170.117.sslip.io",
        runpod_face_video_max_bytes=10 * 1024 * 1024,
    )
    monkeypatch.setattr("app.routers.face_gpu_pull.get_settings", lambda: settings)
    session = AsyncMock()
    session.get = AsyncMock(return_value=None)
    exp = int(time.time()) + 600
    with pytest.raises(HTTPException) as exc:
        await pull_face_gpu_video(
            "file-1",
            _request(),
            exp=exp,
            sig="deadbeef",
            session=session,
        )
    assert exc.value.status_code == 403
    session.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_pull_serves_cached_file_with_range(tmp_path, monkeypatch) -> None:
    path = tmp_path / "file-1.mp4"
    path.write_bytes(b"0123456789abcdef")
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        runpod_api_key="rp_secret",
        public_base_url="https://api.165.245.170.117.sslip.io",
        runpod_face_video_max_bytes=10 * 1024 * 1024,
    )
    monkeypatch.setattr("app.routers.face_gpu_pull.get_settings", lambda: settings)
    monkeypatch.setattr("app.routers.face_gpu_pull.video_cache_path", lambda *_args, **_kwargs: path)
    session = AsyncMock()
    session.get = AsyncMock(
        return_value=SimpleNamespace(
            id="file-1", name="clip.mp4", mime_type="video/mp4", source="drive"
        )
    )
    exp = int(time.time()) + 600
    sig = sign_face_video_pull("file-1", exp, "rp_secret")
    response = await pull_face_gpu_video(
        "file-1",
        _request("bytes=0-0"),
        exp=exp,
        sig=sig,
        session=session,
    )
    assert response.status_code == 206
    assert response.headers["content-range"].startswith("bytes 0-0/")
    assert await _body(response) == b"0"
