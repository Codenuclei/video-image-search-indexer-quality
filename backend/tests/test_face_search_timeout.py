"""Interactive reverse face search: downscale + GPU busy (no CPU fallback)."""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from app.config import Settings
from app.faces.runpod_gpu import RunPodFaceError
from app.reid.face_search import (
    FaceSearchBusyError,
    _detect_query_faces,
    downscale_bgr_for_search,
)


def test_face_search_gpu_timeout_default_allows_cold_start() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.face_search_gpu_timeout_seconds == 120.0
    # Indexing path stays on the long RunPod timeout; do not couple them.
    assert settings.runpod_face_timeout_seconds == 3600.0


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
async def test_search_raises_busy_on_gpu_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
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

    cpu_called = {"n": 0}

    class _FakeEngine:
        def detect_faces(self, _image_bgr: np.ndarray) -> list:
            cpu_called["n"] += 1
            return []

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

    image = np.zeros((64, 64, 3), dtype=np.uint8)
    t0 = asyncio.get_event_loop().time()
    with pytest.raises(FaceSearchBusyError, match="Face search is busy"):
        await _detect_query_faces(image, settings=settings)
    elapsed = asyncio.get_event_loop().time() - t0
    assert cpu_called["n"] == 0
    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_search_raises_busy_on_gpu_error(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        runpod_api_key="rp_test",
        runpod_face_endpoint_id="0zub88paibpsf3",
        runpod_face_gpu_enabled=True,
        face_search_gpu_timeout_seconds=5.0,
        face_search_cpu_timeout_seconds=5.0,
    )

    async def _fail_gpu(_image_bgr, **_kwargs):
        raise RunPodFaceError("gpu down")

    monkeypatch.setattr(
        "app.faces.runpod_gpu.runpod_face_configured",
        lambda _settings=None: True,
    )
    monkeypatch.setattr(
        "app.faces.runpod_gpu.detect_faces_runpod",
        _fail_gpu,
    )
    monkeypatch.setattr(
        "app.reid.face_search.get_face_engine",
        lambda: (_ for _ in ()).throw(AssertionError("CPU must not run")),
    )

    image = np.zeros((64, 64, 3), dtype=np.uint8)
    with pytest.raises(FaceSearchBusyError, match="Face search is busy"):
        await _detect_query_faces(image, settings=settings)


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
        def detect_faces(self, _image_bgr: np.ndarray) -> list:
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
