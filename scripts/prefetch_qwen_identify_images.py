#!/usr/bin/env python3
"""Download the 100 dry-run Drive images to a local JPEG cache.

No Postgres writes. Files are resized (long edge 1024) so later GPU identify
sends bytes, not remote URLs.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

import cv2
import numpy as np

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from app.config import get_settings  # noqa: E402
from app.dependencies import get_drive_client  # noqa: E402
from app.pipelines.common import decode_image_bgr, download_to_temp_file  # noqa: E402

RESULTS_DIR = REPO / "runpod" / "face-buffalo" / "results"
PREVIOUS = RESULTS_DIR / "dry_run_20260904T120651Z.json"
IMAGE_DIR = REPO / "runpod" / "qwen-vl" / "images"
LIMIT = 100
MAX_EDGE = 1024
FETCH_CONCURRENCY = 6
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _load_repo_env() -> None:
    env_path = BACKEND / ".env"
    if not env_path.is_file():
        raise SystemExit(f"Missing {env_path}")
    for line in env_path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, val = s.split("=", 1)
        os.environ.setdefault(key, val.strip().strip('"').strip("'"))


def _ids() -> list[tuple[str, str]]:
    path = PREVIOUS if PREVIOUS.is_file() else None
    if path is None:
        latest = sorted(RESULTS_DIR.glob("dry_run_*.json"))
        path = latest[-1] if latest else None
    if path is None:
        raise SystemExit("No prior dry-run JSON with Drive ids")
    data = json.loads(path.read_text())
    rows = []
    for item in data.get("results") or []:
        did = item.get("drive_file_id")
        if did:
            rows.append((did, item.get("name") or did))
    print(f"Reusing {len(rows)} ids from {path.name}")
    return rows[:LIMIT]


def _jpeg(image_bgr: np.ndarray) -> bytes:
    h, w = image_bgr.shape[:2]
    scale = min(1.0, MAX_EDGE / max(h, w))
    if scale < 1.0:
        image_bgr = cv2.resize(
            image_bgr,
            (int(w * scale), int(h * scale)),
            interpolation=cv2.INTER_AREA,
        )
    ok, buf = cv2.imencode(".jpg", image_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    if not ok:
        raise ValueError("JPEG encode failed")
    return buf.tobytes()


async def _one(index: int, did: str, name: str, sem: asyncio.Semaphore) -> dict:
    async with sem:
        settings = get_settings()
        client = get_drive_client()
        suffix = Path(name).suffix or ".bin"
        async with download_to_temp_file(client, did, settings, suffix=suffix) as path:
            raw = Path(path).read_bytes()
        image = decode_image_bgr(raw, file_name=name)
        jpeg = _jpeg(image)
        h, w = image.shape[:2]
        safe = _SAFE.sub("_", Path(name).stem)[:40] or did[:12]
        dest = IMAGE_DIR / f"{index:03d}_{safe}.jpg"
        dest.write_bytes(jpeg)
        print(f"[{index:03d}] {name} -> {dest.name} {len(jpeg)}B")
        return {
            "index": index,
            "drive_file_id": did,
            "name": name,
            "path": str(dest.relative_to(REPO)),
            "orig_width": int(w),
            "orig_height": int(h),
            "bytes": len(jpeg),
        }


async def main() -> None:
    _load_repo_env()
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    picked = _ids()
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)
    rows = await asyncio.gather(
        *[_one(i, did, name, sem) for i, (did, name) in enumerate(picked, start=1)]
    )
    manifest = IMAGE_DIR / "manifest.json"
    manifest.write_text(json.dumps({"images": rows, "max_edge": MAX_EDGE}, indent=2))
    print(f"Wrote {manifest} n={len(rows)}")


if __name__ == "__main__":
    asyncio.run(main())
