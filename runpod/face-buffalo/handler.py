"""RunPod serverless: FaceEngine.detect_faces on CUDA, plus NVDEC ffmpeg for video.

InsightFace buffalo_l FaceAnalysis.get — not a split det+rec path.
Video: signed HTTPS Range GET into /tmp, then ffmpeg -hwaccel cuda (NVDEC)
then software ffmpeg on this worker. No Drive. No Postgres. Always delete /tmp leftovers.
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import numpy as np

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dfi-face-buffalo")

MODEL_NAME = "buffalo_l"
DET_SIZE = (640, 640)
MIN_CONFIDENCE = 0.5
DEFAULT_VIDEO_MAX_BYTES = 10 * 1024 * 1024 * 1024
DEFAULT_VIDEO_MAX_FRAMES = 80
DEFAULT_RESPONSE_MAX_BYTES = 48 * 1024 * 1024
DOWNLOAD_PARTS = 8
DOWNLOAD_CHUNK = 8 * 1024 * 1024
RANGE_MIN_BYTES = 256 * 1024

_app = None
_providers: list[str] = []
_runtime: dict = {}
_lock = threading.Lock()


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
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) < 5:
        return {"error": raw[:200]}
    name, total, used, free, util = parts[:5]
    try:
        return {
            "name": name,
            "total_mb": int(float(total)),
            "used_mb": int(float(used)),
            "free_mb": int(float(free)),
            "util_pct": int(float(util)),
        }
    except (TypeError, ValueError):
        return {"name": name, "error": raw[:240]}


def _ffmpeg_hwaccels() -> list[str]:
    try:
        raw = subprocess.check_output(
            ["ffmpeg", "-hide_banner", "-hwaccels"],
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    names = []
    for line in raw.splitlines():
        name = line.strip().lower()
        if name and name != "hardware acceleration methods:":
            names.append(name)
    return names


def extract_frame_ffmpeg(
    video_path: str,
    timestamp_sec: float,
    output_path: str,
    *,
    max_width: int = 0,
    require_nvdec: bool = False,
) -> str:
    """NVDEC first (``-hwaccel cuda``), then software ffmpeg on this worker.

    Same seek as CPU ``extract_frame_at`` (``-ss`` before ``-i``). Returns ``nvdec`` or ``cpu``.
    ``max_width`` scales the JPEG for index-time extracts (smaller RunPod payload).
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    ss = f"{float(timestamp_sec):.3f}"
    scale: list[str] = []
    if max_width and int(max_width) > 0:
        scale = ["-vf", f"scale='min({int(max_width)},iw)':-2"]
    nvdec = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-hwaccel",
        "cuda",
        "-ss",
        ss,
        "-i",
        video_path,
        *scale,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        output_path,
    ]
    proc = subprocess.run(nvdec, capture_output=True, timeout=120)
    if proc.returncode == 0 and Path(output_path).is_file() and Path(output_path).stat().st_size > 0:
        return "nvdec"
    if require_nvdec:
        err = (proc.stderr or b"").decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"NVDEC extract failed at {ss}s: {err}")
    cpu = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        ss,
        "-i",
        video_path,
        *scale,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        output_path,
    ]
    proc = subprocess.run(cpu, capture_output=True, timeout=120)
    if proc.returncode == 0 and Path(output_path).is_file() and Path(output_path).stat().st_size > 0:
        return "cpu"
    err = (proc.stderr or b"").decode("utf-8", errors="replace")[:300]
    raise RuntimeError(f"ffmpeg extract failed at {ss}s: {err}")


def video_url_is_blocked(url: str) -> bool:
    lowered = (url or "").casefold()
    return "drive.google.com" in lowered or "googleapis.com/drive" in lowered


def _http_open(url: str, headers: dict[str, str] | None = None, timeout: float = 600.0):
    req = urllib.request.Request(url, headers=headers or {})
    return urllib.request.urlopen(req, timeout=timeout)


def _parse_content_range_total(header: str) -> int:
    # bytes 0-0/12345
    if not header or "/" not in header:
        return 0
    total = header.rsplit("/", 1)[-1].strip()
    if total == "*":
        return 0
    return int(total)


