"""RunPod serverless Qwen identify. JPEG bytes in; never Drive URLs or the face endpoint."""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any

import httpx

from app.config import Settings, get_settings

try:
    from app.objects.identify_tags import IDENTIFY_AND_CAPTION_PROMPT as _DEFAULT_PROMPT
except ImportError:  # 092748f DigitalOcean image still exports IDENTIFY_PROMPT
    from app.objects.identify_tags import IDENTIFY_PROMPT as _DEFAULT_PROMPT

logger = logging.getLogger(__name__)

_REST = "https://rest.runpod.io/v1"
_RUN = "https://api.runpod.ai/v2"
_scaled: tuple[int, int] | None = None
_DRIVE_URL_HINTS = ("drive.google.com", "googleapis.com/drive")


class RunPodQwenError(RuntimeError):
    pass


def runpod_qwen_configured(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    endpoint = (settings.runpod_qwen_endpoint_id or "").strip()
    key = (settings.runpod_api_key or "").strip()
    face = (settings.runpod_face_endpoint_id or "").strip()
    if not endpoint or not key:
        return False
    if face and endpoint == face:
        logger.error("Refusing Qwen GPU: endpoint matches buffalo face")
        return False
    return True


def payload_contains_drive_url(payload: object) -> bool:
    blob = str(payload).casefold()
    return any(hint in blob for hint in _DRIVE_URL_HINTS)


def build_qwen_identify_payload(
    jpeg_bytes: bytes,
    *,
    prompt: str = _DEFAULT_PROMPT,
    max_tokens: int = 1536,
) -> dict[str, Any]:
    payload = {
        "image_b64": base64.b64encode(jpeg_bytes).decode("ascii"),
        "prompt": prompt,
        "max_tokens": max_tokens,
    }
    if payload_contains_drive_url(payload):
        raise RunPodQwenError("Refusing to send a Drive URL to RunPod Qwen")
    return payload


def serverless_qwen_scale(*, active: bool, workers_max: int = 1) -> dict[str, int]:
    if not active:
        return {"workersMin": 0, "workersMax": 0}
    n = max(1, min(8, int(workers_max)))
    return {"workersMin": 1, "workersMax": n}


async def set_qwen_workers_max(settings: Settings, workers_max: int) -> None:
    global _scaled
    desired = serverless_qwen_scale(
        active=workers_max > 0,
        workers_max=int(getattr(settings, "runpod_qwen_workers_max", 1) or 1),
    )
    scaled = (desired["workersMin"], desired["workersMax"])
    if _scaled == scaled:
        return
    endpoint = (settings.runpod_qwen_endpoint_id or "").strip()
    key = (settings.runpod_api_key or "").strip()
    if not endpoint or not key:
        return
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.patch(
            f"{_REST}/endpoints/{endpoint}",
            headers=headers,
            json=desired,
        )
    if resp.status_code >= 400:
        raise RunPodQwenError(f"scale serverless workers={desired} failed {resp.status_code}")
    _scaled = scaled
    logger.info(
        "qwen_gpu_serverless min=%s max=%s endpoint=%s",
        scaled[0],
        scaled[1],
        endpoint[:12],
    )


def parse_qwen_identify_output(output: dict[str, Any]) -> str:
    if output.get("error"):
        raise RunPodQwenError(str(output.get("error")))
    raw = output.get("raw_text")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    results = output.get("results")
    if isinstance(results, list) and results:
        first = results[0] if isinstance(results[0], dict) else {}
        if first.get("error"):
            raise RunPodQwenError(str(first.get("error")))
        nested = first.get("raw_text")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    raise RunPodQwenError(f"Qwen identify output missing raw_text: {list(output.keys())}")


async def identify_jpeg_runpod(
    jpeg_bytes: bytes,
    settings: Settings | None = None,
) -> str:
    settings = settings or get_settings()
    if not runpod_qwen_configured(settings):
        raise RunPodQwenError("RunPod Qwen GPU is not configured")
    payload = build_qwen_identify_payload(
        jpeg_bytes,
        max_tokens=settings.qwen_identify_max_tokens,
    )
    await set_qwen_workers_max(settings, 1)
    endpoint = settings.runpod_qwen_endpoint_id.strip()
    headers = {
        "Authorization": f"Bearer {settings.runpod_api_key.strip()}",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(
        float(getattr(settings, "runpod_qwen_timeout_seconds", 1800.0) or 1800.0),
        connect=30.0,
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        submit = None
        for attempt in range(8):
            await set_qwen_workers_max(settings, 1)
            submit = await client.post(
                f"{_RUN}/{endpoint}/run",
                headers=headers,
                json={"input": payload},
            )
            if submit.status_code != 409:
                break
            logger.warning("qwen endpoint paused; retry %s", attempt + 1)
            await asyncio.sleep(2.0 * (attempt + 1))
        assert submit is not None
        if submit.status_code >= 400:
            raise RunPodQwenError(f"run HTTP {submit.status_code}: {submit.text[:300]}")
        body = submit.json()
        job_id = body.get("id")
        if not job_id:
            raise RunPodQwenError(f"run missing id: {body}")
        status_url = f"{_RUN}/{endpoint}/status/{job_id}"
        deadline = time.monotonic() + float(
            getattr(settings, "runpod_qwen_timeout_seconds", 1800.0) or 1800.0
        )
        status = ""
        while time.monotonic() < deadline:
            try:
                status_resp = await client.get(status_url, headers=headers)
            except httpx.TransportError:
                await asyncio.sleep(2.0)
                continue
            if status_resp.status_code >= 400:
                raise RunPodQwenError(
                    f"status HTTP {status_resp.status_code}: {status_resp.text[:300]}"
                )
            data = status_resp.json()
            status = str(data.get("status") or "")
            if status.lower() in {"completed"}:
                output = data.get("output") or {}
                if not isinstance(output, dict):
                    raise RunPodQwenError("RunPod Qwen output was not an object")
                return parse_qwen_identify_output(output)
            if status.lower() in {"failed", "cancelled", "timed_out"}:
                raise RunPodQwenError(f"job {job_id} {status}: {data.get('error') or data}")
            await asyncio.sleep(1.0)
        raise RunPodQwenError(f"job {job_id} still {status} after timeout")
