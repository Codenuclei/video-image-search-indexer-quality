#!/usr/bin/env python3
"""Run the mixed identify+caption prompt on 10 photos via Qwen serverless.

Never creates a dedicated pod. Uses the existing scale-to-zero endpoint.
No Postgres writes. No Drive URLs in the GPU payload.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(REPO / "runpod" / "qwen-vl"))

from image_source import resolve_qwen_image  # noqa: E402
from tags import IDENTIFY_AND_CAPTION_PROMPT  # noqa: E402

RETTEST = Path("/tmp/qwen-pg-identify-canvas/retest_v2.json")
JPEG_DIR = Path("/tmp/qwen-pg-identify-canvas/full-jpegs")
OUT_DIR = REPO / "runpod" / "qwen-vl" / "results"
ENDPOINT_FILE = REPO / "runpod" / "qwen-vl" / ".endpoint_id"
REST = "https://rest.runpod.io/v1"
FACE_ENDPOINT_NAME = "dfi-face-buffalo"
PICK = (2, 3, 4, 34, 41, 44, 53, 61, 69, 76)


def _load_env() -> None:
    env_path = BACKEND / ".env"
    if not env_path.is_file():
        raise SystemExit(f"Missing {env_path}")
    for line in env_path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, val = s.split("=", 1)
        os.environ.setdefault(key, val.strip().strip('"').strip("'"))


def _auth() -> dict[str, str]:
    key = os.environ.get("RUNPOD_API_KEY", "").strip()
    if not key:
        raise SystemExit("RUNPOD_API_KEY is missing from backend/.env")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _endpoint_id() -> str:
    env_id = os.environ.get("RUNPOD_QWEN_ENDPOINT_ID", "").strip()
    if env_id:
        return env_id
    if ENDPOINT_FILE.is_file():
        return ENDPOINT_FILE.read_text().strip()
    raise SystemExit("No RUNPOD_QWEN_ENDPOINT_ID or runpod/qwen-vl/.endpoint_id")


def _rows() -> list[dict]:
    if not RETTEST.is_file() or not JPEG_DIR.is_dir():
        raise SystemExit(f"Missing {RETTEST} or {JPEG_DIR}")
    by_i = {int(r["i"]): r for r in json.loads(RETTEST.read_text())["results"]}
    rows = []
    for i in PICK:
        src = by_i[i]
        jpeg_path = Path(src["jpeg_path"])
        if not jpeg_path.is_file():
            jpeg_path = JPEG_DIR / f"{src['drive_file_id']}.jpg"
        if not jpeg_path.is_file():
            raise SystemExit(f"Missing JPEG for {src['name']}")
        rows.append(
            {
                "i": i,
                "drive_file_id": src["drive_file_id"],
                "name": src["name"],
                "jpeg_path": str(jpeg_path),
                "jpeg_bytes": jpeg_path.stat().st_size,
            }
        )
    return rows


async def _run_job(
    client: httpx.AsyncClient,
    endpoint_id: str,
    payload: dict,
    timeout_s: float,
) -> dict:
    headers = _auth()
    submit = await client.post(
        f"https://api.runpod.ai/v2/{endpoint_id}/run",
        headers=headers,
        json={"input": payload},
    )
    if submit.status_code >= 400:
        raise RuntimeError(f"run HTTP {submit.status_code}: {submit.text[:500]}")
    body = submit.json()
    job_id = body.get("id")
    if not job_id:
        raise RuntimeError(f"run missing id: {body}")
    status_url = f"https://api.runpod.ai/v2/{endpoint_id}/status/{job_id}"
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            status_resp = await client.get(status_url, headers=headers)
        except httpx.TransportError as exc:
            print(f"  status poll retry ({exc})", flush=True)
            await asyncio.sleep(3.0)
            continue
        if status_resp.status_code >= 400:
            raise RuntimeError(
                f"status HTTP {status_resp.status_code}: {status_resp.text[:500]}"
            )
        data = status_resp.json()
        last = str(data.get("status") or "")
        if last.lower() in {"completed"}:
            return data.get("output") or {}
        if last.lower() in {"failed", "cancelled", "timed_out"}:
            raise RuntimeError(f"job {job_id} {last}: {data.get('error') or data}")
        print(f"  job {job_id} {last}", flush=True)
        await asyncio.sleep(4.0)
    raise RuntimeError(f"job {job_id} still {last} after {timeout_s:.0f}s")


def _scale(client: httpx.Client, headers: dict[str, str], endpoint_id: str, workers_max: int) -> None:
    resp = client.patch(
        f"{REST}/endpoints/{endpoint_id}",
        headers=headers,
        json={"workersMin": 0, "workersMax": workers_max},
    )
    if resp.status_code >= 400:
        raise SystemExit(f"Scale failed {resp.status_code}: {resp.text[:400]}")
    print(f"Endpoint {endpoint_id} workersMax={workers_max}", flush=True)


async def main() -> None:
    from app.objects.identify_tags import parse_identify_output

    _load_env()
    image = resolve_qwen_image()
    if not image.startswith("ghcr.io/"):
        raise SystemExit(f"Refusing non-GHCR worker image: {image}")
    endpoint_id = _endpoint_id()
    if endpoint_id == os.environ.get("RUNPOD_FACE_ENDPOINT_ID", "").strip():
        raise SystemExit("Refusing to use the face buffalo endpoint")
    headers = _auth()
    rows = _rows()
    print(
        f"Serverless identify+caption n={len(rows)} endpoint={endpoint_id} "
        f"prompt_chars={len(IDENTIFY_AND_CAPTION_PROMPT)}",
        flush=True,
    )

    with httpx.Client(timeout=60.0) as sync:
        ep = sync.get(f"{REST}/endpoints/{endpoint_id}", headers=headers)
        if ep.status_code >= 400:
            raise SystemExit(f"Get endpoint failed {ep.status_code}: {ep.text[:400]}")
        info = ep.json() or {}
        name = info.get("name") or ""
        if name == FACE_ENDPOINT_NAME:
            raise SystemExit("Refusing to use the face buffalo endpoint")
        print(
            f"Using {name} workersMin={info.get('workersMin')} "
            f"workersMax={info.get('workersMax')} scaler={info.get('scalerType')}",
            flush=True,
        )
        _scale(sync, headers, endpoint_id, 1)

    payload = {
        "prompt": IDENTIFY_AND_CAPTION_PROMPT,
        "max_tokens": 1536,
        "images": [
            {
                "index": row["i"],
                "name": row["name"],
                "image_b64": base64.b64encode(Path(row["jpeg_path"]).read_bytes()).decode(
                    "ascii"
                ),
            }
            for row in rows
        ],
    }
    timeout = httpx.Timeout(60.0, connect=30.0)
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            print("Healthcheck (cold start may load weights)", flush=True)
            health = await _run_job(client, endpoint_id, {"healthcheck": True}, 1800.0)
            print(f"health ok={health.get('ok')} models={bool(health.get('models'))}", flush=True)
            if not health.get("ok"):
                raise SystemExit(f"Healthcheck failed: {health}")
            print("Submitting 10-image mixed-prompt batch", flush=True)
            out = await _run_job(client, endpoint_id, payload, 900.0)
    finally:
        with httpx.Client(timeout=30.0) as sync:
            _scale(sync, headers, endpoint_id, 0)

    wall = time.perf_counter() - t0
    raw_rows = list(out.get("results") or [])
    by_i = {int(row["i"]): row for row in rows}
    results = []
    for raw in raw_rows:
        idx = int(raw.get("index") or 0)
        src = by_i.get(idx) or {}
        parsed = parse_identify_output(str(raw.get("raw_text") or ""))
        err = raw.get("error")
        results.append(
            {
                "i": idx,
                "drive_file_id": src.get("drive_file_id"),
                "name": src.get("name") or raw.get("name"),
                "jpeg_bytes": src.get("jpeg_bytes"),
                "identify_s": raw.get("identify_s"),
                "objects": [item.label for item in parsed.objects],
                "actions": [item.label for item in parsed.actions],
                "caption": parsed.caption,
                "raw_text": raw.get("raw_text") or "",
                "usage": raw.get("usage"),
                "error": err,
            }
        )
        print(
            f"[{idx}] {results[-1]['name']}: "
            f"obj={len(parsed.objects)} act={len(parsed.actions)} "
            f"cap_words={len(parsed.caption.split())} err={err}",
            flush=True,
        )
        if parsed.caption:
            print(f"    caption: {parsed.caption[:220]}", flush=True)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = OUT_DIR / f"qwen_serverless_ten_{stamp}.json"
    summary = {
        "runtime": "sglang-serverless",
        "endpoint_id": endpoint_id,
        "postgres_writes": False,
        "pod_created": False,
        "images": len(results),
        "errors": sum(1 for row in results if row["error"]),
        "wall_s": round(wall, 2),
        "prompt": "IDENTIFY_AND_CAPTION_PROMPT",
        "mean_objects": round(
            sum(len(row["objects"]) for row in results) / max(1, len(results)), 2
        ),
        "mean_actions": round(
            sum(len(row["actions"]) for row in results) / max(1, len(results)), 2
        ),
        "mean_caption_words": round(
            sum(len(str(row["caption"]).split()) for row in results) / max(1, len(results)),
            1,
        ),
    }
    dest.write_text(json.dumps({"summary": summary, "results": results}, indent=2))
    print(json.dumps(summary, indent=2), flush=True)
    print(f"Wrote {dest}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
