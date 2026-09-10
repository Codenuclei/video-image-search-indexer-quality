#!/usr/bin/env python3
"""Create a RunPod **serverless** template + endpoint for buffalo_l.

Never a dedicated pod. Rest: workersMin=0 workersMax=0. Under load the API
PATCHes workersMin=1 (and max=1), then scales both back to 0 when idle.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
BACKEND_ENV = REPO / "backend" / ".env"
ENDPOINT_FILE = REPO / "runpod" / "face-buffalo" / ".endpoint_id"
HANDLER = REPO / "runpod" / "face-buffalo" / "handler.py"
sys.path.insert(0, str(REPO / "runpod"))
from secure_image import require_registry_auth, resolve_face_image  # noqa: E402
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


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--template-id",
        help="Update this exact existing template instead of resolving by name.",
    )
    parser.add_argument(
        "--endpoint-id",
        help="Update this exact existing endpoint instead of resolving by name.",
    )
    args = parser.parse_args()
    if bool(args.template_id) != bool(args.endpoint_id):
        parser.error("--template-id and --endpoint-id must be provided together")
    return args


def _template_body() -> dict:
    image = resolve_face_image()
    try:
        auth_id = require_registry_auth(image)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return {
        "imageName": image,
        "containerDiskInGb": 40,
        "volumeInGb": 0,
        "dockerEntrypoint": ["python", "-u", "handler.py"],
        "dockerStartCmd": [],
        "containerRegistryAuthId": auth_id,
        "env": {
            "PYTHONUNBUFFERED": "1",
            "RUNPOD_SKIP_AUTO_SYSTEM_CHECKS": "true",
            "FACE_GPU_WORKERS": "0",
            "FACE_REC_BATCH": "32",
            "FACE_VRAM_RESERVE_MB": "2048",
            "FACE_DET_MEM_LIMIT_GB": "2",
            "FACE_REC_MEM_LIMIT_GB": "4",
            "NVIDIA_DRIVER_CAPABILITIES": "compute,utility,video",
        },
    }


def main() -> None:
    args = _args()
    _load_env()
    if not HANDLER.is_file():
        raise SystemExit("Missing runpod/face-buffalo/handler.py")
    headers = _auth()
    body = _template_body()
    with httpx.Client(timeout=60.0) as client:
        if args.template_id:
            template = client.get(f"{REST}/templates/{args.template_id}", headers=headers)
            template.raise_for_status()
            existing = template.json()
            if existing.get("name") != TEMPLATE_NAME:
                raise SystemExit(
                    f"Template {args.template_id} is named {existing.get('name')!r}, "
                    f"expected {TEMPLATE_NAME!r}"
                )
        else:
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
                json=body,
            )
            if updated.status_code >= 400:
                raise SystemExit(f"Update template failed {updated.status_code}: {updated.text}")
            print(f"Updated template {template_id}")
        else:
            created = client.post(
                f"{REST}/templates",
                headers=headers,
                json={"name": TEMPLATE_NAME, "isServerless": True, **body},
            )
            if created.status_code >= 400:
                raise SystemExit(f"Create template failed {created.status_code}: {created.text}")
            template_id = created.json()["id"]
            print(f"Created template {template_id}")

        if args.endpoint_id:
            endpoint = client.get(f"{REST}/endpoints/{args.endpoint_id}", headers=headers)
            endpoint.raise_for_status()
            existing_ep = endpoint.json()
            if existing_ep.get("name") != ENDPOINT_NAME:
                raise SystemExit(
                    f"Endpoint {args.endpoint_id} is named {existing_ep.get('name')!r}, "
                    f"expected {ENDPOINT_NAME!r}"
                )
        else:
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
                    "workersMax": 0,
                    "idleTimeout": 60,
                    "executionTimeoutMs": 3600000,
                    "flashboot": True,
                    "scalerType": "REQUEST_COUNT",
                    "scalerValue": 1,
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
                    "workersMax": 0,
                    "idleTimeout": 60,
                    "executionTimeoutMs": 3600000,
                    "flashboot": True,
                    "scalerType": "REQUEST_COUNT",
                    "scalerValue": 1,
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