def _stream_copy(url: str, dest: str, max_bytes: int) -> int:
    written = 0
    with _http_open(url) as resp, open(dest, "wb") as out:
        while True:
            chunk = resp.read(DOWNLOAD_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                raise RuntimeError(f"download exceeded max_bytes={max_bytes}")
            out.write(chunk)
    return written


def _write_range(url: str, dest: str, start: int, end: int) -> None:
    headers = {"Range": f"bytes={start}-{end}"}
    with _http_open(url, headers=headers) as resp:
        if getattr(resp, "status", 200) != 206:
            raise RuntimeError(f"range GET expected 206, got {getattr(resp, 'status', '?')}")
        remaining = end - start + 1
        with open(dest, "r+b") as out:
            out.seek(start)
            while remaining > 0:
                chunk = resp.read(min(DOWNLOAD_CHUNK, remaining))
                if not chunk:
                    raise RuntimeError("range GET truncated")
                out.write(chunk)
                remaining -= len(chunk)


def download_http_file(url: str, dest: str, *, max_bytes: int, parts: int = DOWNLOAD_PARTS) -> dict:
    """Parallel HTTP Range GET — fastest pull into the worker without stuffing JSON."""
    if video_url_is_blocked(url):
        raise RuntimeError("refusing Drive URL")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("video_url must be http(s)")
    if max_bytes <= 0:
        raise RuntimeError("max_bytes must be positive")

    total = 0
    ranged = False
    probe = None
    try:
        probe = _http_open(url, headers={"Range": "bytes=0-0"}, timeout=60.0)
        status = int(getattr(probe, "status", 200))
        if status == 206:
            total = _parse_content_range_total(probe.headers.get("Content-Range") or "")
            ranged = total > 0
        elif status == 200:
            total = int(probe.headers.get("Content-Length") or 0)
    except urllib.error.HTTPError as exc:
        try:
            exc.close()
        except Exception:  # noqa: BLE001
            pass
        if exc.code in {400, 405, 416, 501}:
            ranged = False
            total = 0
        else:
            raise RuntimeError(f"download probe HTTP {exc.code}") from exc
    finally:
        if probe is not None:
            probe.close()

    if total > max_bytes:
        raise RuntimeError(f"video {total} bytes exceeds max_bytes={max_bytes}")

    use_parts = min(max(1, int(parts)), DOWNLOAD_PARTS)
    if ranged and total > RANGE_MIN_BYTES and use_parts > 1:
        Path(dest).write_bytes(b"")
        with open(dest, "r+b") as out:
            out.truncate(total)
        span = total
        chunk = (span + use_parts - 1) // use_parts
        ranges = []
        start = 0
        while start < span:
            end = min(span - 1, start + chunk - 1)
            ranges.append((start, end))
            start = end + 1
        with ThreadPoolExecutor(max_workers=len(ranges)) as pool:
            list(pool.map(lambda r: _write_range(url, dest, r[0], r[1]), ranges))
        return {"bytes": total, "parts": len(ranges), "mode": "range"}

    written = _stream_copy(url, dest, max_bytes)
    return {"bytes": written, "parts": 1, "mode": "stream"}


def _assert_cuda_app(app) -> list[str]:
    used: list[str] = []
    models = getattr(app, "models", {}) or {}
    for name, model in models.items():
        session = getattr(model, "session", None)
        if session is None:
            continue
        providers = list(session.get_providers())
        if "CUDAExecutionProvider" not in providers:
            raise RuntimeError(f"{name} did not bind CUDA; providers={providers}")
        used = providers
    if not used:
        raise RuntimeError("no ONNX sessions found on FaceAnalysis")
    return used


def faces_from_insightface(image_bgr: np.ndarray, faces, min_confidence: float) -> list[dict]:
    """Same post-get extras as backend FaceEngine.detect_faces (CPU)."""
    results: list[dict] = []
    for face in faces:
        confidence = float(getattr(face, "det_score", 0.0) or 0.0)
        if confidence < min_confidence:
            continue
        bbox = face.bbox.astype(float)
        x1, y1, x2, y2 = bbox
        embedding = face.normed_embedding
        if embedding is None:
            continue
        thumb_b64 = ""
        try:
            import cv2

            h, w = image_bgr.shape[:2]
            ix1, iy1 = max(0, int(x1)), max(0, int(y1))
            ix2, iy2 = min(w, int(x2)), min(h, int(y2))
            crop = image_bgr[iy1:iy2, ix1:ix2]
            if crop.size > 0:
                ok, buf = cv2.imencode(".jpg", crop)
                if ok:
                    thumb_b64 = base64.b64encode(buf.tobytes()).decode("ascii")
        except Exception:  # noqa: BLE001
            pass
        results.append(
            {
                "bbox_x": float(x1),
                "bbox_y": float(y1),
                "bbox_width": float(x2 - x1),
                "bbox_height": float(y2 - y1),
                "confidence": confidence,
                "embedding": embedding.astype(float).tolist(),
                "thumbnail_b64": thumb_b64,
            }
        )
    return results


def _ensure_runtime() -> None:
    global _app, _providers, _runtime
    if _app is not None:
        return
    from insightface.app import FaceAnalysis

    vram0 = _nvidia_smi()
    logger.info("Loading InsightFace model %s on CUDA vram=%s", MODEL_NAME, vram0)
    app = FaceAnalysis(
        name=MODEL_NAME,
        providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
    )
    app.prepare(ctx_id=0, det_size=DET_SIZE)
    _providers = _assert_cuda_app(app)
    _app = app
    _runtime = {
        "model": MODEL_NAME,
        "det_size": list(DET_SIZE),
        "min_confidence": MIN_CONFIDENCE,
        "process": "FaceAnalysis.get",
        "ffmpeg_hwaccels": _ffmpeg_hwaccels(),
        "vram_before": vram0,
        "vram_loaded": _nvidia_smi(),
        "providers": list(_providers),
    }
    logger.info("Runtime %s", _runtime)


def _detect_bgr(image_bgr: np.ndarray, min_confidence: float) -> list[dict]:
    with _lock:
        assert _app is not None
        raw_faces = _app.get(image_bgr)
        return faces_from_insightface(image_bgr, raw_faces, min_confidence)


def _process_video(inp: dict, min_confidence: float) -> dict:
    import cv2

    drive_file_id = str(inp.get("drive_file_id") or "")
    video_url = str(inp.get("video_url") or "").strip()
    video_b64 = inp.get("video_b64")
    timestamps = inp.get("timestamps") or []
    if not video_url and not video_b64:
        return {"error": "video_url is required"}
    if video_url and video_url_is_blocked(video_url):
        return {"error": "refusing Drive URL"}
    if not isinstance(timestamps, list) or not timestamps:
        return {"error": "timestamps[] is required"}
    ts_list = sorted({round(float(t), 3) for t in timestamps})
    max_frames = max(1, int(inp.get("max_frames") or DEFAULT_VIDEO_MAX_FRAMES))
    if len(ts_list) > max_frames:
        return {"error": f"timestamps exceed max_frames={max_frames}", "retryable": False}
    extract_only = bool(inp.get("extract_only"))
    return_jpegs = bool(inp.get("return_jpegs") or extract_only)
    require_nvdec = bool(inp.get("require_nvdec"))
    max_width = int(inp.get("max_width") or 0)
    max_bytes = int(inp.get("video_max_bytes") or DEFAULT_VIDEO_MAX_BYTES)
    max_response_bytes = max(
        1, int(inp.get("max_response_bytes") or DEFAULT_RESPONSE_MAX_BYTES)
    )
    tmpdir = tempfile.mkdtemp(prefix="dfi-rface-video-")
    try:
        suffix = str(inp.get("video_suffix") or ".mp4")
        if not suffix.startswith("."):
            suffix = "." + suffix
        video_path = os.path.join(tmpdir, f"input{suffix}")
        download_ms = 0.0
        download_info: dict = {}
        if video_url:
            t_dl = time.perf_counter()
            try:
                download_info = download_http_file(video_url, video_path, max_bytes=max_bytes)
            except (OSError, urllib.error.URLError, RuntimeError, ValueError) as exc:
                return {"error": f"video download failed: {exc}"}
            download_ms = (time.perf_counter() - t_dl) * 1000.0
        else:
            raw = base64.b64decode(video_b64)
            if len(raw) > max_bytes:
                return {"error": f"video {len(raw)} bytes exceeds max_bytes={max_bytes}"}
            Path(video_path).write_bytes(raw)
            download_info = {"bytes": len(raw), "parts": 0, "mode": "b64"}
        t0 = time.perf_counter()

        def _one(ts: float) -> tuple[float, str, str]:
            out = os.path.join(tmpdir, f"{ts:.3f}.jpg")
            decoder = extract_frame_ffmpeg(
                video_path,
                ts,
                out,
                max_width=max_width if extract_only else 0,
                require_nvdec=require_nvdec,
            )
            return ts, out, decoder

        workers = min(8, max(1, len(ts_list)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            extracted = list(pool.map(_one, ts_list))
        decode_ms = (time.perf_counter() - t0) * 1000.0
        t1 = time.perf_counter()
        frames = []
        response_bytes = 0
        for ts, jpeg_path, decoder in extracted:
            row: dict = {"timestamp": ts, "decoder": decoder}
            if return_jpegs and Path(jpeg_path).is_file():
                jpeg = Path(jpeg_path).read_bytes()
                response_bytes += len(jpeg)
                if response_bytes > max_response_bytes:
                    return {
                        "error": f"JPEG evidence exceeds max_response_bytes={max_response_bytes}",
                        "retryable": False,
                    }
                row["jpeg_b64"] = base64.b64encode(jpeg).decode("ascii")
            if extract_only:
                frames.append(row)
                continue
            image = cv2.imread(jpeg_path)
            if image is None:
                row["error"] = "jpeg decode failed"
                frames.append(row)
                continue
            h, w = image.shape[:2]
            faces = _detect_bgr(image, min_confidence)
            row.update(
                {
                    "width": int(w),
                    "height": int(h),
                    "faces": faces,
                    "face_count": len(faces),
                }
            )
            frames.append(row)
        detect_ms = 0.0 if extract_only else (time.perf_counter() - t1) * 1000.0
        decoders = [row.get("decoder") for row in frames if row.get("decoder")]
        if decoders and all(d == "nvdec" for d in decoders):
            ffmpeg_mode = "nvdec"
        elif decoders and all(d == "cpu" for d in decoders):
            ffmpeg_mode = "cpu"
        else:
            ffmpeg_mode = "mixed"
        return {
            "drive_file_id": drive_file_id,
            "frames": frames,
            "frame_count": len(frames),
            "decode_ms": round(decode_ms, 2),
            "detect_ms": round(detect_ms, 2),
            "download_ms": round(download_ms, 2),
            "download": download_info,
            "ffmpeg": ffmpeg_mode,
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _decode_jpeg_png(image_b64: str) -> np.ndarray:
    import cv2

    raw = base64.b64decode(image_b64)
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Could not decode image bytes (need JPEG or PNG)")
    return image


def _detect_item(item: dict, min_confidence: float) -> dict:
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
    t1 = time.perf_counter()
    faces = _detect_bgr(image, min_confidence)
    detect_ms = (time.perf_counter() - t1) * 1000.0
    h, w = image.shape[:2]
    return {
        "drive_file_id": drive_file_id,
        "width": int(w),
        "height": int(h),
        "faces": faces,
        "face_count": len(faces),
        "decode_ms": round(decode_ms, 2),
        "detect_ms": round(detect_ms, 2),
    }


def handler(job: dict) -> dict:
    try:
        return _handle_job(job)
    except Exception as exc:  # noqa: BLE001
        logger.exception("face_handler_failed")
        return {"ok": False, "error": str(exc)[:500]}


def _handle_job(job: dict) -> dict:
    inp = job.get("input") or {}
    _ensure_runtime()
    if inp.get("healthcheck"):
        return {
            "ok": True,
            "model": MODEL_NAME,
            "providers": _providers,
            "gpu_workers": 1,
            "ffmpeg_hwaccels": _ffmpeg_hwaccels(),
            "runtime": {**_runtime, "vram_now": _nvidia_smi()},
        }

    min_confidence = float(inp.get("min_detection_confidence") or MIN_CONFIDENCE)
    t0 = time.perf_counter()
    if inp.get("video_url") or inp.get("video_b64") or inp.get("timestamps"):
        result = _process_video(inp, min_confidence)
        result["batch_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
        result["model"] = MODEL_NAME
        result["providers"] = _providers
        result["gpu_workers"] = 1
        result["vram"] = _nvidia_smi()
        return result

    items = inp.get("images")
    if not items and inp.get("image_b64"):
        items = [{"drive_file_id": inp.get("drive_file_id") or "", "image_b64": inp["image_b64"]}]
    if not items:
        return {"error": "images[] / image_b64 or video_url+timestamps is required"}

    results = [_detect_item(item, min_confidence) for item in items]
    batch_ms = (time.perf_counter() - t0) * 1000.0
    return {
        "model": MODEL_NAME,
        "providers": _providers,
        "gpu_workers": 1,
        "vram": _nvidia_smi(),
        "batch_size": len(results),
        "batch_ms": round(batch_ms, 2),
        "images": results,
    }


if __name__ == "__main__":
    import runpod

    runpod.serverless.start({"handler": handler})
