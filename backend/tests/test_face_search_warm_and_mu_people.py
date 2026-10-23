"""Face GPU warm-up and MU People portrait matching for reverse face search."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import numpy as np

from app.config import Settings
from app.faces import runpod_gpu as gpu
from app.faces.runpod_gpu import face_gpu_state
from app.reid import mu_people_index


def _settings(**kw) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        runpod_api_key="rp_test",
        runpod_face_endpoint_id="0zub88paibpsf3",
        runpod_face_gpu_enabled=True,
        **kw,
    )


def test_face_gpu_state() -> None:
    assert face_gpu_state({"workers": {"idle": 1}}) == "ready"
    assert face_gpu_state({"workers": {"running": 1}}) == "ready"
    assert face_gpu_state({"workers": {"initializing": 1}}) == "starting"
    assert face_gpu_state({"workers": {}, "jobs": {"inQueue": 2}}) == "starting"
    assert face_gpu_state({"workers": {}, "jobs": {}}) == "cold"


class _Resp:
    def __init__(self, payload: dict, status: int = 200, text: str = "") -> None:
        self._payload = payload
        self.status_code = status
        self.text = text

    def json(self) -> dict:
        return self._payload


def _client_for(health: dict, posts: list[dict]):
    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url: str, headers: dict):
            assert url.endswith("/health")
            return _Resp(health)

        async def post(self, url: str, headers: dict, json: dict):
            posts.append(json)
            return _Resp({"id": "warm-1"})

    return _Client


def _warm(health: dict) -> tuple[dict, list[dict]]:
    posts: list[dict] = []
    gpu._last_warm_submit = 0.0
    with (
        patch("app.faces.runpod_gpu.httpx.AsyncClient", _client_for(health, posts)),
        patch("app.faces.runpod_gpu.ensure_face_endpoint_active", AsyncMock()),
    ):
        out = asyncio.run(gpu.warm_face_endpoint(_settings()))
    return out, posts


def test_warm_submits_healthcheck_only_when_cold() -> None:
    out, posts = _warm({"workers": {}, "jobs": {}})
    assert out == {"gpu": "starting"}
    assert posts == [{"input": {"healthcheck": True}}]


def test_warm_does_not_queue_work_when_worker_up_or_booting() -> None:
    out, posts = _warm({"workers": {"idle": 1}})
    assert out == {"gpu": "ready"} and posts == []
    out, posts = _warm({"workers": {"initializing": 1}})
    assert out == {"gpu": "starting"} and posts == []


def test_warm_cooldown_prevents_repeat_healthchecks() -> None:
    posts: list[dict] = []
    gpu._last_warm_submit = 0.0
    with (
        patch("app.faces.runpod_gpu.httpx.AsyncClient", _client_for({"workers": {}}, posts)),
        patch("app.faces.runpod_gpu.ensure_face_endpoint_active", AsyncMock()),
    ):
        asyncio.run(gpu.warm_face_endpoint(_settings()))
        asyncio.run(gpu.warm_face_endpoint(_settings()))
    assert len(posts) == 1


def _write_index(path, entries: list[dict]) -> None:
    path.write_text(json.dumps({"built_at": 0, "entries": entries}))


def test_match_mu_people_ranks_and_thresholds(tmp_path) -> None:
    a = np.zeros(512, dtype=np.float32)
    a[0] = 1.0
    b = np.zeros(512, dtype=np.float32)
    b[1] = 1.0
    near_a = a.copy()
    near_a[1] = 0.2
    path = tmp_path / "mu.json"
    _write_index(
        path,
        [
            {"name": "Leader A", "role": "CEO", "image_url": "https://x/a.jpg", "embedding": a.tolist()},
            {"name": "Leader B", "role": "CTO", "image_url": "https://x/b.jpg", "embedding": b.tolist()},
        ],
    )
    mu_people_index._loaded_mtime = 0.0
    settings = _settings(mu_people_index_path=str(path))
    hits = mu_people_index.match_mu_people(
        near_a.tolist(), max_distance=0.55, limit=5, settings=settings
    )
    assert [h["name"] for h in hits] == ["Leader A"]
    assert hits[0]["score"] > 0.9
    assert mu_people_index.mu_people_status(settings)["count"] == 2


def test_match_mu_people_empty_without_index(tmp_path) -> None:
    mu_people_index._loaded_mtime = 0.0
    mu_people_index._set_entries([])
    settings = _settings(mu_people_index_path=str(tmp_path / "missing.json"))
    assert mu_people_index.match_mu_people([1.0] * 512, max_distance=0.55, limit=5, settings=settings) == []
    assert mu_people_index.mu_people_status(settings)["ready"] is False
