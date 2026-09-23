"""RunPod Serverless client for faster-whisper transcription (Studio).

Splits the extracted WAV into fixed-length Opus chunks, transcribes each on a
dedicated Whisper endpoint, and returns ordered ``{start_sec, end_sec, text}``
segments on the original timeline. Prefer this over local CPU Whisper when
``RUNPOD_WHISPER_ENABLED`` is set.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import subprocess
import tempfile
import time
import wave
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.video.whisper_engine import WhisperSegment

logger = logging.getLogger(__name__)

# RunPod /run rejects request bodies above ~10MB behind Cloudflare (HTTP 502/413).
MAX_PAYLOAD_BASE64_BYTES = 8 * 1024 * 1024
CHUNK_OPUS_BITRATE = "24k"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_HTTP_ATTEMPTS = 4


class RunPodWhisperError(RuntimeError):
    """Raised when the Whisper serverless job fails or times out."""


def runpod_whisper_configured(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(
        settings.runpod_whisper_enabled
        and (settings.runpod_api_key or "").strip()
        and (settings.runpod_whisper_endpoint_id or "").strip()
    )


def wav_duration_seconds(wav_path: str) -> float:
    with wave.open(wav_path, "rb") as handle:
        rate = handle.getframerate() or 16000
        return handle.getnframes() / float(rate)


def chunk_windows(duration_sec: float, chunk_sec: float) -> list[tuple[float, float]]:
    """Contiguous ``(start, length)`` windows covering ``duration_sec``."""
    if duration_sec <= 0:
        return []
    chunk_sec = max(30.0, float(chunk_sec))
    windows: list[tuple[float, float]] = []
    start = 0.0
    while start < duration_sec - 0.05:
        length = min(chunk_sec, duration_sec - start)
        windows.append((start, length))
        start += length
    return windows


def encode_opus_chunk(wav_path: str, start_sec: float, length_sec: float, out_path: str) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-ss",
        f"{start_sec:.3f}",
        "-t",
        f"{length_sec:.3f}",
        "-i",
        wav_path,
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libopus",
        "-b:a",
        CHUNK_OPUS_BITRATE,
        out_path,
    ]
    subprocess.run(cmd, capture_output=True, timeout=600, check=True)


async def transcribe_wav_runpod(
    wav_path: str,
    settings: Settings | None = None,
) -> list[WhisperSegment]:
    """Transcribe a local WAV via RunPod; returns empty list when disabled."""
    settings = settings or get_settings()
    if not runpod_whisper_configured(settings):
        return []

    duration = await asyncio.to_thread(wav_duration_seconds, wav_path)
    max_audio = float(settings.runpod_whisper_max_audio_seconds)
    if max_audio > 0 and duration > max_audio + 1.0:
        raise RunPodWhisperError(
            f"audio is {duration / 60:.0f} min; RunPod limit is {max_audio / 60:.0f} min"
        )
    windows = chunk_windows(duration, settings.runpod_whisper_chunk_seconds)
    if not windows:
        return []

    concurrency = max(1, int(settings.runpod_whisper_chunk_concurrency))
    semaphore = asyncio.Semaphore(concurrency)
    timeout = httpx.Timeout(60.0, read=120.0)
    started = time.monotonic()
    logger.info(
        "RunPod Whisper: %.0fs audio in %d chunk(s) of <=%.0fs (concurrency=%d)",
        duration,
        len(windows),
        float(settings.runpod_whisper_chunk_seconds),
        concurrency,
    )

    with tempfile.TemporaryDirectory(prefix="runpod-whisper-") as tmp:
        async with httpx.AsyncClient(timeout=timeout) as client:

            async def _one(index: int, start: float, length: float) -> list[WhisperSegment]:
                async with semaphore:
                    chunk_path = os.path.join(tmp, f"chunk-{index:03d}.ogg")
                    await asyncio.to_thread(encode_opus_chunk, wav_path, start, length, chunk_path)
                    with open(chunk_path, "rb") as handle:
                        audio_b64 = base64.b64encode(handle.read()).decode("ascii")
                    os.unlink(chunk_path)
                    if len(audio_b64) > MAX_PAYLOAD_BASE64_BYTES:
                        raise RunPodWhisperError(
                            f"chunk {index} payload {len(audio_b64)} bytes exceeds RunPod limit"
                        )
                    segments = await _run_job(client, settings, audio_b64, label=f"chunk {index}")
                    return offset_segments(segments, start, start + length)

            tasks = [
                asyncio.create_task(_one(i, start, length))
                for i, (start, length) in enumerate(windows)
            ]
            try:
                results = await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

    merged = [seg for chunk in results for seg in chunk]
    merged.sort(key=lambda s: (s.start_sec, s.end_sec))
    logger.info(
        "RunPod Whisper: %d segment(s) from %d chunk(s) in %.0fs",
        len(merged),
        len(windows),
        time.monotonic() - started,
    )
    return merged


def offset_segments(
    segments: list[WhisperSegment], offset_sec: float, chunk_end_sec: float
) -> list[WhisperSegment]:
    out: list[WhisperSegment] = []
    for seg in segments:
        start = min(seg.start_sec + offset_sec, chunk_end_sec)
        end = min(max(seg.end_sec + offset_sec, start), chunk_end_sec)
        out.append(WhisperSegment(start_sec=start, end_sec=end, text=seg.text))
    return out


async def _request_with_retry(
    client: httpx.AsyncClient, method: str, url: str, **kwargs: Any
) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(1, _HTTP_ATTEMPTS + 1):
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            last_exc = exc
        else:
            if resp.status_code not in _RETRYABLE_STATUS or attempt == _HTTP_ATTEMPTS:
                return resp
            last_exc = RunPodWhisperError(f"HTTP {resp.status_code}")
        await asyncio.sleep(min(2.0 * attempt, 10.0))
    raise RunPodWhisperError(f"{method} {url} failed after {_HTTP_ATTEMPTS} attempts: {last_exc}")


async def _run_job(
    client: httpx.AsyncClient,
    settings: Settings,
    audio_b64: str,
    *,
    label: str,
) -> list[WhisperSegment]:
    endpoint = settings.runpod_whisper_endpoint_id.strip()
    headers = {
        "Authorization": f"Bearer {settings.runpod_api_key.strip()}",
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

    submit = await _request_with_retry(client, "POST", run_url, headers=headers, json=payload)
    if submit.status_code >= 400:
        raise RunPodWhisperError(f"{label} run HTTP {submit.status_code}: {submit.text[:300]}")
    data = submit.json()
    job_id = str(data.get("id") or "").strip()
    if not job_id:
        raise RunPodWhisperError(f"{label} missing job id: {data!r}"[:300])

    deadline = time.monotonic() + float(settings.runpod_whisper_timeout_seconds)
    poll = max(0.5, float(settings.runpod_whisper_poll_seconds))
    while time.monotonic() < deadline:
        status_resp = await _request_with_retry(
            client, "GET", f"{status_base}/{job_id}", headers=headers
        )
        if status_resp.status_code >= 400:
            raise RunPodWhisperError(
                f"{label} status HTTP {status_resp.status_code}: {status_resp.text[:300]}"
            )
        body = status_resp.json()
        status = str(body.get("status") or "").upper()
        if status in {"COMPLETED", "COMPLETED_SUCCESS"}:
            output = body.get("output")
            if isinstance(output, dict) and output.get("error"):
                raise RunPodWhisperError(f"{label} job {job_id} error: {output['error']}")
            return _segments_from_output(output)
        if status in {"FAILED", "CANCELLED", "TIMED_OUT", "ERROR"}:
            raise RunPodWhisperError(f"{label} job {job_id} {status}: {body.get('error') or body}")
        await asyncio.sleep(poll)

    raise RunPodWhisperError(
        f"{label} job {job_id} timed out after {settings.runpod_whisper_timeout_seconds:.0f}s"
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
