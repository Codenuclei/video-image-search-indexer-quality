"""
Qwen3-VL client — talks to a local vLLM OpenAI-compatible server (Docker sidecar).

Used for video frame captioning during indexing (replaces Gemini VLM when enabled).
"""
from __future__ import annotations

import base64
import logging
from functools import lru_cache
from pathlib import Path

import httpx

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

_DESCRIBE_PROMPT = (
    "This is a single frame from a video. "
    "Describe what is visible in one concise sentence for visual search indexing. "
    "Focus on objects, people, actions, and scene context."
)


class QwenVlmError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _base_url(settings: Settings) -> str:
    return settings.qwen_vlm_base_url.rstrip("/")


def qwen_vlm_ready_sync(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    if not settings.qwen_vlm_enabled:
        return False
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{_base_url(settings)}/v1/models")
            return resp.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def describe_image_sync(
    image_path: str,
    *,
    timestamp_sec: float,
    settings: Settings | None = None,
) -> str:
    """Caption a video keyframe via Qwen3-VL (sync — use asyncio.to_thread in FastAPI)."""
    settings = settings or get_settings()
    if not settings.qwen_vlm_enabled:
        raise QwenVlmError("Qwen VLM is disabled")

    path = Path(image_path)
    if not path.is_file():
        raise QwenVlmError(f"Image not found: {image_path}")

    image_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    prompt = f"{_DESCRIBE_PROMPT}\nTimestamp: {timestamp_sec:.1f}s"

    payload = {
        "model": settings.qwen_vlm_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        "max_tokens": settings.qwen_vlm_max_tokens,
        "temperature": 0.2,
    }

    url = f"{_base_url(settings)}/v1/chat/completions"
    try:
        with httpx.Client(timeout=settings.qwen_vlm_timeout_seconds) as client:
            resp = client.post(url, json=payload, headers={"Authorization": "Bearer EMPTY"})
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:300] if exc.response is not None else ""
        raise QwenVlmError(f"Qwen VLM HTTP {exc.response.status_code}: {body}") from exc
    except Exception as exc:  # noqa: BLE001
        raise QwenVlmError(str(exc)) from exc

    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise QwenVlmError(f"Unexpected Qwen response: {data!r}") from exc

    return (text or "").strip()[:500]
