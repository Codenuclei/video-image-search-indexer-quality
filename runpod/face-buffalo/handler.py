"""RunPod serverless: buffalo_l detect + batched ArcFace (same 512-d space). No DB writes."""

from __future__ import annotations

import base64
import logging
import os
import queue
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import runpod

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dfi-face-buffalo")

MODEL_NAME = "buffalo_l"
DET_SIZE = (640, 640)
MIN_CONFIDENCE = 0.5
REC_BATCH = max(1, int(os.environ.get("FACE_REC_BATCH", "32")))
VRAM_RESERVE_MB = max(512, int(os.environ.get("FACE_VRAM_RESERVE_MB", "2048")))
DET_MEM_LIMIT_GB = max(1, int(os.environ.get("FACE_DET_MEM_LIMIT_GB", "2")))
REC_MEM_LIMIT_GB = max(1, int(os.environ.get("FACE_REC_MEM_LIMIT_GB", "4")))
MAX_DET_WORKERS = max(1, int(os.environ.get("FACE_MAX_DET_WORKERS", "16")))
REQUESTED_DET_WORKERS = int(os.environ.get("FACE_GPU_WORKERS", "0"))

_det_pool: queue.Queue | None = None
_det_executor: ThreadPoolExecutor | None = None
_rec = None
_rec_batched = False
_rec_lock = threading.Lock()
_providers: list[str] = []
_runtime: dict = {}


def _nvidia_smi() -> dict:
    try:
        raw = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=5,
        ).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": str(exc)}
    name, total, used, free, util = [p.strip() for p in raw.split(",")]
    return {
        "name": name,
        "total_mb": int(float(total)),
        "used_mb": int(float(used)),
        "free_mb": int(float(free)),
        "util_pct": int(float(util)),
    }


def _cuda_provider(mem_limit_gb: int) -> tuple:
    return (
        "CUDAExecutionProvider",
        {
            "device_id": 0,
            "arena_extend_strategy": "kSameAsRequested",
            "gpu_mem_limit": mem_limit_gb * 1024 * 1024 * 1024,
            "cudnn_conv_algo_search": "HEURISTIC",
            "do_copy_in_default_stream": True,
        },
    )


def _providers_list(mem_limit_gb: int) -> list:
    return [_cuda_provider(mem_limit_gb), "CPUExecutionProvider"]


def _assert_cuda(session) -> list[str]:
    used = list(session.get_providers())
    if "CUDAExecutionProvider" not in used:
        raise RuntimeError(f"did not bind CUDA; providers={used}")
    return used


def _buffalo_dir() -> Path:
    homes = []
    env_home = os.environ.get("INSIGHTFACE_HOME", "").strip()
    if env_home:
        homes.append(Path(env_home))
    homes.append(Path.home() / ".insightface")
    for home in homes:
        path = home / "models" / MODEL_NAME
        if (path / "w600k_r50.onnx").is_file():
            return path
    return homes[0] / "models" / MODEL_NAME


def _dynamic_batch_onnx(src: Path) -> Path:
    import onnx

    dst = Path("/tmp") / f"{src.stem}_dynbatch.onnx"
    if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    model = onnx.load(str(src))

    def _dyn(vi) -> None:
        shape = vi.type.tensor_type.shape
        if not shape.dim:
            return
        shape.dim[0].ClearField("dim_value")
        shape.dim[0].dim_param = "N"

    for vi in list(model.graph.input) + list(model.graph.output) + list(model.graph.value_info):
        _dyn(vi)
    onnx.save(model, str(dst))
    return dst


