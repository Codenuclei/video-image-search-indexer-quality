#!/usr/bin/env python3
"""Re-identify the 100 Railway samples on SGLang with full photos + full prompt.

Downloads Drive originals, encodes at production identify size (1024 / q82),
and sends the complete IDENTIFY_PROMPT. No Postgres writes. No Drive URLs
in the GPU payload.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
POD_FILE = REPO / "runpod" / "qwen-vl" / ".pod_id"
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

SAMPLES = Path("/tmp/qwen-pg-identify-canvas/samples.json")
JPEG_DIR = Path("/tmp/qwen-pg-identify-canvas/full-jpegs")
OUT_DIR = REPO / "runpod" / "qwen-vl" / "results"
FETCH_CONCURRENCY = 6
GPU_CONCURRENCY = 16
MAX_DOWNLOAD = 80 * 1024 * 1024


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


def _gap_blob(labels: list[str]) -> str:
    return " ".join(labels).lower()


def _has_grad(labels: list[str]) -> bool:
    blob = _gap_blob(labels)
    return any(tok in blob for tok in ("graduat", "mortarboard", "mortar board"))


def _has_venue(labels: list[str]) -> bool:
    blob = _gap_blob(labels)
    return any(
        tok in blob
        for tok in (
            "restaurant",
            "buffet",
            "cafeteria",
            "cafe",
            "café",
            "dining hall",
            "food stall",
            "coffee shop",
        )
    )


async def _download_full_jpeg(client, item: dict, sem: asyncio.Semaphore, settings) -> Path | None:
    from app.workers.identify_queue import encode_identify_jpeg

    dest = JPEG_DIR / f"{item['drive_file_id']}.jpg"
    if dest.is_file() and dest.stat().st_size > 2000:
        return dest
    async with sem:
        raw = bytearray()
        try:
            async with client.stream_file_content(item["drive_file_id"]) as response:
                async for chunk in response.aiter_bytes(chunk_size=256 * 1024):
                    raw.extend(chunk)
                    if len(raw) > MAX_DOWNLOAD:
                        print(f"skip oversized {item['name']}", flush=True)
                        return None
            jpeg = encode_identify_jpeg(
                bytes(raw),
                file_name=item["name"] or item["drive_file_id"],
                max_edge=settings.qwen_identify_max_edge,
                quality=settings.qwen_identify_jpeg_quality,
                max_bytes=settings.qwen_identify_max_bytes,
            )
            dest.write_bytes(jpeg)
            return dest
        except Exception as exc:  # noqa: BLE001
            print(f"download fail {item['name']}: {type(exc).__name__}: {exc}"[:180], flush=True)
            return None


async def _identify_one(
    http: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    base: str,
    item: dict,
    jpeg_path: Path,
    prompt: str,
    max_tokens: int,
    model: str,
    done: list[int],
    total: int,
) -> dict:
    from app.objects.identify_tags import parse_identify_output
    from app.workers.identify_queue import build_identify_payload

    jpeg = jpeg_path.read_bytes()
    payload = build_identify_payload(
        jpeg,
        model=model,
        prompt=prompt,
        max_tokens=max_tokens,
    )
    async with sem:
        t0 = time.perf_counter()
        try:
            resp = await http.post(
                f"{base}/v1/chat/completions",
                json=payload,
                headers={"Authorization": "Bearer EMPTY"},
            )
            elapsed = time.perf_counter() - t0
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:400]}")
            raw = resp.json()
            text = (
                ((raw.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            ).strip()
            parsed = parse_identify_output(text)
            objects = [x.label for x in parsed.objects]
            actions = [x.label for x in parsed.actions]
            err = None
            usage = raw.get("usage")
        except Exception as exc:  # noqa: BLE001
            elapsed = time.perf_counter() - t0
            text = ""
            objects = []
            actions = []
            err = str(exc)[:300]
            usage = None
    done[0] += 1
    old = list(item.get("objects") or []) + list(item.get("actions") or [])
    new = objects + actions
    added = [tag for tag in new if tag not in old]
    print(
        f"[{done[0]}/{total}] {item['name']}: jpeg={len(jpeg)} obj={len(objects)} "
        f"act={len(actions)} {elapsed:.1f}s added={added[:6]}",
        flush=True,
    )
    return {
        "i": item["i"],
        "drive_file_id": item["drive_file_id"],
        "name": item["name"],
        "jpeg_bytes": len(jpeg),
        "jpeg_path": str(jpeg_path),
        "identify_s": round(elapsed, 3),
        "objects": objects,
        "actions": actions,
        "old_objects": item.get("objects") or [],
        "old_actions": item.get("actions") or [],
        "added": added,
        "raw_text": text,
        "error": err,
        "usage": usage,
        "grad_old": _has_grad(old),
        "grad_new": _has_grad(new),
        "venue_old": _has_venue(old),
        "venue_new": _has_venue(new),
    }


async def main() -> None:
    _load_env()
    from app.config import get_settings
    from app.db.session import get_session_factory
    from app.drive.google_client import DriveDirectClient
    from app.objects.identify_tags import IDENTIFY_PROMPT
    from app.workers.identify_queue import sglang_base_url

    if not SAMPLES.is_file():
        raise SystemExit(f"Missing {SAMPLES}")
    data = json.loads(SAMPLES.read_text())
    items = data["items"]
    settings = get_settings()
    env_base = (os.environ.get("QWEN_IDENTIFY_BASE_URL") or "").rstrip("/")
    settings_base = (sglang_base_url(settings) or "").rstrip("/")
    temp_id = Path("/tmp/qwen-pg-identify-canvas/temp_sglang_pod_id")
    temp_pod = temp_id.read_text().strip() if temp_id.is_file() else ""
    file_pod = POD_FILE.read_text().strip() if POD_FILE.is_file() else ""

    def _ok(url: str) -> bool:
        return bool(url) and "127.0.0.1" not in url and "localhost" not in url

    if _ok(env_base):
        base = env_base
    elif _ok(settings_base):
        base = settings_base
    elif temp_pod:
        base = f"https://{temp_pod}-8000.proxy.runpod.net"
    elif file_pod:
        base = f"https://{file_pod}-8000.proxy.runpod.net"
    else:
        raise SystemExit("No SGLang base URL")
    print(f"sglang {base.split('://', 1)[-1][:40]}…", flush=True)
    prompt = IDENTIFY_PROMPT
    max_tokens = settings.qwen_identify_max_tokens
    model = settings.qwen_identify_model or settings.qwen_vlm_model
    print(
        f"samples={len(items)} max_edge={settings.qwen_identify_max_edge} "
        f"quality={settings.qwen_identify_jpeg_quality} max_tokens={max_tokens} "
        f"prompt_chars={len(prompt)}",
        flush=True,
    )
    JPEG_DIR.mkdir(parents=True, exist_ok=True)
    drive = DriveDirectClient(session_factory=get_session_factory(), settings=settings)
    fetch_sem = asyncio.Semaphore(FETCH_CONCURRENCY)
    paths: dict[str, Path] = {}
    fetch_results = await asyncio.gather(
        *[_download_full_jpeg(drive, item, fetch_sem, settings) for item in items]
    )
    ready: list[tuple[dict, Path]] = []
    for item, path in zip(items, fetch_results, strict=True):
        if path is None:
            continue
        paths[item["drive_file_id"]] = path
        ready.append((item, path))
    print(f"full jpegs ready {len(ready)}/{len(items)}", flush=True)
    if not ready:
        raise SystemExit("No JPEGs downloaded")

    timeout = httpx.Timeout(180.0)
    gpu_sem = asyncio.Semaphore(GPU_CONCURRENCY)
    done = [0]
    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
        probe = await http.get(f"{base}/v1/models", headers={"Authorization": "Bearer EMPTY"})
        if probe.status_code >= 400:
            raise SystemExit(f"SGLang /v1/models HTTP {probe.status_code}")
        results = await asyncio.gather(
            *[
                _identify_one(
                    http,
                    gpu_sem,
                    base,
                    item,
                    path,
                    prompt,
                    max_tokens,
                    model,
                    done,
                    len(ready),
                )
                for item, path in ready
            ]
        )
    wall = time.perf_counter() - t0
    ok = [r for r in results if not r["error"]]
    summary = {
        "runtime": "sglang-pod-railway100-prompt-v2-fulljpeg",
        "postgres_writes": False,
        "n": len(results),
        "errors": sum(1 for r in results if r["error"]),
        "wall_s": round(wall, 2),
        "mean_s": round(sum(r["identify_s"] for r in ok) / len(ok), 3) if ok else None,
        "max_edge": settings.qwen_identify_max_edge,
        "jpeg_quality": settings.qwen_identify_jpeg_quality,
        "max_tokens": max_tokens,
        "prompt_chars": len(prompt),
        "mean_jpeg_bytes": int(sum(r["jpeg_bytes"] for r in ok) / len(ok)) if ok else None,
        "grad_old": sum(1 for r in results if r["grad_old"]),
        "grad_new": sum(1 for r in results if r["grad_new"]),
        "grad_gain": sum(1 for r in results if r["grad_new"] and not r["grad_old"]),
        "venue_old": sum(1 for r in results if r["venue_old"]),
        "venue_new": sum(1 for r in results if r["venue_new"]),
        "venue_gain": sum(1 for r in results if r["venue_new"] and not r["venue_old"]),
        "at": datetime.now(tz=timezone.utc).isoformat(),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = OUT_DIR / f"qwen_identify_railway100_v2_{stamp}.json"
    out.write_text(json.dumps({"summary": summary, "results": results}, indent=2))
    print(json.dumps(summary, indent=2), flush=True)
    print(f"Wrote {out}", flush=True)
    compare = Path("/tmp/qwen-pg-identify-canvas/retest_v2.json")
    compare.write_text(json.dumps({"summary": summary, "results": results}))
    print(f"Wrote {compare}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
