#!/usr/bin/env python3
"""Identify objects on the local 100-image cache via serverless Qwen3-VL.

Sends cached JPEG bytes. No Drive URLs, no Postgres writes.
Does not touch the face buffalo endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
sys.path.insert(0, str(REPO / "runpod" / "qwen-vl"))
from image_source import require_registry_auth, resolve_qwen_image  # noqa: E402
from tags import IDENTIFY_AND_CAPTION_PROMPT, filter_tags  # noqa: E402

IMAGE_DIR = REPO / "runpod" / "qwen-vl" / "images"
MANIFEST = IMAGE_DIR / "manifest.json"
OUT_DIR = REPO / "runpod" / "qwen-vl" / "results"
ENDPOINT_FILE = REPO / "runpod" / "qwen-vl" / ".endpoint_id"
POD_FILE = REPO / "runpod" / "qwen-vl" / ".pod_id"
REST = "https://rest.runpod.io/v1"
MODEL = "Qwen/Qwen3-VL-8B-Instruct"
FACE_ENDPOINT_NAME = "dfi-face-buffalo"
GPU_TYPE_IDS = [
    "NVIDIA A40",
    "NVIDIA RTX A6000",
    "NVIDIA L40",
    "NVIDIA RTX 6000 Ada Generation",
]


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


def _endpoint_id(cli_id: str) -> str:
    if cli_id.strip():
        return cli_id.strip()
    env_id = os.environ.get("RUNPOD_QWEN_ENDPOINT_ID", "").strip()
    if env_id:
        return env_id
    if ENDPOINT_FILE.is_file():
        return ENDPOINT_FILE.read_text().strip()
    raise SystemExit("No endpoint id; run scripts/runpod_create_qwen_endpoint.py")


def _assert_not_face(endpoint_id: str) -> None:
    if endpoint_id == os.environ.get("RUNPOD_FACE_ENDPOINT_ID", "").strip():
        raise SystemExit("Refusing to use the face buffalo endpoint for Qwen identify")


async def _run_job(client: httpx.AsyncClient, endpoint_id: str, payload: dict, timeout_s: float) -> dict:
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
            await asyncio.sleep(2.0)
            continue
        if status_resp.status_code >= 400:
            raise RuntimeError(f"status HTTP {status_resp.status_code}: {status_resp.text[:500]}")
        data = status_resp.json()
        last = str(data.get("status") or "")
        if last.lower() in {"completed"}:
            return data.get("output") or {}
        if last.lower() in {"failed", "cancelled", "timed_out"}:
            raise RuntimeError(f"job {job_id} {last}: {data.get('error') or data}")
        await asyncio.sleep(1.0)
    raise RuntimeError(f"job {job_id} still {last} after {timeout_s:.0f}s")


async def _identify_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    endpoint_id: str,
    row: dict,
    jpeg: bytes,
    done: list[int],
    total: int,
    timeout_s: float,
) -> dict:
    async with sem:
        t0 = time.perf_counter()
        try:
            out = await _run_job(
                client,
                endpoint_id,
                {"image_b64": base64.b64encode(jpeg).decode("ascii"), "name": row["name"], "prompt": IDENTIFY_AND_CAPTION_PROMPT, "max_tokens": 1536},
                timeout_s,
            )
            elapsed = time.perf_counter() - t0
            if out.get("error"):
                raise RuntimeError(out["error"])
            raw_text = str(out.get("raw_text") or "")
            tags = list(out.get("tags") or filter_tags(raw_text))
            err = None
        except Exception as exc:  # noqa: BLE001
            elapsed = time.perf_counter() - t0
            raw_text = ""
            tags = []
            err = str(exc)[:300]
        done[0] += 1
        print(
            f"[{done[0]}/{total}] {row['name']}: tags={len(tags)} {elapsed:.2f}s {tags[:8]}",
            flush=True,
        )
        return {
            "index": row["index"],
            "drive_file_id": row["drive_file_id"],
            "name": row["name"],
            "path": row["path"],
            "identify_s": round(elapsed, 3),
            "tags": tags,
            "raw_text": raw_text,
            "error": err,
        }


async def _identify_batch(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    endpoint_id: str,
    chunk: list[dict],
    done: list[int],
    total: int,
    timeout_s: float,
) -> list[dict]:
    payload = {
        "prompt": IDENTIFY_AND_CAPTION_PROMPT,
        "max_tokens": 1536,
        "images": [
            {
                "index": row["index"],
                "name": row["name"],
                "image_b64": base64.b64encode((REPO / row["path"]).read_bytes()).decode("ascii"),
            }
            for row in chunk
        ]
    }
    by_index = {int(row["index"]): row for row in chunk}
    async with sem:
        t0 = time.perf_counter()
        try:
            out = await _run_job(client, endpoint_id, payload, timeout_s)
            elapsed = time.perf_counter() - t0
            raw_rows = list(out.get("results") or [])
            if not raw_rows:
                raise RuntimeError(out.get("error") or f"empty batch: {str(out)[:300]}")
        except Exception as exc:  # noqa: BLE001
            elapsed = time.perf_counter() - t0
            raw_rows = [
                {"index": row["index"], "error": str(exc)[:300], "tags": [], "raw_text": ""}
                for row in chunk
            ]
    results = []
    for raw in raw_rows:
        idx = int(raw.get("index") or 0)
        src = by_index.get(idx) or {}
        tags = list(raw.get("tags") or [])
        err = raw.get("error")
        done[0] += 1
        name = src.get("name") or raw.get("name") or str(idx)
        print(
            f"[{done[0]}/{total}] {name}: tags={len(tags)} {elapsed:.2f}s {tags[:8]}",
            flush=True,
        )
        results.append(
            {
                "index": idx,
                "drive_file_id": src.get("drive_file_id"),
                "name": name,
                "path": src.get("path"),
                "identify_s": round(float(raw.get("identify_s") or elapsed), 3),
                "tags": tags,
                "raw_text": raw.get("raw_text") or "",
                "error": err,
            }
        )
    return results


def _proxy_url(pod_id: str) -> str:
    return f"https://{pod_id}-8000.proxy.runpod.net"


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
                    {"type": "text", "text": IDENTIFY_AND_CAPTION_PROMPT},
                ],
            }
        ],
        "max_tokens": 1024,
        "temperature": 0.1,
    }


def _create_pod(client: httpx.Client, headers: dict[str, str], cloud: str) -> dict:
    image = resolve_qwen_image()
    try:
        auth_id = require_registry_auth(image)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    body = {
        "name": "dfi-qwen3-vl-sglang",
        "imageName": image,
        "gpuTypeIds": GPU_TYPE_IDS,
        "gpuTypePriority": "custom",
        "gpuCount": 1,
        "containerDiskInGb": 80,
        "volumeInGb": 0,
        "ports": ["8000/http"],
        "cloudType": cloud,
        "computeType": "GPU",
        "allowedCudaVersions": ["12.4", "12.6", "12.8", "12.9"],
        "dockerEntrypoint": ["/app/start.sh"],
        "dockerStartCmd": [],
        "containerRegistryAuthId": auth_id,
        "env": {
            "HF_XET_HIGH_PERFORMANCE": "1",
            "SGLANG_VLM_CACHE_SIZE_MB": "0",
            "QWEN_SERVERLESS": "0",
        },
    }
    resp = client.post(f"{REST}/pods", headers=headers, json=body)
    if resp.status_code >= 400:
        raise SystemExit(f"Create pod {cloud} failed {resp.status_code}: {resp.text[:800]}")
    return resp.json()


def _get_pod(client: httpx.Client, headers: dict[str, str], pod_id: str) -> dict:
    resp = client.get(f"{REST}/pods/{pod_id}", headers=headers)
    if resp.status_code >= 400:
        raise SystemExit(f"Get pod failed {resp.status_code}: {resp.text[:400]}")
    return resp.json()


def _terminate_pod(client: httpx.Client, headers: dict[str, str], pod_id: str) -> None:
    resp = client.delete(f"{REST}/pods/{pod_id}", headers=headers)
    if resp.status_code >= 400:
        print(f"Terminate failed {resp.status_code}: {resp.text[:400]}", file=sys.stderr)
        return
    print(f"Terminated pod {pod_id}", flush=True)


def _wait_models(base: str, timeout_s: float) -> dict:
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=20.0, follow_redirects=True) as c:
                resp = c.get(f"{base}/v1/models")
                last = f"{resp.status_code} {resp.text[:180]}"
                if resp.status_code == 200:
                    return resp.json()
        except Exception as exc:  # noqa: BLE001
            last = str(exc)[:200]
        print(f"  waiting for SGLang /v1/models ({last})", flush=True)
        time.sleep(20)
    raise SystemExit(f"Timed out waiting for SGLang. Last: {last}")


async def _identify_pod_one(
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
            tags = filter_tags(text)
            err = None
        except Exception as exc:  # noqa: BLE001
            elapsed = time.perf_counter() - t0
            raw = {}
            text = ""
            tags = []
            err = str(exc)[:300]
        done[0] += 1
        print(
            f"[{done[0]}/{total}] {row['name']}: tags={len(tags)} {elapsed:.2f}s {tags[:8]}",
            flush=True,
        )
        return {
            "index": row["index"],
            "drive_file_id": row["drive_file_id"],
            "name": row["name"],
            "path": row["path"],
            "identify_s": round(elapsed, 3),
            "tags": tags,
            "raw_text": text,
            "error": err,
            "usage": raw.get("usage"),
        }


def _scale_serverless_zero(headers: dict[str, str]) -> None:
    if not ENDPOINT_FILE.is_file():
        return
    eid = ENDPOINT_FILE.read_text().strip()
    if not eid:
        return
    with httpx.Client(timeout=30.0) as client:
        _scale(client, headers, eid, 0)


def _write_summary(results: list[dict], extra: dict) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    counts: Counter[str] = Counter()
    times = [float(r["identify_s"]) for r in results if not r["error"]]
    wall = extra["wall_s"]
    for row in results:
        for tag in row["tags"]:
            counts[tag] += 1
    summary = {
        **extra,
        "at": datetime.now(tz=timezone.utc).isoformat(),
        "model": MODEL,
        "postgres_writes": False,
        "images": len(results),
        "errors": sum(1 for r in results if r["error"]),
        "mean_s": round(sum(times) / len(times), 3) if times else None,
        "p50_s": round(sorted(times)[len(times) // 2], 3) if times else None,
        "img_per_s": round(len(times) / wall, 3) if wall else None,
        "unique_tags": len(counts),
        "top_tags": counts.most_common(40),
    }
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = OUT_DIR / f"qwen_sglang_identify_{stamp}.json"
    out.write_text(json.dumps({"summary": summary, "results": results}, indent=2))
    print(json.dumps(summary, indent=2), flush=True)
    print(f"Wrote {out}", flush=True)
    return out


def _run_pod(args: argparse.Namespace) -> None:
    headers = _auth()
    _scale_serverless_zero(headers)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    POD_FILE.parent.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=60.0) as client:
        existing_id = (args.pod_id or "").strip()
        if not existing_id and POD_FILE.is_file():
            existing_id = POD_FILE.read_text().strip()
        if args.terminate_only:
            if not existing_id:
                raise SystemExit("No pod id to terminate")
            _terminate_pod(client, headers, existing_id)
            return
        if existing_id and not args.pod_id:
            print(f"Terminating previous pod {existing_id}", flush=True)
            _terminate_pod(client, headers, existing_id)
        if args.pod_id:
            pod_id = args.pod_id
            pod = _get_pod(client, headers, pod_id)
            print(f"Reusing pod {pod_id} status={pod.get('desiredStatus')}", flush=True)
        else:
            try:
                pod = _create_pod(client, headers, args.cloud)
            except SystemExit as exc:
                if args.cloud == "COMMUNITY":
                    print(f"{exc}; retrying SECURE", flush=True)
                    pod = _create_pod(client, headers, "SECURE")
                else:
                    raise
            pod_id = pod["id"]
            POD_FILE.write_text(pod_id + "\n")
            print(
                f"Created pod {pod_id} cloud={pod.get('cloudType')} "
                f"${pod.get('costPerHr')}/hr",
                flush=True,
            )

        try:
            deadline = time.time() + 300
            while time.time() < deadline:
                pod = _get_pod(client, headers, pod_id)
                status = pod.get("desiredStatus")
                print(f"  pod {status}", flush=True)
                if status == "RUNNING":
                    break
                if status in {"EXITED", "TERMINATED"}:
                    raise SystemExit(f"Pod ended early: {status}")
                time.sleep(8)
            else:
                raise SystemExit("Pod did not reach RUNNING")

            base = _proxy_url(pod_id)
            print(f"Waiting for SGLang at {base}", flush=True)
            models = _wait_models(base, args.wait_s)
            print(json.dumps({"models": models}, indent=2)[:600], flush=True)

            if not MANIFEST.is_file():
                raise SystemExit(f"Missing {MANIFEST}; run scripts/prefetch_qwen_identify_images.py")
            rows = json.loads(MANIFEST.read_text())["images"][: args.limit]
            print(
                f"Identifying {len(rows)} local JPEGs on pod concurrency={args.concurrency} "
                f"(SGLang max-running-requests=32)",
                flush=True,
            )
            sem = asyncio.Semaphore(max(1, args.concurrency))
            done = [0]
            t_all = time.perf_counter()

            async def _run() -> list[dict]:
                timeout = httpx.Timeout(180.0)
                limits = httpx.Limits(
                    max_connections=max(16, args.concurrency + 4),
                    max_keepalive_connections=args.concurrency,
                )
                async with httpx.AsyncClient(
                    timeout=timeout, follow_redirects=True, limits=limits
                ) as aclient:
                    tasks = [
                        _identify_pod_one(
                            aclient,
                            sem,
                            base,
                            row,
                            (REPO / row["path"]).read_bytes(),
                            done,
                            len(rows),
                        )
                        for row in rows
                    ]
                    return await asyncio.gather(*tasks)

            results = asyncio.run(_run())
            results.sort(key=lambda r: int(r["index"]))
            _write_summary(
                results,
                {
                    "runtime": "sglang-pod",
                    "pod_id": pod_id,
                    "concurrency": args.concurrency,
                    "wall_s": round(time.perf_counter() - t_all, 2),
                },
            )
        finally:
            if not args.keep:
                _terminate_pod(client, headers, pod_id)
            else:
                print(f"Keeping pod {pod_id} at {_proxy_url(pod_id)}", flush=True)


def _scale(client: httpx.Client, headers: dict[str, str], endpoint_id: str, workers_max: int) -> None:
    resp = client.patch(
        f"{REST}/endpoints/{endpoint_id}",
        headers=headers,
        json={"workersMin": 0, "workersMax": workers_max},
    )
    if resp.status_code >= 400:
        print(f"Scale failed {resp.status_code}: {resp.text[:400]}", file=sys.stderr)
        return
    print(f"Endpoint {endpoint_id} workersMax={workers_max}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("pod", "serverless"), default="serverless")
    parser.add_argument("--endpoint-id", default="")
    parser.add_argument("--pod-id", default="")
    parser.add_argument("--cloud", default="COMMUNITY", choices=("COMMUNITY", "SECURE"))
    parser.add_argument("--wait-s", type=float, default=1800.0)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=32,
        help="In-flight SGLang HTTP requests (pod) or serverless jobs",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=25,
        help="Serverless only: images packed per job. Ignored on --backend pod.",
    )
    parser.add_argument("--scale-zero", action="store_true", help="Set serverless workersMax=0 after the run")
    parser.add_argument("--keep", action="store_true", help="Leave the pod or serverless worker running")
    parser.add_argument("--health-only", action="store_true")
    parser.add_argument("--terminate-only", action="store_true")
    args = parser.parse_args()

    _load_env()
    if args.backend == "pod":
        _run_pod(args)
        return
    headers = _auth()
    endpoint_id = _endpoint_id(args.endpoint_id)
    _assert_not_face(endpoint_id)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=60.0) as sync:
        ep = sync.get(f"{REST}/endpoints/{endpoint_id}", headers=headers)
        if ep.status_code >= 400:
            raise SystemExit(f"Get endpoint failed {ep.status_code}: {ep.text[:400]}")
        name = (ep.json() or {}).get("name") or ""
        if name == FACE_ENDPOINT_NAME:
            raise SystemExit("Refusing to use the face buffalo endpoint for Qwen identify")
        print(f"Using serverless endpoint {endpoint_id} name={name}", flush=True)
        _scale(sync, headers, endpoint_id, 1)

    timeout = httpx.Timeout(60.0, connect=30.0)
    limits = httpx.Limits(
        max_connections=max(16, args.concurrency + 4),
        max_keepalive_connections=max(8, args.concurrency),
    )

    async def _boot() -> None:
        async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
            print("Waiting for worker healthcheck (cold start may pull the model)", flush=True)
            out = await _run_job(client, endpoint_id, {"healthcheck": True}, args.wait_s)
            print(json.dumps({"health": out}, indent=2)[:600], flush=True)
            if not out.get("ok"):
                raise SystemExit(f"Healthcheck failed: {out}")

    asyncio.run(_boot())
    if args.health_only:
        return

    if not MANIFEST.is_file():
        raise SystemExit(f"Missing {MANIFEST}; run scripts/prefetch_qwen_identify_images.py")
    rows = json.loads(MANIFEST.read_text())["images"][: args.limit]
    batch_size = max(1, args.batch_size)
    print(
        f"Identifying {len(rows)} local JPEGs jobs={args.concurrency} "
        f"batch_size={batch_size} (SGLang max-running-requests=32)",
        flush=True,
    )
    sem = asyncio.Semaphore(max(1, args.concurrency))
    done = [0]
    t_all = time.perf_counter()
    job_timeout = min(600.0, args.wait_s)

    async def _run() -> list[dict]:
        async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
            if batch_size == 1:
                tasks = [
                    _identify_one(
                        client,
                        sem,
                        endpoint_id,
                        row,
                        (REPO / row["path"]).read_bytes(),
                        done,
                        len(rows),
                        job_timeout,
                    )
                    for row in rows
                ]
                return await asyncio.gather(*tasks)
            chunks = [rows[i : i + batch_size] for i in range(0, len(rows), batch_size)]
            print(f"Submitting {len(chunks)} parallel batch jobs", flush=True)
            nested = await asyncio.gather(
                *[
                    _identify_batch(
                        client,
                        sem,
                        endpoint_id,
                        chunk,
                        done,
                        len(rows),
                        job_timeout,
                    )
                    for chunk in chunks
                ]
            )
            flat: list[dict] = []
            for part in nested:
                flat.extend(part)
            return flat

    try:
        results = asyncio.run(_run())
        results.sort(key=lambda r: int(r["index"]))
        wall = time.perf_counter() - t_all
        counts: Counter[str] = Counter()
        times = [float(r["identify_s"]) for r in results if not r["error"]]
        for row in results:
            for tag in row["tags"]:
                counts[tag] += 1
        summary = {
            "at": datetime.now(tz=timezone.utc).isoformat(),
            "runtime": "sglang-serverless",
            "model": MODEL,
            "endpoint_id": endpoint_id,
            "postgres_writes": False,
            "concurrency": args.concurrency,
            "batch_size": batch_size,
            "images": len(results),
            "errors": sum(1 for r in results if r["error"]),
            "wall_s": round(wall, 2),
            "mean_s": round(sum(times) / len(times), 3) if times else None,
            "p50_s": round(sorted(times)[len(times) // 2], 3) if times else None,
            "img_per_s": round(len(times) / wall, 3) if wall else None,
            "unique_tags": len(counts),
            "top_tags": counts.most_common(40),
        }
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = OUT_DIR / f"qwen_sglang_identify_{stamp}.json"
        out.write_text(json.dumps({"summary": summary, "results": results}, indent=2))
        print(json.dumps(summary, indent=2), flush=True)
        print(f"Wrote {out}", flush=True)
    finally:
        with httpx.Client(timeout=30.0) as sync:
            if args.scale_zero or not args.keep:
                _scale(sync, headers, endpoint_id, 0)
            else:
                print(f"Keeping endpoint {endpoint_id} workersMax=1", flush=True)


if __name__ == "__main__":
    main()
