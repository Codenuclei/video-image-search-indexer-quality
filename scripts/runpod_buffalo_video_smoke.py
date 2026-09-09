#!/usr/bin/env python3
"""Smoke the signed video pull + RunPod buffalo_l frames.

No Postgres writes. Never sends a Drive URL. Uses PUBLIC_BASE_URL
``/internal/face-gpu-video/{id}`` so the GPU worker Range-GETs the cache.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import selectinload

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(SCRIPTS))
os.chdir(BACKEND)

from app.config import get_settings  # noqa: E402
from app.db.models import DriveFile, Media, MediaType, VideoSegment  # noqa: E402
from app.db.session import get_session_factory  # noqa: E402
from app.faces.runpod_gpu import (  # noqa: E402
    build_face_video_payload,
    payload_contains_drive_url,
    runpod_face_configured,
    set_face_workers_max,
)
from app.faces.video_pull import signed_face_video_pull_url  # noqa: E402
from runpod_buffalo_embed_smoke import RESULTS_DIR, _endpoint_id, _load_repo_env, _run_job  # noqa: E402
from runpod_buffalo_l2_recall import _wait_face_ready  # noqa: E402

MAX_SMOKE_BYTES = 40 * 1024 * 1024


async def _pick_video() -> tuple[DriveFile, list[float]] | None:
    factory = get_session_factory()
    async with factory() as session:
        rows = (
            await session.execute(
                select(Media)
                .options(selectinload(Media.drive_file), selectinload(Media.video_segments))
                .where(Media.type == MediaType.VIDEO)
                .limit(80)
            )
        ).scalars().all()
    ranked: list[tuple[int, DriveFile, list[float]]] = []
    for media in rows:
        drive = media.drive_file
        if drive is None:
            continue
        stamps = sorted(
            {
                round(float(seg.start_sec or 0.0), 3)
                for seg in (media.video_segments or [])
                if seg.start_sec is not None
            }
        )
        if not stamps:
            stamps = [0.0]
        ranked.append((len(stamps), drive, stamps[:3]))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return (ranked[0][1], ranked[0][2]) if ranked else None


async def main() -> None:
    _load_repo_env()
    os.environ["PUBLIC_BASE_URL"] = (
        os.environ.get("VIDEO_PULL_PUBLIC_BASE_URL")
        or "https://api.165.245.170.117.sslip.io"
    )
    settings = get_settings()
    if not os.environ.get("RUNPOD_API_KEY", "").strip():
        raise SystemExit("RUNPOD_API_KEY missing")
    if not runpod_face_configured(settings):
        raise SystemExit("RunPod face endpoint is not configured (or matches Qwen)")
    if not (settings.public_base_url or "").strip():
        raise SystemExit("PUBLIC_BASE_URL missing")

    dummy_id = "video-pull-smoke-missing"
    dummy_url = signed_face_video_pull_url(dummy_id, settings)
    if "drive.google.com" in dummy_url.casefold():
        raise SystemExit("Refusing Drive URL")
    parsed = urlparse(dummy_url)
    query = parse_qs(parsed.query)
    bad = dummy_url.replace(query["sig"][0], "deadbeef")
    print(f"Video pull HMAC smoke base={settings.public_base_url}", flush=True)

    timeout = httpx.Timeout(60.0, connect=20.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
        denied = await http.get(bad, headers={"Range": "bytes=0-0"})
        print(f"bad sig HTTP {denied.status_code}", flush=True)
        if denied.status_code != 403:
            raise SystemExit(f"expected 403 for bad signature, got {denied.status_code}")
        missing = await http.get(dummy_url, headers={"Range": "bytes=0-0"})
        print(f"missing file HTTP {missing.status_code}", flush=True)
        if missing.status_code != 404:
            raise SystemExit(f"expected 404 for unknown id, got {missing.status_code}")

    drive_file = None
    timestamps = [0.0]
    probe_status = None
    probe_headers: dict[str, str] = {}
    try:
        picked = await _pick_video()
    except Exception as exc:  # noqa: BLE001
        print(f"Postgres video pick skipped: {type(exc).__name__}", flush=True)
        picked = None
    if picked is not None:
        drive_file, timestamps = picked
        url = signed_face_video_pull_url(drive_file.id, settings)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
            probe = await http.get(url, headers={"Range": "bytes=0-0"})
        probe_status = probe.status_code
        probe_headers = {k.lower(): v for k, v in probe.headers.items()}
        print(
            f"cached probe file={drive_file.id} HTTP {probe.status_code} "
            f"content-range={probe.headers.get('content-range')}",
            flush=True,
        )

    summary = {
        "at": datetime.now(tz=timezone.utc).isoformat(),
        "postgres_writes": False,
        "drive_file_id": None if drive_file is None else drive_file.id,
        "name": None if drive_file is None else drive_file.name,
        "public_base_url": settings.public_base_url,
        "bad_signature_status": denied.status_code,
        "missing_file_status": missing.status_code,
        "range_status": probe_status,
        "accept_ranges": probe_headers.get("accept-ranges"),
        "content_range": probe_headers.get("content-range"),
        "cached": probe_status in {200, 206},
        "runpod": None,
    }

    if probe_status is None:
        print("No cached video probed — HMAC 403/404 is enough for this smoke.", flush=True)
    elif probe_status == 404:
        print("Cache miss on the API box — HMAC path is live; skip GPU download.", flush=True)
    elif probe_status == 206:
        total = 0
        header = probe_headers.get("content-range") or ""
        if "/" in header:
            try:
                total = int(header.rsplit("/", 1)[-1])
            except ValueError:
                total = 0
        if total > MAX_SMOKE_BYTES:
            print(f"Cached video is {total} bytes; skip GPU to keep the smoke small.", flush=True)
        else:
            payload = build_face_video_payload(
                timestamps,
                video_url=url,
                drive_file_id=drive_file.id,
                video_suffix=Path(drive_file.name or "clip.mp4").suffix or ".mp4",
            )
            if payload_contains_drive_url(payload):
                raise SystemExit("Refusing Drive URL in RunPod payload")
            endpoint_id = _endpoint_id()
            await set_face_workers_max(settings, 1)
            try:
                job_timeout = httpx.Timeout(600.0, connect=30.0)
                async with httpx.AsyncClient(timeout=job_timeout) as http:
                    await _wait_face_ready(http, endpoint_id)
                    health = await _run_job(
                        http, endpoint_id, {"healthcheck": True}, timeout_s=1800.0
                    )
                    print(
                        f"health model={health.get('model')} providers={health.get('providers')}",
                        flush=True,
                    )
                    t0 = time.perf_counter()
                    output = await _run_job(http, endpoint_id, payload, timeout_s=1800.0)
                    elapsed = time.perf_counter() - t0
            finally:
                await set_face_workers_max(settings, 0)
            frames = output.get("frames") or []
            summary["runpod"] = {
                "elapsed_s": round(elapsed, 1),
                "ffmpeg": output.get("ffmpeg"),
                "download": output.get("download"),
                "frame_count": len(frames),
                "face_counts": [int(frame.get("face_count") or 0) for frame in frames],
                "error": output.get("error"),
            }
            print(json.dumps(summary["runpod"], indent=2), flush=True)
    else:
        raise SystemExit(f"unexpected Range status {probe_status}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = RESULTS_DIR / f"video_pull_{stamp}.json"
    dest.write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2), flush=True)
    print(f"Wrote {dest}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
