#!/usr/bin/env python3
"""One-shot Qwen3-VL identify load test on a RunPod GPU Pod.

Uses the baked GHCR SGLang image (not Docker Hub vLLM). We rent a pod, wait
until weights are loaded, send one image identify request, then terminate.

No Postgres writes. Face serverless endpoint is left untouched.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
OUT_DIR = REPO / "runpod" / "qwen-vl" / "results"
POD_FILE = REPO / "runpod" / "qwen-vl" / ".pod_id"
REST = "https://rest.runpod.io/v1"
MODEL = "Qwen/Qwen3-VL-8B-Instruct"
sys.path.insert(0, str(REPO / "runpod" / "qwen-vl"))
from image_source import require_registry_auth, resolve_qwen_image  # noqa: E402
DEMO_JPEG = "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen-VL/assets/demo.jpeg"
IDENTIFY_PROMPT = (
    "List every distinct object, garment, brand, and sign you can see. "
    "Nouns only. No bounding boxes. One item per line."
)
GPU_TYPE_IDS = [
    "NVIDIA A40",
    "NVIDIA RTX A6000",
    "NVIDIA L40",
    "NVIDIA RTX 6000 Ada Generation",
    "NVIDIA RTX A5000",
    "NVIDIA GeForce RTX 4090",
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


def _proxy_url(pod_id: str) -> str:
    return f"https://{pod_id}-8000.proxy.runpod.net"


def _create_pod(client: httpx.Client, headers: dict[str, str], cloud: str) -> dict:
    image = resolve_qwen_image()
    try:
        auth_id = require_registry_auth(image)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    body = {
        "name": "dfi-qwen3-vl-8b-load",
        "imageName": image,
        "gpuTypeIds": GPU_TYPE_IDS,
        "gpuCount": 1,
        "containerDiskInGb": 80,
        "volumeInGb": 0,
        "ports": ["8000/http"],
        "cloudType": cloud,
        "computeType": "GPU",
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


def _terminate(client: httpx.Client, headers: dict[str, str], pod_id: str) -> None:
    resp = client.delete(f"{REST}/pods/{pod_id}", headers=headers)
    if resp.status_code >= 400:
        print(f"Terminate failed {resp.status_code}: {resp.text[:400]}", file=sys.stderr)
        return
    print(f"Terminated pod {pod_id}")


def _wait_models(base: str, timeout_s: float) -> dict:
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=20.0, follow_redirects=True) as c:
                resp = c.get(f"{base}/v1/models")
                last = f"{resp.status_code} {resp.text[:200]}"
                if resp.status_code == 200:
                    return resp.json()
        except Exception as exc:  # noqa: BLE001
            last = str(exc)[:200]
        print(f"  waiting for /v1/models ({last})")
        time.sleep(20)
    raise SystemExit(f"Timed out waiting for SGLang. Last: {last}")


def _identify(base: str, image_url: str) -> dict:
    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": IDENTIFY_PROMPT},
                ],
            }
        ],
        "max_tokens": 256,
        "temperature": 0.2,
    }
    with httpx.Client(timeout=180.0, follow_redirects=True) as c:
        resp = c.post(f"{base}/v1/chat/completions", json=payload)
        if resp.status_code >= 400:
            raise SystemExit(f"Identify failed {resp.status_code}: {resp.text[:800]}")
        return resp.json()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="Leave the pod running after the test")
    parser.add_argument("--pod-id", default="", help="Reuse an existing pod instead of creating one")
    parser.add_argument("--cloud", default="COMMUNITY", choices=("COMMUNITY", "SECURE"))
    parser.add_argument("--wait-s", type=float, default=1500.0)
    parser.add_argument(
        "--terminate-only",
        action="store_true",
        help="Terminate the saved/.pod_id or --pod-id pod and exit",
    )
    args = parser.parse_args()

    _load_env()
    headers = _auth()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    POD_FILE.parent.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=60.0) as client:
        existing_id = (args.pod_id or "").strip()
        if not existing_id and POD_FILE.is_file():
            existing_id = POD_FILE.read_text().strip()
        if args.terminate_only:
            if not existing_id:
                raise SystemExit("No pod id to terminate")
            _terminate(client, headers, existing_id)
            return
        if existing_id and not args.pod_id:
            print(f"Terminating previous pod {existing_id}")
            _terminate(client, headers, existing_id)
        if args.pod_id:
            pod_id = args.pod_id
            pod = _get_pod(client, headers, pod_id)
            print(f"Reusing pod {pod_id} status={pod.get('desiredStatus')}")
        else:
            try:
                pod = _create_pod(client, headers, args.cloud)
            except SystemExit as exc:
                if args.cloud == "COMMUNITY":
                    print(f"{exc}; retrying SECURE")
                    pod = _create_pod(client, headers, "SECURE")
                else:
                    raise
            pod_id = pod["id"]
            POD_FILE.write_text(pod_id + "\n")
            print(
                f"Created pod {pod_id} cloud={pod.get('cloudType')} "
                f"gpu={pod.get('machine', {}).get('gpuTypeId') or pod.get('gpu')} "
                f"${pod.get('costPerHr')}/hr"
            )

        try:
            deadline = time.time() + 300
            while time.time() < deadline:
                pod = _get_pod(client, headers, pod_id)
                status = pod.get("desiredStatus")
                runtime = pod.get("runtime") or {}
                print(f"  pod {status} runtime_keys={list(runtime)[:8]}")
                if status == "RUNNING":
                    break
                if status in {"EXITED", "TERMINATED"}:
                    raise SystemExit(f"Pod ended early: {status} {json.dumps(pod)[:500]}")
                time.sleep(8)
            else:
                raise SystemExit("Pod did not reach RUNNING")

            base = _proxy_url(pod_id)
            print(f"Waiting for model load at {base} (HF download + SGLang init)")
            models = _wait_models(base, args.wait_s)
            print(json.dumps({"models": models}, indent=2)[:800])

            t0 = time.perf_counter()
            result = _identify(base, DEMO_JPEG)
            elapsed = time.perf_counter() - t0
            text = (
                ((result.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            ).strip()
            summary = {
                "at": datetime.now(tz=timezone.utc).isoformat(),
                "runtime": "sglang",
                "sglang_required": True,
                "model": MODEL,
                "pod_id": pod_id,
                "identify_s": round(elapsed, 2),
                "usage": result.get("usage"),
                "text": text,
            }
            stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            out = OUT_DIR / f"qwen_identify_{stamp}.json"
            out.write_text(json.dumps({"summary": summary, "raw": result}, indent=2))
            print(json.dumps(summary, indent=2))
            print(f"Wrote {out}")
        finally:
            if not args.keep:
                _terminate(client, headers, pod_id)
            else:
                print(f"Keeping pod {pod_id} at {_proxy_url(pod_id)}")


if __name__ == "__main__":
    main()
