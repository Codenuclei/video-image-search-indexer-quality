#!/usr/bin/env python3
"""Dry-run buffalo_l on 100 PROCESSED Drive images via RunPod. No Postgres writes."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import httpx
import numpy as np
from sqlalchemy import select

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from app.config import get_settings  # noqa: E402
from app.db.models import DriveFile, DriveFileStatus, Media, MediaType  # noqa: E402
from app.db.session import get_session_factory  # noqa: E402
from app.dependencies import get_drive_client  # noqa: E402
from app.pipelines.common import decode_image_bgr, download_to_temp_file  # noqa: E402

RESULTS_DIR = REPO / "runpod" / "face-buffalo" / "results"
ENDPOINT_FILE = REPO / "runpod" / "face-buffalo" / ".endpoint_id"
LIMIT = 100
MAX_EDGE = 2048
MAX_JPEG_BYTES = 6 * 1024 * 1024


def _load_repo_env() -> None:
    env_path = BACKEND / ".env"
    if not env_path.is_file():
        raise SystemExit(f"Missing {env_path}")
    for line in env_path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, val = s.split("=", 1)
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def _endpoint_id() -> str:
    env_id = os.environ.get("RUNPOD_FACE_ENDPOINT_ID", "").strip()
    if env_id:
        return env_id
    if ENDPOINT_FILE.is_file():
        return ENDPOINT_FILE.read_text().strip()
    raise SystemExit("No RUNPOD_FACE_ENDPOINT_ID or runpod/face-buffalo/.endpoint_id")


def _jpeg_bytes(image_bgr: np.ndarray) -> bytes:
    h, w = image_bgr.shape[:2]
    scale = min(1.0, MAX_EDGE / max(h, w))
    if scale < 1.0:
        image_bgr = cv2.resize(
            image_bgr,
            (int(w * scale), int(h * scale)),
            interpolation=cv2.INTER_AREA,
        )
    quality = 90
    encoded = None
    while quality >= 50:
        ok, buf = cv2.imencode(".jpg", image_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if not ok:
            raise ValueError("JPEG encode failed")
        encoded = buf.tobytes()
        if len(encoded) <= MAX_JPEG_BYTES:
            return encoded
        quality -= 10
    assert encoded is not None
    return encoded


async def _pick_images(limit: int) -> list[tuple[str, str, str]]:
    factory = get_session_factory()
    async with factory() as session:
        rows = (
            await session.execute(
                select(DriveFile.id, DriveFile.name, DriveFile.mime_type)
                .join(Media, Media.drive_file_id == DriveFile.id)
                .where(
                    DriveFile.status == DriveFileStatus.PROCESSED,
                    Media.type == MediaType.IMAGE,
                    DriveFile.mime_type.like("image/%"),
                )
                .order_by(DriveFile.last_synced_at.desc().nulls_last())
                .limit(limit)
            )
        ).all()
    return [(r[0], r[1], r[2]) for r in rows]


async def _fetch_jpeg(drive_file_id: str, name: str) -> bytes:
    settings = get_settings()
    client = get_drive_client()
    suffix = Path(name).suffix or ".bin"
    async with download_to_temp_file(client, drive_file_id, settings, suffix=suffix) as path:
        raw = Path(path).read_bytes()
    image = decode_image_bgr(raw, file_name=name)
    return _jpeg_bytes(image)


async def _runsync(client: httpx.AsyncClient, endpoint_id: str, payload: dict) -> dict:
    url = f"https://api.runpod.ai/v2/{endpoint_id}/runsync"
    headers = {"Authorization": f"Bearer {os.environ['RUNPOD_API_KEY']}", "Content-Type": "application/json"}
    response = await client.post(url, headers=headers, json={"input": payload})
    if response.status_code >= 400:
        raise RuntimeError(f"runsync HTTP {response.status_code}: {response.text[:500]}")
    body = response.json()
    if body.get("status") not in {"COMPLETED", "completed"}:
        raise RuntimeError(f"runsync status={body.get('status')} error={body.get('error') or body}")
    return body.get("output") or {}


async def main() -> None:
    _load_repo_env()
    if not os.environ.get("RUNPOD_API_KEY", "").strip():
        raise SystemExit("RUNPOD_API_KEY missing")
    endpoint_id = _endpoint_id()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    rows = await _pick_images(LIMIT)
    if not rows:
        raise SystemExit("No PROCESSED image rows in the database")
    print(f"Selected {len(rows)} PROCESSED images. Endpoint {endpoint_id}")

    results: list[dict] = []
    timeout = httpx.Timeout(600.0, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        health = await _runsync(http, endpoint_id, {"healthcheck": True})
        print(f"Health: model={health.get('model')} providers={health.get('providers')}")
        for i, (drive_file_id, name, mime) in enumerate(rows, start=1):
            row: dict = {
                "index": i,
                "drive_file_id": drive_file_id,
                "name": name,
                "mime_type": mime,
            }
            t0 = time.perf_counter()
            try:
                jpeg = await _fetch_jpeg(drive_file_id, name)
                output = await _runsync(
                    http,
                    endpoint_id,
                    {
                        "drive_file_id": drive_file_id,
                        "image_b64": base64.b64encode(jpeg).decode("ascii"),
                    },
                )
                row.update(output)
                row["roundtrip_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
                row["jpeg_bytes"] = len(jpeg)
                print(
                    f"[{i}/{len(rows)}] {name}: faces={row.get('face_count')} "
                    f"detect_ms={row.get('detect_ms')} roundtrip_ms={row.get('roundtrip_ms')}"
                )
            except Exception as exc:  # noqa: BLE001
                row["error"] = str(exc)
                row["roundtrip_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
                print(f"[{i}/{len(rows)}] {name}: ERROR {exc}")
            results.append(row)

    ok = [r for r in results if "error" not in r]
    detect_ms = [float(r["detect_ms"]) for r in ok if r.get("detect_ms") is not None]
    face_counts = [int(r.get("face_count") or 0) for r in ok]
    summary = {
        "at": datetime.now(tz=timezone.utc).isoformat(),
        "endpoint_id": endpoint_id,
        "requested": len(rows),
        "ok": len(ok),
        "errors": len(results) - len(ok),
        "faces_total": sum(face_counts),
        "faces_mean": round(statistics.mean(face_counts), 3) if face_counts else None,
        "detect_ms_p50": round(statistics.median(detect_ms), 2) if detect_ms else None,
        "detect_ms_p95": round(sorted(detect_ms)[max(0, int(len(detect_ms) * 0.95) - 1)], 2) if detect_ms else None,
        "detect_ms_mean": round(statistics.mean(detect_ms), 2) if detect_ms else None,
        "providers": ok[0].get("providers") if ok else None,
        "model": "buffalo_l",
        "postgres_writes": False,
    }
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULTS_DIR / f"dry_run_{stamp}.json"
    out_path.write_text(json.dumps({"summary": summary, "results": results}, indent=2))
    print(json.dumps(summary, indent=2))
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
