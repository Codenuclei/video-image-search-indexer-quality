"""RunPod Serverless client for faster-whisper transcription (Studio).

Uploads a short WAV (or base64 audio) to a dedicated Whisper endpoint and returns
ordered ``{start_sec, end_sec, text}`` segments. Prefer this over local CPU
Whisper when ``RUNPOD_WHISPER_ENABLED`` is set.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.video.whisper_engine import WhisperSegment

logger = logging.getLogger(__name__)


class RunPodWhisperError(RuntimeError):
    """Raised when the Whisper serverless job fails or times out."""


def runpod_whisper_configured(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(
        settings.runpod_whisper_enabled
        and (settings.runpod_api_key or "").strip()
        and (settings.runpod_whisper_endpoint_id or "").strip()
    )


async def transcribe_wav_runpod(
    wav_path: str,
    settings: Settings | None = None,
) -> list[WhisperSegment]:
    """Transcribe a local WAV via RunPod; returns empty list when disabled."""
    settings = settings or get_settings()
    if not runpod_whisper_configured(settings):
        return []

    with open(wav_path, "rb") as handle:
        audio_b64 = base64.b64encode(handle.read()).decode("ascii")

    endpoint = settings.runpod_whisper_endpoint_id.strip()
    api_key = settings.runpod_api_key.strip()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "input": {
            "audio_base64": audio_b64,
            "model": settings.runpod_whisper_model_size or "base",
            "vad_filter": True,
            "beam_size": 1,
        }
    }
    run_url = f"https://api.runpod.ai/v2/{endpoint}/run"
    status_base = f"https://api.runpod.ai/v2/{endpoint}/status"

    timeout = httpx.Timeout(60.0, read=settings.runpod_whisper_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        submit = await client.post(run_url, headers=headers, json=payload)
        if submit.status_code >= 400:
            raise RunPodWhisperError(
                f"run HTTP {submit.status_code}: {submit.text[:300]}"
            )
        data = submit.json()
        job_id = str(data.get("id") or "").strip()
        if not job_id:
            raise RunPodWhisperError(f"missing job id: {data!r}"[:300])

        deadline = time.monotonic() + float(settings.runpod_whisper_timeout_seconds)
        poll = max(0.5, float(settings.runpod_whisper_poll_seconds))
        while time.monotonic() < deadline:
            status_resp = await client.get(f"{status_base}/{job_id}", headers=headers)
            if status_resp.status_code >= 400:
                raise RunPodWhisperError(
                    f"status HTTP {status_resp.status_code}: {status_resp.text[:300]}"
                )
            body = status_resp.json()
            status = str(body.get("status") or "").upper()
            if status in {"COMPLETED", "COMPLETED_SUCCESS"}:
                return _segments_from_output(body.get("output"))
            if status in {"FAILED", "CANCELLED", "TIMED_OUT", "ERROR"}:
                raise RunPodWhisperError(
                    f"job {job_id} {status}: {body.get('error') or body}"
                )
            await asyncio.sleep(poll)

    raise RunPodWhisperError(
        f"job {job_id} timed out after {settings.runpod_whisper_timeout_seconds:.0f}s"
    )


def _segments_from_output(output: Any) -> list[WhisperSegment]:
    rows: list[Any]
    if isinstance(output, dict):
        rows = list(output.get("segments") or output.get("cues") or [])
    elif isinstance(output, list):
        rows = output
    else:
        rows = []
    out: list[WhisperSegment] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(row.get("start_sec", row.get("start", 0.0)))
            end = float(row.get("end_sec", row.get("end", start)))
        except (TypeError, ValueError):
            continue
        if end < start:
            end = start
        out.append(WhisperSegment(start_sec=start, end_sec=end, text=text))
    return out
