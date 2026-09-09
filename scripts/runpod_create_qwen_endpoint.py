#!/usr/bin/env python3
"""Create or update the Qwen3-VL SGLang RunPod serverless endpoint (scale-to-zero).

Does not touch the face buffalo endpoint. No Postgres writes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
BACKEND_ENV = REPO / "backend" / ".env"
ENDPOINT_FILE = REPO / "runpod" / "qwen-vl" / ".endpoint_id"
HANDLER = REPO / "runpod" / "qwen-vl" / "handler.py"
TAGS = REPO / "runpod" / "qwen-vl" / "tags.py"
sys.path.insert(0, str(REPO / "runpod" / "qwen-vl"))
from image_source import require_registry_auth, resolve_qwen_image  # noqa: E402
MODEL = "Qwen/Qwen3-VL-8B-Instruct"
TEMPLATE_NAME = "dfi-qwen3-vl-sglang-v2"
ENDPOINT_NAME = "dfi-qwen3-vl-sglang-v2"
REST = "https://rest.runpod.io/v1"
GPU_TYPE_IDS = [
    "NVIDIA A40",
    "NVIDIA RTX A6000",
    "NVIDIA L40",
    "NVIDIA RTX 6000 Ada Generation",
    "NVIDIA GeForce RTX 4090",
]


def _load_env() -> None:
    if not BACKEND_ENV.is_file():
        raise SystemExit(f"Missing {BACKEND_ENV}")
    for line in BACKEND_ENV.read_text().splitlines():
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


def _find_named(items: list[dict], name: str) -> dict | None:
    for item in items:
        if item.get("name") == name:
            return item
    return None


def _template_body() -> dict:
    image = resolve_qwen_image()
    try:
        auth_id = require_registry_auth(image)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return {
        "imageName": image,
        "containerDiskInGb": 80,
        "volumeInGb": 0,
        "dockerEntrypoint": ["/app/start.sh"],
        "dockerStartCmd": [],
        "containerRegistryAuthId": auth_id,
        "env": {
            "PYTHONUNBUFFERED": "1",
            "SGLANG_URL": "http://127.0.0.1:8000",
            "QWEN_MODEL": MODEL,
            "QWEN_CONCURRENCY": "32",
            "QWEN_MAX_TOKENS": "1536",
            "QWEN_SERVERLESS": "1",
            "RUNPOD_INIT_TIMEOUT": "1800",
            "HF_XET_HIGH_PERFORMANCE": "1",
            "SGLANG_VLM_CACHE_SIZE_MB": "0",
        },
    }


def _endpoint_body(template_id: str) -> dict:
    return {
        "name": ENDPOINT_NAME,
        "templateId": template_id,
        "computeType": "GPU",
        "gpuCount": 1,
        "gpuTypeIds": GPU_TYPE_IDS,
        "workersMin": 0,
        "workersMax": 1,
        "idleTimeout": 180,
        "executionTimeoutMs": 1800000,
        "flashboot": True,
        "scalerType": "REQUEST_COUNT",
        "scalerValue": 32,
    }


def main() -> None:
    _load_env()
    headers = _auth()
    if not HANDLER.is_file() or not TAGS.is_file():
        raise SystemExit("Missing runpod/qwen-vl/handler.py or tags.py")
    start_sh = REPO / "runpod" / "qwen-vl" / "start.sh"
    if not start_sh.is_file():
        raise SystemExit("Missing runpod/qwen-vl/start.sh")
    with httpx.Client(timeout=60.0) as client:
        templates = client.get(f"{REST}/templates", headers=headers)
        templates.raise_for_status()
        payload = templates.json()
        if isinstance(payload, dict):
            payload = payload.get("templates") or payload.get("data") or []
        existing = _find_named(payload, TEMPLATE_NAME)
        body = _template_body()
        if existing:
            template_id = existing["id"]
            updated = client.patch(f"{REST}/templates/{template_id}", headers=headers, json=body)
            if updated.status_code >= 400:
                raise SystemExit(f"Update template failed {updated.status_code}: {updated.text[:800]}")
            print(f"Updated template {template_id}")
        else:
            created = client.post(
                f"{REST}/templates",
                headers=headers,
                json={"name": TEMPLATE_NAME, "isServerless": True, **body},
            )
            if created.status_code >= 400:
                raise SystemExit(f"Create template failed {created.status_code}: {created.text[:800]}")
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
                    "templateId": template_id,
                    "gpuTypeIds": GPU_TYPE_IDS,
                    "workersMin": 0,
                    "workersMax": 1,
                    "idleTimeout": 180,
                    "executionTimeoutMs": 1800000,
                    "scalerType": "REQUEST_COUNT",
                    "scalerValue": 32,
                },
            )
            if patched.status_code >= 400:
                raise SystemExit(f"Update endpoint failed {patched.status_code}: {patched.text[:800]}")
            print(f"Updated endpoint {endpoint_id}")
        else:
            created_ep = client.post(
                f"{REST}/endpoints",
                headers=headers,
                json=_endpoint_body(template_id),
            )
            if created_ep.status_code >= 400:
                raise SystemExit(f"Create endpoint failed {created_ep.status_code}: {created_ep.text[:800]}")
            endpoint_id = created_ep.json()["id"]
            print(f"Created endpoint {endpoint_id}")

    ENDPOINT_FILE.write_text(endpoint_id + "\n")
    print(f"Wrote {ENDPOINT_FILE}")
    print(f"run https://api.runpod.ai/v2/{endpoint_id}/run")


if __name__ == "__main__":
    main()
