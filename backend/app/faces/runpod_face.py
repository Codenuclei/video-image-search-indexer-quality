"""RunPod Serverless ArcFace client for Studio quote-window recognition.

Reuses the same request/response contract as the search-stack face GPU worker:
JPEG frames in, detections with 512-d embeddings out. Studio keeps all results
in its own Postgres / cache — this client is stateless inference only.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
import numpy as np

from app.config import Settings, get_settings
from app.faces.engine import DetectedFace

logger = logging.getLogger(__name__)


class RunPodFaceError(RuntimeError):
    """Raised when the ArcFace serverless job fails or times out."""


@dataclass(frozen=True)
class FrameFaceResult:
    frame_ts: float
    faces: list[DetectedFace]


def runpod_face_configured(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(
        settings.runpod_face_gpu_enabled
        and (settings.runpod_api_key or "").strip()
        and (settings.runpod_face_endpoint_id or "").strip()
    )


def _encode_jpeg_b64(image_bgr: np.ndarray, settings: Settings) -> str:
    import cv2

    img = image_bgr
    max_edge = max(64, int(settings.runpod_face_max_edge or 1280))
    h, w = img.shape[:2]
    scale = min(1.0, max_edge / float(max(h, w)))
    if scale < 0.999:
        img = cv2.resize(
            img,
            (max(1, int(w * scale)), max(1, int(h * scale))),
            interpolation=cv2.INTER_AREA,
        )
    quality = max(40, min(95, int(settings.runpod_face_jpeg_quality or 90)))
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RunPodFaceError("failed to encode JPEG for RunPod face job")
    return base64.b64encode(buf.tobytes()).decode("ascii")


async def detect_faces_runpod_frames(
    frames: list[tuple[float, np.ndarray]],
    settings: Settings | None = None,
) -> list[FrameFaceResult]:
    """Detect faces on multiple BGR frames via RunPod.

    ``frames`` is a list of ``(frame_ts, image_bgr)``. Returns one result per
    successfully submitted frame (empty face lists are kept).
    """
    settings = settings or get_settings()
    if not runpod_face_configured(settings) or not frames:
        return []

    endpoint = settings.runpod_face_endpoint_id.strip()
    api_key = settings.runpod_api_key.strip()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    encoded = [
        {
            "frame_ts": float(ts),
            "image_base64": _encode_jpeg_b64(image, settings),
        }
        for ts, image in frames
        if image is not None and getattr(image, "size", 0)
    ]
    if not encoded:
        return []

    payload = {"input": {"frames": encoded, "task": "detect_faces"}}
    run_url = f"https://api.runpod.ai/v2/{endpoint}/run"
    status_base = f"https://api.runpod.ai/v2/{endpoint}/status"
    timeout = httpx.Timeout(60.0, read=settings.runpod_face_timeout_seconds)

    async with httpx.AsyncClient(timeout=timeout) as client:
        submit = await client.post(run_url, headers=headers, json=payload)
        if submit.status_code >= 400:
            raise RunPodFaceError(f"run HTTP {submit.status_code}: {submit.text[:300]}")
        data = submit.json()
        job_id = str(data.get("id") or "").strip()
        if not job_id:
            raise RunPodFaceError(f"missing job id: {data!r}"[:300])

        deadline = time.monotonic() + float(settings.runpod_face_timeout_seconds)
        poll = max(0.4, float(settings.runpod_face_poll_seconds))
        while time.monotonic() < deadline:
            status_resp = await client.get(f"{status_base}/{job_id}", headers=headers)
            if status_resp.status_code >= 400:
                raise RunPodFaceError(
                    f"status HTTP {status_resp.status_code}: {status_resp.text[:300]}"
                )
            body = status_resp.json()
            status = str(body.get("status") or "").upper()
            if status in {"COMPLETED", "COMPLETED_SUCCESS"}:
                return _results_from_output(body.get("output"), fallback_ts=[e["frame_ts"] for e in encoded])
            if status in {"FAILED", "CANCELLED", "TIMED_OUT", "ERROR"}:
                raise RunPodFaceError(
                    f"job {job_id} {status}: {body.get('error') or body}"
                )
            await asyncio.sleep(poll)

    raise RunPodFaceError(
        f"job {job_id} timed out after {settings.runpod_face_timeout_seconds:.0f}s"
    )


def _results_from_output(
    output: Any,
    *,
    fallback_ts: list[float],
) -> list[FrameFaceResult]:
    rows: list[Any]
    if isinstance(output, dict):
        rows = list(output.get("frames") or output.get("results") or [])
    elif isinstance(output, list):
        rows = output
    else:
        rows = []

    out: list[FrameFaceResult] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        try:
            ts = float(row.get("frame_ts", fallback_ts[index] if index < len(fallback_ts) else 0.0))
        except (TypeError, ValueError, IndexError):
            ts = float(fallback_ts[index]) if index < len(fallback_ts) else 0.0
        faces_raw = row.get("faces") or row.get("detections") or []
        faces: list[DetectedFace] = []
        if isinstance(faces_raw, list):
            for face in faces_raw:
                parsed = _parse_face(face)
                if parsed is not None:
                    faces.append(parsed)
        out.append(FrameFaceResult(frame_ts=ts, faces=faces))
    return out


def _parse_face(raw: Any) -> DetectedFace | None:
    if not isinstance(raw, dict):
        return None
    emb = raw.get("embedding") or raw.get("normed_embedding")
    if not isinstance(emb, list) or len(emb) != 512:
        return None
    try:
        bbox = raw.get("bbox") or [
            raw.get("bbox_x", 0),
            raw.get("bbox_y", 0),
            (raw.get("bbox_x", 0) or 0) + (raw.get("bbox_width", 0) or 0),
            (raw.get("bbox_y", 0) or 0) + (raw.get("bbox_height", 0) or 0),
        ]
        if isinstance(bbox, dict):
            x1 = float(bbox.get("x1", bbox.get("x", 0)))
            y1 = float(bbox.get("y1", bbox.get("y", 0)))
            x2 = float(bbox.get("x2", x1 + float(bbox.get("w", bbox.get("width", 0)))))
            y2 = float(bbox.get("y2", y1 + float(bbox.get("h", bbox.get("height", 0)))))
        else:
            x1, y1, x2, y2 = [float(v) for v in bbox[:4]]
        # Accept either xyxy or xywh.
        if x2 <= x1 or y2 <= y1:
            w = float(raw.get("bbox_width") or 0)
            h = float(raw.get("bbox_height") or 0)
            x1 = float(raw.get("bbox_x") or x1)
            y1 = float(raw.get("bbox_y") or y1)
            x2, y2 = x1 + w, y1 + h
        conf = float(raw.get("confidence", raw.get("det_score", 0.0)) or 0.0)
    except (TypeError, ValueError):
        return None
    return DetectedFace(
        bbox_x=float(x1),
        bbox_y=float(y1),
        bbox_width=float(max(0.0, x2 - x1)),
        bbox_height=float(max(0.0, y2 - y1)),
        confidence=conf,
        embedding=[float(v) for v in emb],
        thumbnail_jpeg=b"",
    )
