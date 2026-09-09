#!/usr/bin/env python3
"""Catalog objects on the last 100 dry-run images with YOLOE-26 prompt-free.

No Postgres writes. Uses the built-in 4,585-class vocab (no class list required).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from app.dependencies import get_drive_client  # noqa: E402
from app.objects.yoloe_engine import YOLOE_MODEL_VERSION, detect_objects_bgr  # noqa: E402
from app.pipelines.common import decode_image_bgr, download_to_temp_file  # noqa: E402
from app.config import get_settings  # noqa: E402

RESULTS_DIR = REPO / "runpod" / "face-buffalo" / "results"
PREVIOUS = RESULTS_DIR / "dry_run_20260904T120651Z.json"
OUT_DIR = REPO / "runpod" / "object-yoloe" / "results"
LIMIT = 100
MAX_EDGE = 1600
FETCH_CONCURRENCY = 6


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


def _resize(image_bgr: np.ndarray) -> np.ndarray:
    h, w = image_bgr.shape[:2]
    scale = min(1.0, MAX_EDGE / max(h, w))
    if scale >= 1.0:
        return image_bgr
    return cv2.resize(image_bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)


async def _fetch(drive_file_id: str, name: str) -> np.ndarray:
    settings = get_settings()
    client = get_drive_client()
    suffix = Path(name).suffix or ".bin"
    async with download_to_temp_file(client, drive_file_id, settings, suffix=suffix) as path:
        raw = Path(path).read_bytes()
    return _resize(decode_image_bgr(raw, file_name=name))


async def main() -> None:
    _load_repo_env()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    picked = _ids()
    print(f"YOLOE catalog {len(picked)} images model={YOLOE_MODEL_VERSION} (no Postgres writes)")

    sem = asyncio.Semaphore(FETCH_CONCURRENCY)
    images: dict[str, np.ndarray] = {}

    async def _one(did: str, name: str) -> None:
        async with sem:
            images[did] = await _fetch(did, name)

    t_fetch = time.perf_counter()
    await asyncio.gather(*[_one(did, name) for did, name in picked])
    print(f"Prefetched {len(images)} in {time.perf_counter() - t_fetch:.1f}s")

    # Warm weights before the timed loop.
    detect_objects_bgr(next(iter(images.values())))

    results = []
    counts: Counter[str] = Counter()
    t0 = time.perf_counter()
    for i, (did, name) in enumerate(picked, start=1):
        image = images[did]
        t1 = time.perf_counter()
        dets = detect_objects_bgr(image)
        ms = (time.perf_counter() - t1) * 1000.0
        h, w = image.shape[:2]
        for d in dets:
            counts[d.canonical_label] += 1
        print(f"[{i}/{len(picked)}] {name}: objects={len(dets)} ms={ms:.0f}")
        results.append(
            {
                "index": i,
                "drive_file_id": did,
                "name": name,
                "width": int(w),
                "height": int(h),
                "detect_ms": round(ms, 2),
                "object_count": len(dets),
                "objects": [
                    {
                        "label": d.label,
                        "canonical_label": d.canonical_label,
                        "category": d.category,
                        "confidence": round(d.confidence, 4),
                        "bbox_x": round(d.bbox_x, 2),
                        "bbox_y": round(d.bbox_y, 2),
                        "bbox_width": round(d.bbox_width, 2),
                        "bbox_height": round(d.bbox_height, 2),
                    }
                    for d in dets
                ],
            }
        )

    elapsed = time.perf_counter() - t0
    summary = {
        "at": datetime.now(tz=timezone.utc).isoformat(),
        "model": YOLOE_MODEL_VERSION,
        "mode": "prompt-free-4585",
        "postgres_writes": False,
        "images": len(picked),
        "objects_total": sum(r["object_count"] for r in results),
        "unique_labels": len(counts),
        "detect_s": round(elapsed, 2),
        "img_per_s": round(len(picked) / elapsed, 3) if elapsed else None,
        "top_labels": counts.most_common(40),
    }
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = OUT_DIR / f"yoloe_catalog_{stamp}.json"
    out.write_text(json.dumps({"summary": summary, "results": results}, indent=2))
    print(json.dumps(summary, indent=2))
    print(f"Wrote {out}")


if __name__ == "__main__":
    asyncio.run(main())