def _load_rec():
    import onnxruntime as ort
    from insightface.model_zoo.arcface_onnx import ArcFaceONNX

    src = _buffalo_dir() / "w600k_r50.onnx"
    if not src.is_file():
        raise RuntimeError(f"missing ArcFace weights at {src}")
    dummy = [np.zeros((112, 112, 3), dtype=np.uint8) for _ in range(min(8, REC_BATCH))]
    dyn = _dynamic_batch_onnx(src)
    sess = ort.InferenceSession(str(dyn), providers=_providers_list(REC_MEM_LIMIT_GB))
    used = _assert_cuda(sess)
    rec = ArcFaceONNX(model_file=str(dyn), session=sess)
    batched = True
    try:
        rec.get_feat(dummy)
    except Exception as exc:  # noqa: BLE001
        logger.warning("dynamic-batch ArcFace failed (%s); sequential fallback", exc)
        sess = ort.InferenceSession(str(src), providers=_providers_list(REC_MEM_LIMIT_GB))
        used = _assert_cuda(sess)
        rec = ArcFaceONNX(model_file=str(src), session=sess)
        rec.get_feat(dummy[:1])
        batched = False
    logger.info("ArcFace ready providers=%s batched=%s", used, batched)
    return rec, used, batched


def _load_det():
    from insightface.app import FaceAnalysis

    app = FaceAnalysis(
        name=MODEL_NAME,
        allowed_modules=["detection"],
        providers=_providers_list(DET_MEM_LIMIT_GB),
    )
    app.prepare(ctx_id=0, det_size=DET_SIZE)
    session = app.models["detection"].session
    used = _assert_cuda(session)
    return app, used


