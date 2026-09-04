#!/usr/bin/env python3
"""Create a RunPod serverless template + A4000 endpoint for buffalo_l (scale-to-zero)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
BACKEND_ENV = REPO / "backend" / ".env"
ENDPOINT_FILE = REPO / "runpod" / "face-buffalo" / ".endpoint_id"
IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
HANDLER_URL = (
    "https://raw.githubusercontent.com/Codenuclei/"
    "video-image-search-indexer-quality/main/runpod/face-buffalo/handler.py"
)
ORT_CUDA12 = (
    "https://aiinfra.pkgs.visualstudio.com/PublicPackages/_packaging/onnxruntime-cuda-12/pypi/simple/"
)
START_CMD = [
    "bash",
    "-lc",
    (
        "set -euo pipefail; "
        "export PYTHONUNBUFFERED=1; "
        "export RUNPOD_SKIP_AUTO_SYSTEM_CHECKS=true; "
        "python -m pip install -q numpy opencv-python-headless onnx Pillow requests tqdm easydict scikit-image runpod; "
        f"python -m pip install -q --extra-index-url {ORT_CUDA12} 'onnxruntime-gpu==1.20.2'; "
        "python -m pip install -q --no-deps insightface; "
        f"curl -fsSL {HANDLER_URL} -o /tmp/handler.py; "
        "exec python -u /tmp/handler.py"
    ),
]
GPU_TYPE_IDS = ["NVIDIA RTX A4000", "NVIDIA RTX A5000"]
TEMPLATE_NAME = "dfi-face-buffalo"
ENDPOINT_NAME = "dfi-face-buffalo"
REST = "https://rest.runpod.io/v1"


def _load_env() -> None:
    if not BACKEND_ENV.is_file():
        raise SystemExit(f"Missing {BACKEND_ENV}")
    for line in BACKEND_ENV.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, val = s.split("=", 1)
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def _auth() -> dict[str, str]:
    key = os.environ.get("RUNPOD_API_KEY", "").strip()
    if not key:
        raise SystemExit("RUNPOD_API_KEY is missing from backend/.env")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _find_named(items: list[dict], name: str) -> dict | None:
    for item in items:
        if item.get("name") == name:
            return item
    return None


def main() -> None:
    _load_env()
    headers = _auth()
    with httpx.Client(timeout=60.0) as client:
        templates = client.get(f"{REST}/templates", headers=headers)
        templates.raise_for_status()
        payload = templates.json()
        if isinstance(payload, dict):
            payload = payload.get("templates") or payload.get("data") or []
        existing = _find_named(payload, TEMPLATE_NAME)

        if existing:
            template_id = existing["id"]
            updated = client.patch(
                f"{REST}/templates/{template_id}",
                headers=headers,
                json={
                    "imageName": IMAGE,
                    "containerDiskInGb": 40,
                    "dockerStartCmd": START_CMD,
                    "env": {
                        "PYTHONUNBUFFERED": "1",
                        "RUNPOD_SKIP_AUTO_SYSTEM_CHECKS": "true",
                    },
                },
            )
            if updated.status_code >= 400:
                raise SystemExit(f"Update template failed {updated.status_code}: {updated.text}")
            print(f"Updated template {template_id}")
        else:
            created = client.post(
                f"{REST}/templates",
                headers=headers,
                json={
                    "name": TEMPLATE_NAME,
                    "imageName": IMAGE,
                    "isServerless": True,
                    "containerDiskInGb": 40,
                    "volumeInGb": 0,
                    "dockerStartCmd": START_CMD,
                    "env": {
                        "PYTHONUNBUFFERED": "1",
                        "RUNPOD_SKIP_AUTO_SYSTEM_CHECKS": "true",
                    },
                },
            )
            if created.status_code >= 400:
                raise SystemExit(f"Create template failed {created.status_code}: {created.text}")
            template_id = created.json()["id"]
            print(f"Created template {template_id}")

        endpoints = client.get(f"{REST}/endpoints", headers=headers)
        endpoints.raise_for_status()
        ep_payload = endpoints.json()
        if isinstance(ep_payload, dict):
            ep_payload = ep_payload.get("endpoints") or ep_payload.get("data") or []
        existing_ep = _find_named(ep_payload, ENDPOINT_NAME)
        if existing_ep:
            endpoint_id = existing_ep["id"]
            patched = client.patch(
                f"{REST}/endpoints/{endpoint_id}",
                headers=headers,
                json={
                    "gpuTypeIds": GPU_TYPE_IDS,
                    "workersMin": 0,
                    "workersMax": 1,
                    "idleTimeout": 300,
                    "executionTimeoutMs": 600000,
                },
            )
            if patched.status_code >= 400:
                raise SystemExit(f"Update endpoint failed {patched.status_code}: {patched.text}")
            print(f"Updated endpoint {endpoint_id}")
        else:
            created_ep = client.post(
                f"{REST}/endpoints",
                headers=headers,
                json={
                    "name": ENDPOINT_NAME,
                    "templateId": template_id,
                    "computeType": "GPU",
                    "gpuCount": 1,
                    "gpuTypeIds": GPU_TYPE_IDS,
                    "workersMin": 0,
                    "workersMax": 1,
                    "idleTimeout": 300,
                    "executionTimeoutMs": 600000,
                    "flashboot": True,
                    "scalerType": "QUEUE_DELAY",
                    "scalerValue": 4,
                },
            )
            if created_ep.status_code >= 400:
                raise SystemExit(f"Create endpoint failed {created_ep.status_code}: {created_ep.text}")
            endpoint_id = created_ep.json()["id"]
            print(f"Created endpoint {endpoint_id}")

    ENDPOINT_FILE.write_text(endpoint_id + "\n")
    print(f"Wrote {ENDPOINT_FILE}")
    print(f"runsync https://api.runpod.ai/v2/{endpoint_id}/runsync")


if __name__ == "__main__":
    main()
