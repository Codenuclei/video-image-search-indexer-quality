"""RunPod Serverless handler for faster-whisper transcription.

Deploy as a dedicated Studio Whisper endpoint. Input:

```json
{
  "input": {
    "audio_base64": "<wav bytes>",
    "model": "base",
    "vad_filter": true,
    "beam_size": 1
  }
}
```

Output:

```json
{
  "segments": [
    {"start_sec": 0.0, "end_sec": 1.2, "text": "..."}
  ]
}
```
"""

from __future__ import annotations

import base64
import logging
import os
import tempfile
from typing import Any

logger = logging.getLogger("runpod_whisper")

_MODEL = None
_MODEL_NAME = None


def _load_model(name: str):
    global _MODEL, _MODEL_NAME
    from faster_whisper import WhisperModel

    if _MODEL is not None and _MODEL_NAME == name:
        return _MODEL
    device = os.environ.get("WHISPER_DEVICE", "cuda")
    compute = os.environ.get("WHISPER_COMPUTE_TYPE", "float16" if device == "cuda" else "int8")
    logger.info("Loading Whisper model=%s device=%s compute=%s", name, device, compute)
    _MODEL = WhisperModel(name, device=device, compute_type=compute)
    _MODEL_NAME = name
    return _MODEL


def handler(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("input") if isinstance(event, dict) else None
    if not isinstance(payload, dict):
        return {"error": "missing input", "segments": []}
    audio_b64 = payload.get("audio_base64") or payload.get("wav_base64")
    if not audio_b64:
        return {"error": "audio_base64 required", "segments": []}
    model_name = str(payload.get("model") or os.environ.get("WHISPER_MODEL_SIZE") or "base")
    vad = bool(payload.get("vad_filter", True))
    beam = int(payload.get("beam_size") or 1)
    try:
        raw = base64.b64decode(audio_b64)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"invalid base64: {exc}", "segments": []}

    with tempfile.TemporaryDirectory() as tmp:
        wav_path = os.path.join(tmp, "audio.wav")
        with open(wav_path, "wb") as handle:
            handle.write(raw)
        model = _load_model(model_name)
        segments_iter, _info = model.transcribe(
            wav_path,
            beam_size=beam,
            vad_filter=vad,
        )
        segments = []
        for seg in segments_iter:
            text = (seg.text or "").strip()
            if not text:
                continue
            segments.append(
                {
                    "start_sec": float(seg.start),
                    "end_sec": float(seg.end),
                    "text": text,
                }
            )
    return {"segments": segments, "model": model_name}


# RunPod Serverless entrypoint
try:
    import runpod

    runpod.serverless.start({"handler": handler})
except ImportError:
    pass
