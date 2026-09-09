"""RunPod buffalo_l GPU client. Stills are JPEG bytes; video is a signed pull URL."""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from app.config import Settings, get_settings
from app.faces.engine import DetectedFace
from app.faces.video_pull import signed_face_video_pull_url

logger = logging.getLogger(__name__)

_REST = "https://rest.runpod.io/v1"
_RUN = "https://api.runpod.ai/v2"
_scaled: tuple[int, int] | None = None
_MAX_BATCH_IMAGES = 16
_MAX_BATCH_BYTES = 6 * 1024 * 1024


class RunPodFaceError(RuntimeError):
    pass


def runpod_face_configured(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    endpoint = (settings.runpod_face_endpoint_id or "").strip()
    key = (settings.runpod_api_key or "").strip()
    qwen = (settings.runpod_qwen_endpoint_id or "").strip()
    if not settings.runpod_face_gpu_enabled:
        return False
    if not endpoint or not key:
        return False
    if qwen and endpoint == qwen:
        logger.error("Refusing face GPU: endpoint matches Qwen identify")
        return False
    return True


def jpeg_scale(height: int, width: int, max_edge: int) -> float:
    if max_edge <= 0:
        return 1.0
    return min(1.0, max_edge / max(height, width, 1))


def encode_face_jpeg(
    image_bgr: np.ndarray,
    *,
    max_edge: int = 0,
    quality: int = 95,
) -> bytes:
    import cv2

    h, w = image_bgr.shape[:2]
    scale = jpeg_scale(h, w, max_edge)
    if scale < 1.0:
        image_bgr = cv2.resize(
            image_bgr,
            (max(1, int(w * scale)), max(1, int(h * scale))),
            interpolation=cv2.INTER_AREA,
        )
    ok, buf = cv2.imencode(".jpg", image_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RunPodFaceError("JPEG encode failed")
    return buf.tobytes()


def _thumbnail(image_bgr: np.ndarray, bbox_x: float, bbox_y: float, bbox_w: float, bbox_h: float) -> bytes:
    import cv2

    h, w = image_bgr.shape[:2]
    x1, y1 = max(0, int(bbox_x)), max(0, int(bbox_y))
    x2, y2 = min(w, int(bbox_x + bbox_w)), min(h, int(bbox_y + bbox_h))
    crop = image_bgr[y1:y2, x1:x2]
    if crop.size == 0:
        return b""
    ok, buf = cv2.imencode(".jpg", crop)
    return buf.tobytes() if ok else b""


def _face_from_dict(face: dict[str, Any]) -> DetectedFace | None:
    embedding = face.get("embedding")
    if not isinstance(embedding, list) or len(embedding) < 128:
        return None
    thumb = b""
    raw_b64 = face.get("thumbnail_b64")
    if isinstance(raw_b64, str) and raw_b64:
        try:
            thumb = base64.b64decode(raw_b64)
        except Exception:  # noqa: BLE001
            thumb = b""
    return DetectedFace(
        bbox_x=float(face.get("bbox_x") or 0.0),
        bbox_y=float(face.get("bbox_y") or 0.0),
        bbox_width=float(face.get("bbox_width") or 0.0),
        bbox_height=float(face.get("bbox_height") or 0.0),
        confidence=float(face.get("confidence") or 0.0),
        embedding=[float(x) for x in embedding],
        thumbnail_jpeg=thumb,
    )


def parse_face_output(output: dict[str, Any]) -> list[DetectedFace]:
    rows = parse_face_output_images(output)
    return rows[0] if rows else []


def parse_face_output_images(output: dict[str, Any]) -> list[list[DetectedFace]]:
    if output.get("error"):
        raise RunPodFaceError(str(output.get("error")))
    images = output.get("images")
    rows: list[dict[str, Any]]
    if isinstance(images, list) and images:
        rows = [row if isinstance(row, dict) else {} for row in images]
    elif isinstance(output.get("faces"), list):
        rows = [output]
    else:
        rows = [{}]
    parsed: list[list[DetectedFace]] = []
    for row in rows:
        if row.get("error"):
            raise RunPodFaceError(str(row.get("error")))
        faces = row.get("faces") or []
        out: list[DetectedFace] = []
        for face in faces:
            if not isinstance(face, dict):
                continue
            detected = _face_from_dict(face)
            if detected is not None:
                out.append(detected)
        parsed.append(out)
    return parsed


def _scale_faces(faces: list[DetectedFace], scale: float) -> list[DetectedFace]:
    if scale >= 0.999:
        return faces
    inv = 1.0 / scale
    return [
        DetectedFace(
            bbox_x=face.bbox_x * inv,
            bbox_y=face.bbox_y * inv,
            bbox_width=face.bbox_width * inv,
            bbox_height=face.bbox_height * inv,
            confidence=face.confidence,
            embedding=face.embedding,
            thumbnail_jpeg=face.thumbnail_jpeg,
        )
        for face in faces
    ]


def _with_thumbnails(faces: list[DetectedFace], image_bgr: np.ndarray) -> list[DetectedFace]:
    return [
        DetectedFace(
            bbox_x=face.bbox_x,
            bbox_y=face.bbox_y,
            bbox_width=face.bbox_width,
            bbox_height=face.bbox_height,
            confidence=face.confidence,
            embedding=face.embedding,
            thumbnail_jpeg=face.thumbnail_jpeg
            or _thumbnail(image_bgr, face.bbox_x, face.bbox_y, face.bbox_width, face.bbox_height),
        )
        for face in faces
    ]


def build_face_payload(
    image_jpeg: bytes,
    *,
    drive_file_id: str = "",
    min_confidence: float = 0.5,
) -> dict[str, Any]:
    if not image_jpeg:
        raise RunPodFaceError("image bytes are required")
    return {
        "drive_file_id": drive_file_id,
        "image_b64": base64.b64encode(image_jpeg).decode("ascii"),
        "min_detection_confidence": min_confidence,
    }


def build_face_batch_payload(
    items: list[tuple[bytes, str]],
    *,
    min_confidence: float = 0.5,
) -> dict[str, Any]:
    if not items:
        raise RunPodFaceError("images are required")
    return {
        "min_detection_confidence": min_confidence,
        "images": [
            {
                "drive_file_id": drive_file_id,
                "image_b64": base64.b64encode(jpeg).decode("ascii"),
            }
            for jpeg, drive_file_id in items
        ],
    }


def build_face_video_payload(
    timestamps: list[float],
    *,
    video_url: str,
    drive_file_id: str = "",
    min_confidence: float = 0.5,
    video_suffix: str = ".mp4",
    video_max_bytes: int = 10 * 1024 * 1024 * 1024,
) -> dict[str, Any]:
    if not (video_url or "").strip():
        raise RunPodFaceError("video_url is required")
    if not timestamps:
        raise RunPodFaceError("timestamps are required")
    return {
        "drive_file_id": drive_file_id,
        "video_url": video_url.strip(),
        "video_suffix": video_suffix or ".mp4",
        "video_max_bytes": int(video_max_bytes),
        "timestamps": [round(float(t), 3) for t in timestamps],
        "min_detection_confidence": min_confidence,
    }


def payload_contains_drive_url(payload: dict[str, Any]) -> bool:
    blob = str(payload).casefold()
    return "drive.google.com" in blob or "googleapis.com/drive" in blob


def serverless_worker_scale(*, active: bool) -> dict[str, int]:
    """Serverless endpoint size: one worker under load, scale to zero when idle.

    Never uses a dedicated pod. ``workersMin=1`` keeps a worker while jobs run;
    idle sets min and max to 0 so RunPod can autoscale off.
    """
    if active:
        return {"workersMin": 1, "workersMax": 1}
    return {"workersMin": 0, "workersMax": 0}


async def set_face_workers_max(settings: Settings, workers_max: int) -> None:
    """Scale the face **serverless** endpoint. ``workers_max>0`` means load."""
    global _scaled
    desired = serverless_worker_scale(active=workers_max > 0)
    scaled = (desired["workersMin"], desired["workersMax"])
    if _scaled == scaled:
        return
    endpoint = (settings.runpod_face_endpoint_id or "").strip()
    key = (settings.runpod_api_key or "").strip()
    if not endpoint or not key:
        return
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.patch(
            f"{_REST}/endpoints/{endpoint}",
            headers=headers,
            json=desired,
        )
    if resp.status_code >= 400:
        raise RunPodFaceError(f"scale serverless workers={desired} failed {resp.status_code}")
    _scaled = scaled
    logger.info(
        "face_gpu_serverless min=%s max=%s endpoint=%s",
        scaled[0],
        scaled[1],
        endpoint[:12],
    )


def _chunk_jpegs(encoded: list[tuple[bytes, str, float, np.ndarray]]) -> list[list[tuple[bytes, str, float, np.ndarray]]]:
    batches: list[list[tuple[bytes, str, float, np.ndarray]]] = []
    current: list[tuple[bytes, str, float, np.ndarray]] = []
    current_bytes = 0
    for item in encoded:
        jpeg_len = len(item[0])
        if current and (
            len(current) >= _MAX_BATCH_IMAGES or current_bytes + jpeg_len > _MAX_BATCH_BYTES
        ):
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(item)
        current_bytes += jpeg_len
    if current:
        batches.append(current)
    return batches


async def _run_face_job(payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    if payload_contains_drive_url(payload):
        raise RunPodFaceError("Refusing to send a Drive URL to RunPod")
    await set_face_workers_max(settings, 1)
    endpoint = settings.runpod_face_endpoint_id.strip()
    headers = {
        "Authorization": f"Bearer {settings.runpod_api_key.strip()}",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(settings.runpod_face_timeout_seconds, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        submit = await client.post(
            f"{_RUN}/{endpoint}/run",
            headers=headers,
            json={"input": payload},
        )
        if submit.status_code >= 400:
            raise RunPodFaceError(f"run HTTP {submit.status_code}: {submit.text[:300]}")
        body = submit.json()
        job_id = body.get("id")
        if not job_id:
            raise RunPodFaceError(f"run missing id: {body}")
        status_url = f"{_RUN}/{endpoint}/status/{job_id}"
        deadline = time.monotonic() + settings.runpod_face_timeout_seconds
        status = ""
        while time.monotonic() < deadline:
            status_resp = await client.get(status_url, headers=headers)
            if status_resp.status_code >= 400:
                raise RunPodFaceError(
                    f"status HTTP {status_resp.status_code}: {status_resp.text[:300]}"
                )
            data = status_resp.json()
            status = str(data.get("status") or "")
            if status in {"COMPLETED", "completed"}:
                output = data.get("output") or {}
                if not isinstance(output, dict):
                    raise RunPodFaceError("RunPod output was not an object")
                return output
            if status in {"FAILED", "failed", "CANCELLED", "cancelled", "TIMED_OUT", "timed_out"}:
                raise RunPodFaceError(f"job {job_id} {status}: {data.get('error') or data}")
            await asyncio.sleep(0.5)
        raise RunPodFaceError(f"job {job_id} still {status} after timeout")


async def detect_faces_runpod(
    image_bgr: np.ndarray,
    *,
    drive_file_id: str = "",
    settings: Settings | None = None,
) -> list[DetectedFace]:
    rows = await detect_faces_runpod_batch(
        [image_bgr],
        drive_file_id=drive_file_id,
        settings=settings,
    )
    return rows[0] if rows else []


async def detect_faces_runpod_batch(
    images_bgr: list[np.ndarray],
    *,
    drive_file_id: str = "",
    settings: Settings | None = None,
) -> list[list[DetectedFace]]:
    settings = settings or get_settings()
    if not runpod_face_configured(settings):
        raise RunPodFaceError("RunPod face GPU is not configured")
    if not images_bgr:
        return []

    encoded: list[tuple[bytes, str, float, np.ndarray]] = []
    for image_bgr in images_bgr:
        h, w = image_bgr.shape[:2]
        jpeg = encode_face_jpeg(
            image_bgr,
            max_edge=settings.runpod_face_max_edge,
            quality=settings.runpod_face_jpeg_quality,
        )
        scale = jpeg_scale(h, w, settings.runpod_face_max_edge)
        encoded.append((jpeg, drive_file_id, scale, image_bgr))

    out: list[list[DetectedFace]] = []
    for batch in _chunk_jpegs(encoded):
        payload = build_face_batch_payload(
            [(jpeg, did) for jpeg, did, _scale, _img in batch],
            min_confidence=settings.min_detection_confidence,
        )
        output = await _run_face_job(payload, settings)
        parsed = parse_face_output_images(output)
        if len(parsed) != len(batch):
            raise RunPodFaceError(
                f"RunPod returned {len(parsed)} image(s) for batch of {len(batch)}"
            )
        for (_jpeg, _did, scale, image_bgr), faces in zip(batch, parsed, strict=True):
            finished = _with_thumbnails(_scale_faces(faces, scale), image_bgr)
            out.append(finished)
        logger.info(
            "face_gpu_batch file=%s n=%d faces=%s providers=%s",
            drive_file_id[:12],
            len(batch),
            [len(row) for row in parsed],
            output.get("providers"),
        )
    return out


async def detect_faces_runpod_video(
    video_path: str,
    timestamps: list[float],
    *,
    drive_file_id: str = "",
    settings: Settings | None = None,
) -> list[tuple[float, list[DetectedFace], int, int]]:
    """API already downloaded the file. GPU worker Range-GETs it, then ffmpeg + buffalo_l."""
    settings = settings or get_settings()
    if not runpod_face_configured(settings):
        raise RunPodFaceError("RunPod face GPU is not configured")
    if not timestamps:
        return []
    path = Path(video_path)
    if not path.is_file():
        raise RunPodFaceError(f"cached video missing: {path}")
    size = int(path.stat().st_size)
    max_bytes = max(1, int(settings.runpod_face_video_max_bytes))
    if size > max_bytes:
        raise RunPodFaceError(
            f"video {size} bytes exceeds runpod_face_video_max_bytes={max_bytes}"
        )
    try:
        video_url = signed_face_video_pull_url(drive_file_id, settings)
    except ValueError as exc:
        raise RunPodFaceError(str(exc)) from exc
    payload = build_face_video_payload(
        timestamps,
        video_url=video_url,
        drive_file_id=drive_file_id,
        min_confidence=settings.min_detection_confidence,
        video_suffix=path.suffix or ".mp4",
        video_max_bytes=max_bytes,
    )
    output = await _run_face_job(payload, settings)
    if output.get("error"):
        raise RunPodFaceError(str(output.get("error")))
    rows: list[tuple[float, list[DetectedFace], int, int]] = []
    for frame in output.get("frames") or []:
        if not isinstance(frame, dict):
            continue
        if frame.get("error"):
            logger.warning(
                "face_gpu_video_frame_skip ts=%s err=%s",
                frame.get("timestamp"),
                frame.get("error"),
            )
            continue
        faces = []
        for face in frame.get("faces") or []:
            if not isinstance(face, dict):
                continue
            detected = _face_from_dict(face)
            if detected is not None:
                faces.append(detected)
        rows.append(
            (
                float(frame.get("timestamp") or 0.0),
                faces,
                int(frame.get("width") or 0),
                int(frame.get("height") or 0),
            )
        )
    logger.info(
        "face_gpu_video file=%s frames=%d ffmpeg=%s providers=%s",
        drive_file_id[:12],
        len(rows),
        output.get("ffmpeg"),
        output.get("providers"),
    )
    return rows
