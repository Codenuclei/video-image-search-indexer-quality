"""Face embeddings for MU People (website Executive Leaders) for reverse face search.

Portraits are embedded once (one batched GPU job) and stored on disk so every
API worker shares them. A search then compares its query embedding against
~20 vectors in memory: no extra GPU work per search.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import cv2
import httpx
import numpy as np

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

_ROSTER_TAB = "executive"
_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; DriveFaceIndexer/1.0; +https://mastersunion.org)"
    ),
    "Accept": "image/*,*/*;q=0.8",
}

_entries: list[dict[str, Any]] = []
_matrix: np.ndarray | None = None
_loaded_mtime: float = 0.0
_build_task: asyncio.Task | None = None


def _index_path(settings: Settings) -> Path:
    return Path(settings.mu_people_index_path)


def _set_entries(entries: list[dict[str, Any]]) -> None:
    global _entries, _matrix
    vectors = [np.asarray(e["embedding"], dtype=np.float32) for e in entries]
    if vectors:
        matrix = np.stack(vectors)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        _matrix = matrix / np.maximum(norms, 1e-12)
    else:
        _matrix = None
    _entries = entries


def _reload_from_disk(settings: Settings) -> None:
    """Pick up an index written by any worker process (cheap stat when unchanged)."""
    global _loaded_mtime
    path = _index_path(settings)
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return
    if mtime <= _loaded_mtime:
        return
    try:
        data = json.loads(path.read_text())
        _set_entries(list(data.get("entries") or []))
        _loaded_mtime = mtime
    except Exception:  # noqa: BLE001
        logger.warning("mu_people_index unreadable path=%s", path, exc_info=True)


def mu_people_status(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    _reload_from_disk(settings)
    age = time.time() - _loaded_mtime if _loaded_mtime else None
    return {
        "ready": bool(_entries),
        "count": len(_entries),
        "age_seconds": round(age) if age is not None else None,
        "building": _build_task is not None and not _build_task.done(),
    }


def ensure_mu_people_index(settings: Settings | None = None) -> None:
    """Start a background (re)build when the index is missing or older than the TTL."""
    global _build_task
    settings = settings or get_settings()
    _reload_from_disk(settings)
    fresh = _loaded_mtime and time.time() - _loaded_mtime < settings.mu_people_index_ttl_seconds
    if fresh or (_build_task is not None and not _build_task.done()):
        return
    _build_task = asyncio.create_task(_build_index(settings))


async def _download(client: httpx.AsyncClient, url: str) -> np.ndarray | None:
    try:
        resp = await client.get(url)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("mu_people portrait download failed url=%s: %s", url[:120], exc)
        return None
    arr = np.frombuffer(resp.content, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


async def _detect_batch(images: list[np.ndarray], settings: Settings) -> list[list]:
    from app.faces.runpod_gpu import detect_faces_runpod_batch, runpod_face_configured

    if runpod_face_configured(settings):
        return await detect_faces_runpod_batch(images, drive_file_id="mu_people", settings=settings)
    from app.faces.engine import get_face_engine
    from app.pipelines.async_cpu import run_cpu_bound

    engine = get_face_engine()
    return [await run_cpu_bound(engine.detect_faces, image) for image in images]


async def _build_index(settings: Settings) -> None:
    from app.reid.mu_leadership import fetch_leadership_roster

    t0 = time.monotonic()
    try:
        roster = await fetch_leadership_roster(_ROSTER_TAB)
        people = [p for p in roster.get("people") or [] if p.get("image_url")]
        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=True, headers=_FETCH_HEADERS
        ) as client:
            images = await asyncio.gather(*(_download(client, p["image_url"]) for p in people))
        pairs = [(p, img) for p, img in zip(people, images, strict=True) if img is not None]
        if not pairs:
            logger.warning("mu_people_index no portraits downloaded")
            return
        detections = await _detect_batch([img for _p, img in pairs], settings)
        entries: list[dict[str, Any]] = []
        for (person, _img), faces in zip(pairs, detections, strict=True):
            if not faces:
                continue
            face = max(faces, key=lambda f: float(f.bbox_width) * float(f.bbox_height))
            entries.append(
                {
                    "name": person["name"],
                    "role": person.get("role") or "",
                    "image_url": person["image_url"],
                    "linkedin_url": person.get("linkedin_url") or None,
                    "embedding": [float(v) for v in face.embedding],
                }
            )
        path = _index_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps({"built_at": time.time(), "entries": entries}))
        os.replace(tmp, path)
        _reload_from_disk(settings)
        logger.info(
            "mu_people_index built people=%d embedded=%d ms=%.0f",
            len(people),
            len(entries),
            (time.monotonic() - t0) * 1000.0,
        )
    except Exception:  # noqa: BLE001
        logger.warning("mu_people_index build failed", exc_info=True)


def match_mu_people(
    embedding: list[float] | np.ndarray,
    *,
    max_distance: float,
    limit: int,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """Rank MU People portraits by cosine distance to the query face."""
    settings = settings or get_settings()
    _reload_from_disk(settings)
    if _matrix is None or not _entries:
        return []
    query = np.asarray(embedding, dtype=np.float32)
    query = query / max(float(np.linalg.norm(query)), 1e-12)
    distances = 1.0 - _matrix @ query
    order = np.argsort(distances)
    out: list[dict[str, Any]] = []
    for idx in order[:limit]:
        d = float(distances[idx])
        if d > max_distance:
            break
        entry = _entries[int(idx)]
        out.append(
            {
                "name": entry["name"],
                "role": entry.get("role") or "",
                "image_url": entry["image_url"],
                "linkedin_url": entry.get("linkedin_url"),
                "score": round(max(0.0, 1.0 - d), 4),
                "distance": round(d, 4),
            }
        )
    return out