def _ensure_runtime() -> None:
    global _det_pool, _det_executor, _rec, _rec_batched, _providers, _runtime
    if _det_pool is not None:
        return
    import onnxruntime as ort

    vram0 = _nvidia_smi()
    logger.info("ORT %s available=%s vram=%s", ort.__version__, ort.get_available_providers(), vram0)
    first, det_providers = _load_det()
    _providers = det_providers
    dets = [first]
    vram_det = _nvidia_smi()
    per_mb = max(512, int(vram_det.get("used_mb", 0) - int(vram0.get("used_mb", 0))))
    rec, rec_providers, rec_batched = _load_rec()
    _rec = rec
    _rec_batched = rec_batched
    _providers = rec_providers
    vram1 = _nvidia_smi()
    if REQUESTED_DET_WORKERS > 0:
        target = min(MAX_DET_WORKERS, REQUESTED_DET_WORKERS)
    else:
        free = int(vram1.get("free_mb") or 0)
        extra = max(0, (free - VRAM_RESERVE_MB) // per_mb)
        target = min(MAX_DET_WORKERS, 1 + extra)
    while len(dets) < target:
        dets.append(_load_det()[0])
        now = _nvidia_smi()
        if int(now.get("free_mb") or 0) < VRAM_RESERVE_MB:
            break

    _det_pool = queue.Queue()
    for app in dets:
        _det_pool.put(app)
    _det_executor = ThreadPoolExecutor(max_workers=len(dets), thread_name_prefix="det")
    vram = _nvidia_smi()
    _runtime = {
        "det_workers": len(dets),
        "rec_batch": REC_BATCH,
        "rec_mem_limit_gb": REC_MEM_LIMIT_GB,
        "det_mem_limit_gb": DET_MEM_LIMIT_GB,
        "vram_before": vram0,
        "vram_loaded": vram,
        "det_mb_each": per_mb,
        "rec_batched": rec_batched,
        "model": MODEL_NAME,
        "det_size": list(DET_SIZE),
    }
    logger.info("Runtime %s", _runtime)


def _decode_jpeg_png(image_b64: str) -> np.ndarray:
    import cv2

    raw = base64.b64decode(image_b64)
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Could not decode image bytes (need JPEG or PNG)")
    return image


def _detect_one(app, image_bgr: np.ndarray, min_confidence: float) -> list[dict]:
    from insightface.utils import face_align

    bboxes, kpss = app.det_model.detect(image_bgr, input_size=DET_SIZE, max_num=0)
    out: list[dict] = []
    if bboxes is None or len(bboxes) == 0:
        return out
    for i, row in enumerate(bboxes):
        confidence = float(row[4])
        if confidence < min_confidence:
            continue
        kps = None if kpss is None else kpss[i]
        if kps is None:
            continue
        x1, y1, x2, y2 = [float(v) for v in row[:4]]
        crop = face_align.norm_crop(image_bgr, landmark=kps, image_size=112)
        if crop is None:
            continue
        out.append(
            {
                "bbox_x": x1,
                "bbox_y": y1,
                "bbox_width": x2 - x1,
                "bbox_height": y2 - y1,
                "confidence": confidence,
                "crop": crop,
            }
        )
    return out


def _embed_crops(crops: list[np.ndarray]) -> np.ndarray:
    assert _rec is not None
    step = REC_BATCH if _rec_batched else 1
    chunks: list[np.ndarray] = []
    with _rec_lock:
        for i in range(0, len(crops), step):
            batch = crops[i : i + step]
            feats = np.asarray(_rec.get_feat(batch), dtype=np.float32)
            chunks.append(feats)
    feats = np.concatenate(chunks, axis=0) if chunks else np.zeros((0, 512), dtype=np.float32)
    norms = np.linalg.norm(feats, axis=1, keepdims=True)
    feats = feats / np.clip(norms, 1e-8, None)
    return feats


def _detect_item(item: dict, min_confidence: float) -> dict:
    assert _det_pool is not None
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
    app = _det_pool.get()
    try:
        t1 = time.perf_counter()
        faces = _detect_one(app, image, min_confidence)
        detect_ms = (time.perf_counter() - t1) * 1000.0
    finally:
        _det_pool.put(app)
    h, w = image.shape[:2]
    return {
        "drive_file_id": drive_file_id,
        "width": int(w),
        "height": int(h),
        "faces": faces,
        "decode_ms": round(decode_ms, 2),
        "detect_ms": round(detect_ms, 2),
    }


def handler(job: dict) -> dict:
    inp = job.get("input") or {}
    _ensure_runtime()
    if inp.get("healthcheck"):
        return {
            "ok": True,
            "model": MODEL_NAME,
            "providers": _providers,
            "gpu_workers": _runtime.get("det_workers"),
            "runtime": {**_runtime, "vram_now": _nvidia_smi()},
        }

    min_confidence = float(inp.get("min_detection_confidence") or MIN_CONFIDENCE)
    items = inp.get("images")
    if not items and inp.get("image_b64"):
        items = [{"drive_file_id": inp.get("drive_file_id") or "", "image_b64": inp["image_b64"]}]
    if not items:
        return {"error": "images[] or image_b64 is required"}

    assert _det_executor is not None
    t0 = time.perf_counter()
    futures = [_det_executor.submit(_detect_item, item, min_confidence) for item in items]
    detected = [fut.result() for fut in futures]
    crops: list[np.ndarray] = []
    owners: list[tuple[int, int]] = []
    for img_i, row in enumerate(detected):
        for face_i, face in enumerate(row.get("faces") or []):
            crop = face.pop("crop", None)
            if crop is None:
                continue
            crops.append(crop)
            owners.append((img_i, face_i))
    t_emb = time.perf_counter()
    if crops:
        embs = _embed_crops(crops)
        for (img_i, face_i), vec in zip(owners, embs, strict=True):
            detected[img_i]["faces"][face_i]["embedding"] = [round(float(x), 7) for x in vec.tolist()]
    embed_ms = (time.perf_counter() - t_emb) * 1000.0
    results = []
    for row in detected:
        faces = row.get("faces") or []
        results.append(
            {
                **{k: v for k, v in row.items() if k != "faces"},
                "face_count": len(faces),
                "faces": faces,
                "embed_ms": round(embed_ms, 2) if not row.get("error") else None,
            }
        )
    batch_ms = (time.perf_counter() - t0) * 1000.0
    return {
        "model": MODEL_NAME,
        "providers": _providers,
        "gpu_workers": _runtime.get("det_workers"),
        "rec_batch": REC_BATCH,
        "vram": _nvidia_smi(),
        "batch_size": len(results),
        "batch_ms": round(batch_ms, 2),
        "images": results,
    }


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
