#!/usr/bin/env python3
"""Production retrieve → Qwen identify those hits → experimental overlay.

1. Call live production GET /search (Railway by default). Does not change it.
2. Download JPEG bytes for the returned images (Drive cache / local prefetch).
3. Identify objects + actions on a Secure Cloud SGLang pod.
4. Write runpod/qwen-vl/results/experimental_overlay.json for /search/testv2 only.
5. Re-rank that same shortlist with Qwen labels (experimental search).

No Postgres writes. Face buffalo endpoint is never used.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import importlib.util
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from app.objects.identify_tags import (  # noqa: E402
    EXPERIMENTAL_OVERLAY_NAME,
    IDENTIFY_PROMPT,
    IdentifyResult,
    overlay_from_payload,
    parse_identify_output,
    persist_rows,
    rerank_shortlist_with_identify,
)

DEFAULT_BASE = "https://dfi-backend-production.up.railway.app"
DEFAULT_QUERIES = [
    "person handing over a ceremonial cheque",
    "students cooking food in a campus kitchen",
    "people wearing a navy blazer on stage",
    "hyrox delhi masters union race backdrop",
    "person speaking into a shure microphone",
    "students presenting a trophy during the ceremony",
]
IMAGE_DIR = REPO / "runpod" / "qwen-vl" / "prod-hits"
OUT_DIR = REPO / "runpod" / "qwen-vl" / "results"
MAX_EDGE = 1024
FETCH_CONCURRENCY = 6
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
MODEL = "Qwen/Qwen3-VL-8B-Instruct"


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


def _identify_mod():
    spec = importlib.util.spec_from_file_location(
        "runpod_qwen_sglang_identify",
        REPO / "scripts" / "runpod_qwen_sglang_identify.py",
    )
    if spec is None or spec.loader is None:
        raise SystemExit("Cannot load runpod_qwen_sglang_identify.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _jpeg(image_bgr):
    import cv2

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


def _existing_prefetch() -> dict[str, Path]:
    manifest = REPO / "runpod" / "qwen-vl" / "images" / "manifest.json"
    found: dict[str, Path] = {}
    if not manifest.is_file():
        return found
    try:
        payload = json.loads(manifest.read_text())
    except (OSError, json.JSONDecodeError):
        return found
    for row in payload.get("images") or []:
        fid = str(row.get("drive_file_id") or "").strip()
        rel = row.get("path")
        if not fid or not rel:
            continue
        path = REPO / rel
        if path.is_file():
            found[fid] = path
    return found


def fetch_production_search(
    client: httpx.Client,
    base: str,
    query: str,
    *,
    captions: bool,
    rerank: bool,
    mime: str,
) -> dict:
    params = {"q": query, "mime": mime, "rerank": str(rerank).lower()}
    if captions:
        params["captions"] = "true"
    resp = client.get(f"{base}/search", params=params, timeout=180.0)
    resp.raise_for_status()
    return resp.json()


def production_image_files(payload: dict, top: int) -> list[dict]:
    files: list[dict] = []
    seen: set[str] = set()
    for item in payload.get("files") or []:
        mime = str(item.get("mime_type") or "")
        if not mime.startswith("image/"):
            continue
        fid = str(item.get("drive_file_id") or "").strip()
        if not fid or fid in seen:
            continue
        seen.add(fid)
        files.append(
            {
                "drive_file_id": fid,
                "name": item.get("name") or fid,
                "score": item.get("score"),
                "caption": item.get("caption"),
                "matched_objects": item.get("matched_objects") or [],
            }
        )
        if len(files) >= top:
            break
    return files


async def _download_one(
    index: int,
    fid: str,
    name: str,
    dest: Path,
    sem: asyncio.Semaphore,
    prefetch: dict[str, Path],
) -> dict:
    async with sem:
        if dest.is_file() and dest.stat().st_size > 512:
            return {
                "index": index,
                "drive_file_id": fid,
                "name": name,
                "path": str(dest.relative_to(REPO)),
                "bytes": dest.stat().st_size,
                "source": "cache",
            }
        cached = prefetch.get(fid)
        if cached is not None and cached.is_file():
            dest.write_bytes(cached.read_bytes())
            return {
                "index": index,
                "drive_file_id": fid,
                "name": name,
                "path": str(dest.relative_to(REPO)),
                "bytes": dest.stat().st_size,
                "source": "prefetch",
            }
        from app.config import get_settings
        from app.dependencies import get_drive_client
        from app.pipelines.common import decode_image_bgr, download_to_temp_file

        settings = get_settings()
        client = get_drive_client()
        suffix = Path(name).suffix or ".bin"
        async with download_to_temp_file(client, fid, settings, suffix=suffix) as path:
            raw = Path(path).read_bytes()
        image = decode_image_bgr(raw, file_name=name)
        jpeg = _jpeg(image)
        dest.write_bytes(jpeg)
        print(f"  downloaded {name} ({len(jpeg)}B)", flush=True)
        return {
            "index": index,
            "drive_file_id": fid,
            "name": name,
            "path": str(dest.relative_to(REPO)),
            "bytes": len(jpeg),
            "source": "drive",
        }


def _payload(jpeg: bytes) -> dict:
    b64 = base64.b64encode(jpeg).decode("ascii")
    return {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                    {"type": "text", "text": IDENTIFY_PROMPT},
                ],
            }
        ],
        "max_tokens": 1024,
        "temperature": 0.1,
    }


def _label_dump(parsed: IdentifyResult) -> tuple[list[dict], list[dict]]:
    objects = [
        {"label": item.label, "synonyms": list(item.synonyms)}
        for item in parsed.objects
    ]
    actions = [
        {"label": item.label, "synonyms": list(item.synonyms)}
        for item in parsed.actions
    ]
    return objects, actions


async def _identify_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    base: str,
    row: dict,
    jpeg: bytes,
    done: list[int],
    total: int,
) -> dict:
    async with sem:
        t0 = time.perf_counter()
        try:
            resp = await client.post(f"{base}/v1/chat/completions", json=_payload(jpeg))
            elapsed = time.perf_counter() - t0
            if resp.status_code >= 400:
                raise RuntimeError(f"Identify failed {resp.status_code}: {resp.text[:500]}")
            raw = resp.json()
            text = (
                ((raw.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            ).strip()
            parsed = parse_identify_output(text)
            err = None
        except Exception as exc:  # noqa: BLE001
            elapsed = time.perf_counter() - t0
            raw = {}
            text = ""
            parsed = IdentifyResult()
            err = str(exc)[:300]
        done[0] += 1
        objects, actions = _label_dump(parsed)
        print(
            f"[{done[0]}/{total}] {row['name']}: "
            f"obj={len(objects)} act={len(actions)} {elapsed:.2f}s",
            flush=True,
        )
        return {
            "index": row["index"],
            "drive_file_id": row["drive_file_id"],
            "name": row["name"],
            "path": row["path"],
            "identify_s": round(elapsed, 3),
            "objects": objects,
            "actions": actions,
            "tags": [item["label"] for item in objects + actions],
            "raw_text": text,
            "error": err,
            "usage": raw.get("usage"),
        }


def _write_overlay(results: list[dict], extra: dict) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "summary": {
            **extra,
            "at": datetime.now(tz=timezone.utc).isoformat(),
            "model": MODEL,
            "postgres_writes": False,
            "production_search_unchanged": True,
            "images": len(results),
            "errors": sum(1 for row in results if row.get("error")),
        },
        "results": results,
    }
    overlay_path = OUT_DIR / EXPERIMENTAL_OVERLAY_NAME
    overlay_path.write_text(json.dumps(payload, indent=2))
    stamped = OUT_DIR / f"prod_hits_identify_{stamp}.json"
    stamped.write_text(json.dumps(payload, indent=2))
    print(f"Wrote experimental overlay {overlay_path}", flush=True)
    print(f"Wrote {stamped}", flush=True)
    return overlay_path


def _compare(queries: dict[str, list[dict]], overlay: dict) -> dict:
    comparison = {}
    for query, files in queries.items():
        experimental = rerank_shortlist_with_identify(query, files, overlay)
        prod_ids = [item["drive_file_id"] for item in files]
        exp_ids = [item["drive_file_id"] for item in experimental]
        matched = [item for item in experimental if item.get("qwen_match")]
        comparison[query] = {
            "production_count": len(files),
            "qwen_match_count": len(matched),
            "production_top10": prod_ids[:10],
            "experimental_top10": exp_ids[:10],
            "experimental_matches": [
                {
                    "drive_file_id": item["drive_file_id"],
                    "name": item.get("name"),
                    "qwen_objects": item.get("qwen_objects") or [],
                    "qwen_actions": item.get("qwen_actions") or [],
                }
                for item in matched[:12]
            ],
        }
    return comparison


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--search-base", default=os.environ.get("DFI_BASE_URL", DEFAULT_BASE))
    parser.add_argument("--query", action="append", default=[])
    parser.add_argument("--top", type=int, default=24)
    parser.add_argument("--captions", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--rerank", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--mime", default="all")
    parser.add_argument("--skip-identify", action="store_true")
    parser.add_argument("--identify-only", action="store_true")
    parser.add_argument("--cloud", default="SECURE", choices=("SECURE", "COMMUNITY"))
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--wait-s", type=float, default=1800.0)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    queries = args.query or list(DEFAULT_QUERIES)

    _load_repo_env()
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = args.search_base.rstrip("/")
    print(
        f"Production search {base} captions={args.captions} rerank={args.rerank} "
        f"top={args.top} queries={len(queries)}",
        flush=True,
    )

    production_path = IMAGE_DIR / "production_search.json"
    if args.identify_only:
        if not production_path.is_file():
            raise SystemExit(f"Missing {production_path}; run without --identify-only first")
        saved = json.loads(production_path.read_text())
        production = saved.get("production") or {}
        downloaded = saved.get("images") or []
        queries = saved.get("queries") or queries
        print(
            f"Reusing {len(downloaded)} cached production hits from {production_path.name}",
            flush=True,
        )
    else:
        production = {}
        with httpx.Client(follow_redirects=True, timeout=180.0) as client:
            for query in queries:
                print(f"\n== production /search  {query!r}", flush=True)
                t0 = time.perf_counter()
                payload = fetch_production_search(
                    client,
                    base,
                    query,
                    captions=args.captions,
                    rerank=args.rerank,
                    mime=args.mime,
                )
                files = production_image_files(payload, args.top)
                production[query] = files
                print(
                    f"  {len(files)} images in {time.perf_counter() - t0:.1f}s "
                    f"(answer={(payload.get('answer') or '')[:80]!r})",
                    flush=True,
                )
                for i, item in enumerate(files[:8], start=1):
                    print(f"    {i:02d}. {item['name'][:70]}", flush=True)

        unique: dict[str, dict] = {}
        for query, files in production.items():
            for item in files:
                fid = item["drive_file_id"]
                row = unique.setdefault(
                    fid,
                    {
                        "drive_file_id": fid,
                        "name": item["name"],
                        "queries": [],
                    },
                )
                row["queries"].append(query)

        print(f"\nUnique production-hit images: {len(unique)}", flush=True)
        prefetch = _existing_prefetch()
        sem = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def _download_all() -> list[dict]:
            tasks = []
            for index, (fid, row) in enumerate(unique.items(), start=1):
                safe = _SAFE.sub("_", Path(row["name"]).stem)[:40] or fid[:12]
                dest = IMAGE_DIR / f"{index:03d}_{safe}.jpg"
                tasks.append(_download_one(index, fid, row["name"], dest, sem, prefetch))
            return await asyncio.gather(*tasks)

        downloaded = asyncio.run(_download_all())
        downloaded.sort(key=lambda row: int(row["index"]))
        production_path.write_text(
            json.dumps(
                {
                    "search_base": base,
                    "queries": queries,
                    "captions": args.captions,
                    "rerank": args.rerank,
                    "top": args.top,
                    "production": production,
                    "images": downloaded,
                },
                indent=2,
            )
        )
        (IMAGE_DIR / "manifest.json").write_text(
            json.dumps({"images": downloaded, "max_edge": MAX_EDGE, "queries": queries}, indent=2)
        )
        print(f"Cached {len(downloaded)} JPEGs under {IMAGE_DIR}", flush=True)

    if args.skip_identify:
        print("Skipping Qwen identify (--skip-identify)", flush=True)
        return 0

    qwen = _identify_mod()
    headers = qwen._auth()
    qwen._scale_serverless_zero(headers)
    pod_id = ""
    with httpx.Client(timeout=60.0) as client:
        existing_id = ""
        if qwen.POD_FILE.is_file():
            existing_id = qwen.POD_FILE.read_text().strip()
        if existing_id:
            print(f"Terminating previous pod {existing_id}", flush=True)
            qwen._terminate_pod(client, headers, existing_id)
        pod = qwen._create_pod(client, headers, args.cloud)
        pod_id = pod["id"]
        qwen.POD_FILE.write_text(pod_id + "\n")
        print(
            f"Created pod {pod_id} cloud={pod.get('cloudType')} ${pod.get('costPerHr')}/hr",
            flush=True,
        )
        try:
            deadline = time.time() + 300
            while time.time() < deadline:
                pod = qwen._get_pod(client, headers, pod_id)
                status = pod.get("desiredStatus")
                print(f"  pod {status}", flush=True)
                if status == "RUNNING":
                    break
                if status in {"EXITED", "TERMINATED"}:
                    raise SystemExit(f"Pod ended early: {status}")
                time.sleep(8)
            else:
                raise SystemExit("Pod did not reach RUNNING")

            proxy = qwen._proxy_url(pod_id)
            print(f"Waiting for SGLang at {proxy}", flush=True)
            models = qwen._wait_models(proxy, args.wait_s)
            print(json.dumps({"models": models}, indent=2)[:500], flush=True)

            done = [0]
            t_all = time.perf_counter()

            async def _run_identify() -> list[dict]:
                timeout = httpx.Timeout(180.0)
                limits = httpx.Limits(
                    max_connections=max(16, args.concurrency + 4),
                    max_keepalive_connections=args.concurrency,
                )
                sem_id = asyncio.Semaphore(max(1, args.concurrency))
                async with httpx.AsyncClient(
                    timeout=timeout, follow_redirects=True, limits=limits
                ) as aclient:
                    tasks = [
                        _identify_one(
                            aclient,
                            sem_id,
                            proxy,
                            row,
                            (REPO / row["path"]).read_bytes(),
                            done,
                            len(downloaded),
                        )
                        for row in downloaded
                    ]
                    return await asyncio.gather(*tasks)

            results = asyncio.run(_run_identify())
            results.sort(key=lambda row: int(row["index"]))
            wall = round(time.perf_counter() - t_all, 2)
            extra = {
                "runtime": "sglang-pod-prod-hits",
                "pod_id": pod_id,
                "search_base": base,
                "queries": queries,
                "captions": args.captions,
                "rerank": args.rerank,
                "top": args.top,
                "concurrency": args.concurrency,
                "wall_s": wall,
                "production": {
                    query: [item["drive_file_id"] for item in files]
                    for query, files in production.items()
                },
            }
            overlay_path = _write_overlay(results, extra)
            overlay = overlay_from_payload(json.loads(overlay_path.read_text()))
            comparison = _compare(production, overlay)
            extra["comparison"] = comparison
            overlay_path.write_text(
                json.dumps(
                    {**json.loads(overlay_path.read_text()), "comparison": comparison},
                    indent=2,
                )
            )
            print(json.dumps({"wall_s": wall, "comparison": comparison}, indent=2), flush=True)
        finally:
            if pod_id and not args.keep:
                qwen._terminate_pod(client, headers, pod_id)
            elif pod_id:
                print(f"Keeping pod {pod_id}", flush=True)

    print(
        "Experimental overlay ready for /search/testv2. Production /search was not modified.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
