#!/usr/bin/env python3
"""Build annotated YOLOE preview JPEGs (data URLs) for a canvas review."""

from __future__ import annotations

import asyncio
import base64
import json
import os
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

CATALOG = REPO / "runpod" / "object-yoloe" / "results" / "yoloe_catalog_20260904T134451Z.json"
OUT = REPO / "runpod" / "object-yoloe" / "results" / "yoloe_previews.json"
PICK = {51, 99, 45, 36, 80, 61, 52, 85, 62, 19, 16, 22}
CONF_MIN = 0.80
MAX_W = 420
JUNK = {
    "remove", "reveal", "extrude", "assemble", "modern", "lesson",
    "clip art", "team presentation", "press room", "number icon",
    "frame", "head",
}
COLORS = [
    (46, 163, 242),
    (80, 200, 120),
    (80, 80, 220),
    (40, 180, 220),
]


def _load_env() -> None:
    for line in (BACKEND / ".env").read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        os.environ.setdefault(k, v.strip().strip('"').strip("'"))


def _kind(label: str, category: str) -> str:
    if label in JUNK:
        return "junk"
    if category != "open_vocab":
        return "taxonomy"
    return "open"


def _annotate(image: np.ndarray, objects: list[dict]) -> np.ndarray:
    h, w = image.shape[:2]
    scale = min(1.0, MAX_W / max(w, 1))
    if scale < 1.0:
        image = cv2.resize(image, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        sx, sy = scale, scale
    else:
        sx, sy = 1.0, 1.0
    vis = image.copy()
    for i, obj in enumerate(objects[:40]):
        x = int(obj["bbox_x"] * sx)
        y = int(obj["bbox_y"] * sy)
        bw = int(obj["bbox_width"] * sx)
        bh = int(obj["bbox_height"] * sy)
        kind = _kind(obj["canonical_label"], obj["category"])
        color = {"junk": (60, 60, 200), "taxonomy": (80, 200, 120)}.get(kind, COLORS[i % len(COLORS)])
        cv2.rectangle(vis, (x, y), (x + bw, y + bh), color, 2)
        tag = f"{obj['canonical_label'][:22]} {obj['confidence']:.2f}"
        ty = max(14, y - 4)
        cv2.putText(vis, tag, (x, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.38, color, 1, cv2.LINE_AA)
    return vis


async def _fetch(did: str, name: str) -> np.ndarray:
    settings = get_settings()
    client = get_drive_client()
    suffix = Path(name).suffix or ".bin"
    async with download_to_temp_file(client, did, settings, suffix=suffix) as path:
        raw = Path(path).read_bytes()
    return decode_image_bgr(raw, file_name=name)


async def main() -> None:
    _load_env()
    catalog = json.loads(CATALOG.read_text())
    rows = [r for r in catalog["results"] if r["index"] in PICK]
    previews = []
    for row in rows:
        print(f"annotate {row['index']} {row['name']}")
        image = await _fetch(row["drive_file_id"], row["name"])
        objects = [o for o in row["objects"] if float(o["confidence"]) > CONF_MIN]
        vis = _annotate(image, objects)
        ok, buf = cv2.imencode(".jpg", vis, [int(cv2.IMWRITE_JPEG_QUALITY), 52])
        if not ok:
            continue
        counts: dict[str, int] = {}
        for obj in objects:
            counts[obj["canonical_label"]] = counts.get(obj["canonical_label"], 0) + 1
        labels = sorted(
            (
                {
                    "label": k,
                    "n": v,
                    "kind": _kind(k, next(o["category"] for o in objects if o["canonical_label"] == k)),
                    "conf": round(
                        max(o["confidence"] for o in objects if o["canonical_label"] == k),
                        3,
                    ),
                }
                for k, v in counts.items()
            ),
            key=lambda x: (-x["n"], x["label"]),
        )
        previews.append(
            {
                "index": row["index"],
                "name": row["name"],
                "object_count": len(objects),
                "object_count_all": row["object_count"],
                "src": "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii"),
                "labels": labels[:24],
            }
        )
    OUT.write_text(json.dumps({"previews": previews}, separators=(",", ":")))
    print(f"Wrote {OUT} n={len(previews)} bytes={OUT.stat().st_size}")


if __name__ == "__main__":
    asyncio.run(main())
