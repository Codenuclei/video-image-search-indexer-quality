"""Interactive reverse face search: downscale + GPU→CPU timeout fallback."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import numpy as np
import pytest

from app.config import Settings
from app.db.models import EMBEDDING_DIM
from app.faces.engine import DetectedFace
from app.reid.face_search import (
    FaceSearchBusyError,
    _detect_query_faces,
    downscale_bgr_for_search,
)


def test_downscale_bgr_for_search_caps_long_edge() -> None:
    image = np.zeros((2000, 3000, 3), dtype=np.uint8)
    out = downscale_bgr_for_search(image, 1600)
    h, w = out.shape[:2]
    assert max(h, w) == 1600
    assert abs(w / h - 1.5) < 0.02


def test_downscale_leaves_small_images() -> None:
    image = np.zeros((600, 800, 3), dtype=np.uint8)
    out = downscale_bgr_for_search(image, 1600)
    assert out is image
    assert out.shape[:2] == (600, 800)


@pytest.mark.asyncio
async def test_search_falls_back_to_cpu_on_gpu_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        runpod_api_key="rp_test",
        runpod_face_endpoint_id="0zub88paibpsf3",
        runpod_face_gpu_enabled=True,
        face_search_gpu_timeout_seconds=0.2,
        face_search_cpu_timeout_seconds=5.0,
    )

    async def _slow_gpu(_image_bgr, **_kwargs):
        await asyncio.sleep(5.0)
        return []

    fake_face = DetectedFace(
        bbox_x=0.0,
        bbox_y=0.0,
        bbox_width=10.0,
        bbox_height=10.0,
        confidence=0.99,
        embedding=[0.0] * EMBEDDING_DIM,
        thumbnail_jpeg=b"",
    )

    class _FakeEngine:
        def detect_faces(self, _image_bgr: np.ndarray) -> list[DetectedFace]:
            return [fake_face]

    monkeypatch.setattr(
        "app.faces.runpod_gpu.runpod_face_configured",
        lambda _settings=None: True,
    )
    monkeypatch.setattr(
        "app.faces.runpod_gpu.detect_faces_runpod",
        _slow_gpu,
    )
    monkeypatch.setattr(
        "app.reid.face_search.get_face_engine",
        lambda: _FakeEngine(),
    )
    monkeypatch.setattr(
        "app.reid.face_search.run_cpu_bound",
        AsyncMock(side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs)),
    )

    image = np.zeros((64, 64, 3), dtype=np.uint8)
    t0 = asyncio.get_event_loop().time()
    detections, engine = await _detect_query_faces(image, settings=settings)
    elapsed = asyncio.get_event_loop().time() - t0

    assert engine == "cpu"
    assert len(detections) == 1
    assert detections[0].confidence == 0.99
    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_detect_raises_busy_when_cpu_also_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        runpod_api_key="",
        runpod_face_gpu_enabled=False,
        face_search_gpu_timeout_seconds=0.2,
        face_search_cpu_timeout_seconds=0.1,
    )

    class _FakeEngine:
        def detect_faces(self, _image_bgr: np.ndarray) -> list[DetectedFace]:
            return []

    monkeypatch.setattr(
        "app.faces.runpod_gpu.runpod_face_configured",
        lambda _settings=None: False,
    )
    monkeypatch.setattr(
        "app.reid.face_search.get_face_engine",
        lambda: _FakeEngine(),
    )

    async def _slow_cpu(_fn, *_args, **_kwargs):
        await asyncio.sleep(5.0)
        return []

    monkeypatch.setattr("app.reid.face_search.run_cpu_bound", _slow_cpu)

    image = np.zeros((32, 32, 3), dtype=np.uint8)
    with pytest.raises(FaceSearchBusyError, match="Face search is busy"):
        await _detect_query_faces(image, settings=settings)
