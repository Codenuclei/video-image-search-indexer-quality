from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass
class FennecScene:
    scene_id: int
    file_id: int
    filename: str
    path: str
    start_time: float
    end_time: float
    transcript: str | None
    visual_score: float | None
    transcript_score: float | None


class FennecClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    @property
    def enabled(self) -> bool:
        return self._settings.fennec_enabled

    def _base(self) -> str:
        return self._settings.fennec_base_url.rstrip("/")

    async def ready(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._base()}/api/ready")
                if resp.status_code != 200:
                    return False
                data = resp.json()
                return bool(data.get("models_ready") or data.get("clip_loaded"))
        except Exception:  # noqa: BLE001
            return False

    async def search(
        self,
        *,
        visual: str | None = None,
        transcript_semantic: str | None = None,
        transcript: str | None = None,
        path_contains: str | None = None,
        limit: int = 50,
    ) -> list[FennecScene]:
        params: dict[str, Any] = {"limit": limit}
        if visual:
            params["visual"] = visual
        if transcript_semantic:
            params["transcript_semantic"] = transcript_semantic
        if transcript:
            params["transcript"] = transcript
        if path_contains:
            params["path"] = path_contains

        async with httpx.AsyncClient(timeout=self._settings.fennec_timeout_seconds) as client:
            resp = await client.get(f"{self._base()}/api/search", params=params)
            resp.raise_for_status()
            payload = resp.json()

        scenes: list[FennecScene] = []
        for item in payload.get("results") or payload.get("scenes") or []:
            if not isinstance(item, dict):
                continue
            scenes.append(
                FennecScene(
                    scene_id=int(item.get("id") or item.get("scene_id") or 0),
                    file_id=int(item.get("file_id") or 0),
                    filename=str(item.get("filename") or ""),
                    path=str(item.get("path") or ""),
                    start_time=float(item.get("start_time") or item.get("start_tc") or 0),
                    end_time=float(item.get("end_time") or item.get("end_tc") or 0),
                    transcript=item.get("transcript"),
                    visual_score=_maybe_float(item.get("visual_score") or item.get("similarity")),
                    transcript_score=_maybe_float(item.get("transcript_score")),
                )
            )
        return [s for s in scenes if s.scene_id > 0]


def _maybe_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


_client: FennecClient | None = None


def get_fennec_client() -> FennecClient:
    global _client
    if _client is None:
        _client = FennecClient()
    return _client
