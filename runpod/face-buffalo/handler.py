"""RunPod serverless handler: InsightFace buffalo_l detect (no DB writes)."""

from __future__ import annotations

import base64
import logging
import os
import queue
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import runpod

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dfi-face-buffalo")

MODEL_NAME = "buffalo_l"
DET_SIZE = (640, 640)
MIN_CONFIDENCE = 0.5
GPU_WORKERS = max(1, int(os.environ.get("FACE_GPU_WORKERS", "8")))
GPU_MEM_LIMIT_GB = max(1, int(os.environ.get("FACE_GPU_MEM_LIMIT_GB", "2")))

_pool: queue.Queue | None = None
_executor: ThreadPoolExecutor | None = None
_providers: list[str] = []


def _cuda_provider() -> tuple:
    return (
        "CUDAExecutionProvider",
        {
            "device_id": 0,
            "arena_extend_strategy": "kSameAsRequested",
            "gpu_mem_limit": GPU_MEM_LIMIT_GB * 1024 * 1024 * 1024,
            "cudnn_conv_algo_search": "HEURISTIC",
            "do_copy_in_default_stream": True,
        },
    )


def _make_app():
    from insightface.app import FaceAnalysis

    app = FaceAnalysis(name=MODEL_NAME, providers=[_cuda_provider(), "CPUExecutionProvider"])
    app.prepare(ctx_id=0, det_size=DET_SIZE)
    session = app.models.get("detection")
    used = list(session.session.get_providers())  # type: ignore[union-attr]
    if "CUDAExecutionProvider" not in used:
        raise RuntimeError(f"buffalo_l did not bind CUDA; providers={used}")
    return app, used


def _ensure_pool() -> None:
    global _pool, _executor, _providers
    if _pool is not None:
        return
    import onnxruntime as ort

    logger.info(
        "ORT %s available=%s workers=%s mem_limit_gb=%s",
        ort.__version__,
        ort.get_available_providers(),
        GPU_WORKERS,
        GPU_MEM_LIMIT_GB,
    )
    apps = []
    used = []
    for i in range(GPU_WORKERS):
        app, used = _make_app()
        apps.append(app)
        logger.info("Loaded CUDA replica %s/%s providers=%s", i + 1, GPU_WORKERS, used)
    _providers = used
    _pool = queue.Queue()
    for app in apps:
        _pool.put(app)
    _executor = ThreadPoolExecutor(max_workers=GPU_WORKERS, thread_name_prefix="buffalo")


def _decode_jpeg_png(image_b64: str) -> np.ndarray:
    import cv2

    raw = base64.b64decode(image_b64)
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Could not decode image bytes (need JPEG or PNG)")
    return image


def _faces_from_app(app, image_bgr: np.ndarray, min_confidence: float) -> tuple[list[dict], float]:
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
    assert _pool is not None
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
    app = _pool.get()
    try:
        faces, detect_ms = _faces_from_app(app, image, min_confidence)
    finally:
        _pool.put(app)
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
    _ensure_pool()
    if inp.get("healthcheck"):
        return {
            "ok": True,
            "model": MODEL_NAME,
            "providers": _providers,
            "gpu_workers": GPU_WORKERS,
        }

    min_confidence = float(inp.get("min_detection_confidence") or MIN_CONFIDENCE)
    items = inp.get("images")
    if not items and inp.get("image_b64"):
        items = [{"drive_file_id": inp.get("drive_file_id") or "", "image_b64": inp["image_b64"]}]
    if not items:
        return {"error": "images[] or image_b64 is required"}

    assert _executor is not None
    t0 = time.perf_counter()
    futures = [_executor.submit(_run_one, item, min_confidence) for item in items]
    results = [fut.result() for fut in futures]
    batch_ms = (time.perf_counter() - t0) * 1000.0
    return {
        "model": MODEL_NAME,
        "providers": _providers,
        "gpu_workers": GPU_WORKERS,
        "batch_size": len(results),
        "batch_ms": round(batch_ms, 2),
        "images": results,
    }


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
