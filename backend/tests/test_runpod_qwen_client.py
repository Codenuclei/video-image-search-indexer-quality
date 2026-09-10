"""Qwen identify goes to serverless, never a dedicated proxy or the face endpoint."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from app.config import Settings
from app.qwen.runpod_serverless import (
    build_qwen_identify_batch_payload,
    build_qwen_identify_payload,
    payload_contains_drive_url,
    runpod_qwen_configured,
    serverless_qwen_scale,
    parse_qwen_identify_output,
)


def test_qwen_needs_endpoint_and_key() -> None:
    settings = Settings(_env_file=None, runpod_api_key="", runpod_qwen_endpoint_id="ya97mr5kgtwdi1")  # type: ignore[call-arg]
    assert runpod_qwen_configured(settings) is False
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        runpod_api_key="rp_test",
        runpod_qwen_endpoint_id="ya97mr5kgtwdi1",
        runpod_face_endpoint_id="0zub88paibpsf3",
    )
    assert runpod_qwen_configured(settings) is True


def test_qwen_refuses_face_endpoint() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        runpod_api_key="rp_test",
        runpod_qwen_endpoint_id="0zub88paibpsf3",
        runpod_face_endpoint_id="0zub88paibpsf3",
    )
    assert runpod_qwen_configured(settings) is False


def test_qwen_payload_is_bytes_not_drive_url() -> None:
    payload = build_qwen_identify_payload(b"\xff\xd8fakejpeg")
    assert "image_b64" in payload
    assert "http" not in str(payload)
    assert payload_contains_drive_url(payload) is False


def test_qwen_payload_rejects_drive_url_blob() -> None:
    assert payload_contains_drive_url({"image_url": "https://drive.google.com/file/d/x"}) is True


def test_qwen_batch_payload_uses_images_array() -> None:
    payload = build_qwen_identify_batch_payload(
        [(b"\xff\xd8one", "one"), (b"\xff\xd8two", "two")]
    )
    assert len(payload["images"]) == 2
    assert [row["index"] for row in payload["images"]] == [0, 1]
    assert all(row["image_b64"] for row in payload["images"])
    assert payload_contains_drive_url(payload) is False


def test_qwen_scale_min_one_under_load() -> None:
    assert serverless_qwen_scale(active=True) == {"workersMin": 1, "workersMax": 1}
    assert serverless_qwen_scale(active=True, workers_max=2) == {"workersMin": 1, "workersMax": 2}
    assert serverless_qwen_scale(active=False, workers_max=2) == {"workersMin": 0, "workersMax": 0}


def test_parse_qwen_identify_output_prefers_raw_text() -> None:
    text = parse_qwen_identify_output({"raw_text": "OBJECTS\ncoffee cup | mug\n", "tags": []})
    assert "coffee cup" in text


def test_post_identify_uses_serverless_when_configured() -> None:
    import inspect

    from app.workers import identify_queue as ident

    src = inspect.getsource(ident._post_identify)
    assert "runpod_qwen_configured" in src
    assert "identify_jpeg_runpod" in src
    assert src.index("runpod_qwen_configured") < src.index("sglang_base_url")
    assert "proxy.runpod.net" in src


def test_identify_pipeline_caps_gpu_on_serverless() -> None:
    import inspect

    from app.workers import identify_queue as ident

    src = inspect.getsource(ident.IdentifyWorkerLoop._run)
    assert "runpod_qwen_configured" in src
    assert "min(8, gpu_n)" in src


@patch("app.qwen.runpod_serverless.set_qwen_workers_max", new_callable=AsyncMock)
def test_identify_jpeg_runpod_posts_input_bytes(_scale: AsyncMock) -> None:
    from app.qwen.runpod_serverless import identify_jpeg_runpod

    class _Resp:
        def __init__(self, payload: dict, status: int = 200) -> None:
            self._payload = payload
            self.status_code = status
            self.text = ""

        def json(self) -> dict:
            return self._payload

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url: str, headers: dict, json: dict):
            assert "/run" in url
            assert "ya97mr5kgtwdi1" in url
            assert "image_b64" in json["input"]
            assert "drive.google.com" not in str(json)
            return _Resp({"id": "job-1"})

        async def get(self, url: str, headers: dict):
            return _Resp({"status": "COMPLETED", "output": {"raw_text": "OBJECTS\nflag | banner\n"}})

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        runpod_api_key="rp_test",
        runpod_qwen_endpoint_id="ya97mr5kgtwdi1",
        runpod_face_endpoint_id="0zub88paibpsf3",
        qwen_identify_max_tokens=1536,
        runpod_qwen_timeout_seconds=30.0,
    )
    with patch("app.qwen.runpod_serverless.httpx.AsyncClient", _Client):
        text = asyncio.run(identify_jpeg_runpod(b"\xff\xd8abc", settings))
    assert "flag" in text


@patch("app.qwen.runpod_serverless.set_qwen_workers_max", new_callable=AsyncMock)
def test_identify_jpeg_retries_paused_endpoint(_scale: AsyncMock) -> None:
    from app.qwen.runpod_serverless import identify_jpeg_runpod

    posts = {"n": 0}

    class _Resp:
        def __init__(self, payload: dict, status: int = 200) -> None:
            self._payload = payload
            self.status_code = status
            self.text = "paused" if status == 409 else ""

        def json(self) -> dict:
            return self._payload

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url: str, headers: dict, json: dict):
            posts["n"] += 1
            if posts["n"] == 1:
                return _Resp({}, status=409)
            return _Resp({"id": "job-2"})

        async def get(self, url: str, headers: dict):
            return _Resp({"status": "COMPLETED", "output": {"raw_text": "OBJECTS\ncup\n"}})

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        runpod_api_key="rp_test",
        runpod_qwen_endpoint_id="ya97mr5kgtwdi1",
        runpod_face_endpoint_id="0zub88paibpsf3",
        qwen_identify_max_tokens=1536,
        runpod_qwen_timeout_seconds=30.0,
    )
    with (
        patch("app.qwen.runpod_serverless.httpx.AsyncClient", _Client),
        patch("app.qwen.runpod_serverless.asyncio.sleep", new_callable=AsyncMock),
    ):
        text = asyncio.run(identify_jpeg_runpod(b"\xff\xd8abc", settings))
    assert posts["n"] == 2
    assert "cup" in text

