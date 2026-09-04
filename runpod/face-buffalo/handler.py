"""RunPod serverless handler: InsightFace buffalo_l detect (no DB writes)."""

from __future__ import annotations

import base64
import logging
import time

import numpy as np
import runpod

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dfi-face-buffalo")

MODEL_NAME = "buffalo_l"
DET_SIZE = (640, 640)
MIN_CONFIDENCE = 0.5
CUDA_PROVIDER = (
    "CUDAExecutionProvider",
    {
        "device_id": 0,
        "arena_extend_strategy": "kNextPowerOfTwo",
        "gpu_mem_limit": 12 * 1024 * 1024 * 1024,
        "cudnn_conv_algo_search": "EXHAUSTIVE",
        "do_copy_in_default_stream": True,
    },
)

_app = None
_providers: list[str] = []


def _load_app():
    global _app, _providers
    if _app is not None:
        return _app
    from insightface.app import FaceAnalysis
    import onnxruntime as ort

    logger.info("ORT %s available=%s", ort.__version__, ort.get_available_providers())
    providers = [CUDA_PROVIDER, "CPUExecutionProvider"]
    logger.info("Loading InsightFace %s", MODEL_NAME)
    app = FaceAnalysis(name=MODEL_NAME, providers=providers)
    app.prepare(ctx_id=0, det_size=DET_SIZE)
    session = app.models.get("detection")
    used = []
    try:
        used = list(session.session.get_providers())  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        used = [p[0] if isinstance(p, tuple) else p for p in providers]
    if "CUDAExecutionProvider" not in used:
        raise RuntimeError(f"buffalo_l did not bind CUDA; providers={used}")
    _providers = used
    _app = app
    logger.info("InsightFace ready providers=%s", used)
    return app


def _decode_jpeg_png(image_b64: str) -> np.ndarray:
    import cv2

    raw = base64.b64decode(image_b64)
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Could not decode image bytes (need JPEG or PNG)")
    return image


def _detect(image_bgr: np.ndarray, min_confidence: float) -> tuple[list[dict], float]:
    app = _load_app()
    t0 = time.perf_counter()
    faces = app.get(image_bgr)
    detect_ms = (time.perf_counter() - t0) * 1000.0
    out: list[dict] = []
    for face in faces:
        confidence = float(getattr(face, "det_score", 0.0) or 0.0)
        if confidence < min_confidence:
            continue
        if getattr(face, "normed_embedding", None) is None:
            continue
        x1, y1, x2, y2 = face.bbox.astype(float)
        out.append(
            {
                "bbox_x": float(x1),
                "bbox_y": float(y1),
                "bbox_width": float(x2 - x1),
                "bbox_height": float(y2 - y1),
                "confidence": confidence,
            }
        )
    return out, detect_ms


def _run_one(item: dict, min_confidence: float) -> dict:
    drive_file_id = str(item.get("drive_file_id") or "")
    image_b64 = item.get("image_b64")
    if not image_b64:
        return {"drive_file_id": drive_file_id, "error": "image_b64 is required"}
    t0 = time.perf_counter()
    try:
        image = _decode_jpeg_png(image_b64)
    except Exception as exc:  # noqa: BLE001
        return {"drive_file_id": drive_file_id, "error": str(exc)}
    decode_ms = (time.perf_counter() - t0) * 1000.0
    faces, detect_ms = _detect(image, min_confidence)
    h, w = image.shape[:2]
    return {
        "drive_file_id": drive_file_id,
        "width": int(w),
        "height": int(h),
        "face_count": len(faces),
        "faces": faces,
        "decode_ms": round(decode_ms, 2),
        "detect_ms": round(detect_ms, 2),
    }


def handler(job: dict) -> dict:
    inp = job.get("input") or {}
    if inp.get("healthcheck"):
        _load_app()
        return {"ok": True, "model": MODEL_NAME, "providers": _providers}

    min_confidence = float(inp.get("min_detection_confidence") or MIN_CONFIDENCE)
    items = inp.get("images")
    if not items and inp.get("image_b64"):
        items = [{"drive_file_id": inp.get("drive_file_id") or "", "image_b64": inp["image_b64"]}]
    if not items:
        return {"error": "images[] or image_b64 is required"}

    _load_app()
    t0 = time.perf_counter()
    results = [_run_one(item, min_confidence) for item in items]
    batch_ms = (time.perf_counter() - t0) * 1000.0
    return {
        "model": MODEL_NAME,
        "providers": _providers,
        "batch_size": len(results),
        "batch_ms": round(batch_ms, 2),
        "images": results,
    }


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
