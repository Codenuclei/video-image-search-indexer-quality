#!/usr/bin/env python3
"""Benchmark the face search RunPod endpoint with a single worker.

Phases: cold start, warm sequential, then concurrent bursts. Scale stays
workersMin=0, workersMax=1 (never raise workersMin; see AGENTS.md).

Usage:
  backend/.venv/bin/python scripts/runpod_face_bench_one_worker.py [--bursts 5,10,20]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
REST = "https://rest.runpod.io/v1"
RUN = "https://api.runpod.ai/v2"
SEARCH_TIMEOUT_S = 120.0
IMAGES = {
    "group_small": REPO / "backend/.venv/lib/python3.12/site-packages/insightface/data/images/t1.jpg",
    "event_full": REPO / "runpod/qwen-vl/caption-extras/051_P1022342.jpg",
}


def to_search_jpeg(path: Path) -> bytes:
    """Match production: full resolution, JPEG quality 95 (RUNPOD_FACE_MAX_EDGE=0)."""
    import cv2

    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise SystemExit(f"could not read image {path}")
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not ok:
        raise SystemExit(f"could not encode {path}")
    return buf.tobytes()


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (REPO / ".env").read_text().splitlines():
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line.strip())
        if m:
            env[m[1]] = m[2].strip().strip("\"'")
    return env


class Bench:
    def __init__(self, endpoint: str, key: str) -> None:
        self.endpoint = endpoint
        self.headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=30.0))

    async def scale_one(self) -> None:
        resp = await self.client.patch(
            f"{REST}/endpoints/{self.endpoint}",
            headers=self.headers,
            json={"workersMin": 0, "workersMax": 1},
        )
        resp.raise_for_status()

    async def health(self) -> dict:
        resp = await self.client.get(f"{RUN}/{self.endpoint}/health", headers=self.headers)
        resp.raise_for_status()
        return resp.json()

    async def one_search(self, image_b64: str) -> dict:
        payload = {
            "input": {
                "min_detection_confidence": 0.5,
                "images": [{"drive_file_id": "bench", "image_b64": image_b64}],
            }
        }
        t0 = time.monotonic()
        submit = await self.client.post(f"{RUN}/{self.endpoint}/run", headers=self.headers, json=payload)
        if submit.status_code == 409:
            await self.scale_one()
            submit = await self.client.post(
                f"{RUN}/{self.endpoint}/run", headers=self.headers, json=payload
            )
        if submit.status_code >= 400:
            return {"ok": False, "error": f"submit {submit.status_code}: {submit.text[:200]}",
                    "wall_s": time.monotonic() - t0}
        job_id = submit.json()["id"]
        while True:
            await asyncio.sleep(0.5)
            data = (await self.client.get(
                f"{RUN}/{self.endpoint}/status/{job_id}", headers=self.headers
            )).json()
            status = data.get("status")
            if status in {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}:
                break
        wall = time.monotonic() - t0
        faces = None
        if status == "COMPLETED":
            imgs = (data.get("output") or {}).get("images") or []
            faces = len((imgs[0] or {}).get("faces") or []) if imgs else 0
        return {
            "ok": status == "COMPLETED",
            "status": status,
            "wall_s": wall,
            "queue_s": (data.get("delayTime") or 0) / 1000.0,
            "exec_s": (data.get("executionTime") or 0) / 1000.0,
            "faces": faces,
            "error": data.get("error"),
        }


def summarize(name: str, rows: list[dict]) -> dict:
    ok = [r for r in rows if r["ok"]]
    walls = sorted(r["wall_s"] for r in ok)
    execs = sorted(r["exec_s"] for r in ok)

    def pct(vals: list[float], p: float) -> float:
        if not vals:
            return float("nan")
        return vals[min(len(vals) - 1, int(round(p * (len(vals) - 1))))]

    summary = {
        "phase": name,
        "n": len(rows),
        "ok": len(ok),
        "over_120s": sum(1 for r in rows if r["wall_s"] > SEARCH_TIMEOUT_S),
        "wall_p50": pct(walls, 0.5),
        "wall_p95": pct(walls, 0.95),
        "wall_max": walls[-1] if walls else float("nan"),
        "exec_p50": statistics.median(execs) if execs else float("nan"),
        "exec_max": execs[-1] if execs else float("nan"),
        "faces": sorted({r["faces"] for r in ok}),
        "errors": [r.get("error") for r in rows if not r["ok"]][:3],
    }
    print(
        f"{name:<28} ok={summary['ok']}/{summary['n']}  over120s={summary['over_120s']}  "
        f"wall p50={summary['wall_p50']:.1f}s p95={summary['wall_p95']:.1f}s max={summary['wall_max']:.1f}s  "
        f"exec p50={summary['exec_p50']:.2f}s max={summary['exec_max']:.2f}s  faces={summary['faces']}",
        flush=True,
    )
    if summary["errors"]:
        print(f"  errors: {summary['errors']}", flush=True)
    return summary


async def wait_for_zero_workers(bench: Bench, max_wait_s: float) -> bool:
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        w = (await bench.health()).get("workers") or {}
        if sum(int(v or 0) for v in w.values()) == 0:
            return True
        await asyncio.sleep(5)
    return False


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bursts", default="5,10,20")
    parser.add_argument("--warm-n", type=int, default=10)
    parser.add_argument("--cold-wait", type=float, default=120.0)
    parser.add_argument("--image", help="query image; defaults to bundled sample photos")
    args = parser.parse_args()

    env = load_env()
    endpoint = env.get("RUNPOD_FACE_ENDPOINT_ID", "").strip()
    key = env.get("RUNPOD_API_KEY", "").strip()
    if not endpoint or not key:
        print("RUNPOD_FACE_ENDPOINT_ID / RUNPOD_API_KEY missing in .env", file=sys.stderr)
        return 2

    sources = {"query": Path(args.image)} if args.image else IMAGES
    images: dict[str, str] = {}
    for name, p in sources.items():
        jpeg = to_search_jpeg(p)
        images[name] = base64.b64encode(jpeg).decode("ascii")
        print(f"image {name}: {p.name} -> {len(jpeg) / 1024:.0f} KB jpeg", flush=True)
    burst_image = images.get("event_full") or next(iter(images.values()))

    bench = Bench(endpoint, key)
    results: list[dict] = []
    try:
        print(f"health before: {json.dumps(await bench.health())}", flush=True)
        await bench.scale_one()

        if await wait_for_zero_workers(bench, args.cold_wait):
            results.append(summarize("cold start (1 job)", [await bench.one_search(burst_image)]))
        else:
            print("workers still up; skipping cold start", flush=True)

        for name, b64 in images.items():
            await bench.scale_one()
            rows = [await bench.one_search(b64) for _ in range(args.warm_n)]
            results.append(summarize(f"warm sequential {name}", rows))

        for size in [int(x) for x in args.bursts.split(",") if x]:
            await bench.scale_one()
            rows = await asyncio.gather(*(bench.one_search(burst_image) for _ in range(size)))
            results.append(summarize(f"burst {size} concurrent", list(rows)))

        print(f"health after: {json.dumps(await bench.health())}", flush=True)
    finally:
        await bench.client.aclose()

    out = REPO / ".tmp-runpod-face-bench.json"
    out.write_text(json.dumps(results, indent=1, default=str))
    print(f"wrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
