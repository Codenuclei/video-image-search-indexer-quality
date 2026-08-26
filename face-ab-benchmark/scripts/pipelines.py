#!/usr/bin/env python3
"""Detector + recognizer pipeline wrappers for controlled A/B comparison."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np
import onnxruntime as ort

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"

# Fair thread config (same for every pipeline).
_THREADS = int(os.environ.get("FACE_AB_THREADS", "4"))
for _k in (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
):
    os.environ.setdefault(_k, str(_THREADS))


def _ort_session(model_path: Path) -> ort.InferenceSession:
    so = ort.SessionOptions()
    so.intra_op_num_threads = _THREADS
    so.inter_op_num_threads = 1
    return ort.InferenceSession(
        str(model_path),
        sess_options=so,
        providers=["CPUExecutionProvider"],
    )


@dataclass
class FaceHit:
    bbox: tuple[float, float, float, float]  # x1,y1,x2,y2
    score: float
    landmarks: np.ndarray | None  # (5,2) if available
    embedding: np.ndarray | None = None


@dataclass
class TimedResult:
    faces: list[FaceHit]
    decode_ms: float
    detect_ms: float
    align_ms: float
    embedding_ms: float
    total_ms: float


def l2_normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v) + 1e-12)
    return (v / n).astype(np.float32)


def cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(l2_normalize(a), l2_normalize(b)))


# InsightFace 5-point template for 112x112 (ArcFace / MobileFaceNet).
_ARC_FACE_DST = np.array(
    [
        [38.2946, 51.6963],
        [73.5318, 51.5014],
        [56.0252, 71.7366],
        [41.5493, 92.3655],
        [70.7299, 92.2041],
    ],
    dtype=np.float32,
)


def align_face(image_bgr: np.ndarray, landmarks_5x2: np.ndarray, image_size: int = 112) -> np.ndarray:
    src = landmarks_5x2.astype(np.float32)
    dst = _ARC_FACE_DST.copy()
    if image_size != 112:
        dst *= image_size / 112.0
    m = cv2.estimateAffinePartial2D(src, dst, method=cv2.LMEDS)[0]
    return cv2.warpAffine(image_bgr, m, (image_size, image_size), borderValue=0.0)


class Pipeline(Protocol):
    name: str

    def process(self, image_bgr: np.ndarray, *, embed: bool = True) -> TimedResult: ...


class InsightFacePipeline:
    """Our architecture (buffalo_l) or buffalo_s variant via FaceAnalysis."""

    def __init__(self, pack: str, det_size: tuple[int, int] = (640, 640), label: str | None = None):
        from insightface.app import FaceAnalysis

        self.name = label or f"insightface_{pack}"
        self.pack = pack
        self.det_size = det_size
        root = str(Path.home() / ".insightface")
        self._app = FaceAnalysis(name=pack, root=root, providers=["CPUExecutionProvider"])
        # ctx_id=-1 forces CPU in insightface
        self._app.prepare(ctx_id=-1, det_size=det_size)

    def process(self, image_bgr: np.ndarray, *, embed: bool = True) -> TimedResult:
        t0 = time.perf_counter()
        t1 = time.perf_counter()
        faces_raw = self._app.get(image_bgr)
        t2 = time.perf_counter()
        hits: list[FaceHit] = []
        align_ms = 0.0
        emb_ms = 0.0
        # FaceAnalysis already aligns+embeds internally; attribute combined to embedding.
        for f in faces_raw:
            bbox = f.bbox.astype(float)
            kps = getattr(f, "kps", None)
            emb = None
            if embed and getattr(f, "normed_embedding", None) is not None:
                emb = np.asarray(f.normed_embedding, dtype=np.float32)
            hits.append(
                FaceHit(
                    bbox=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
                    score=float(getattr(f, "det_score", 0.0) or 0.0),
                    landmarks=np.asarray(kps, dtype=np.float32) if kps is not None else None,
                    embedding=emb,
                )
            )
        emb_ms = (t2 - t1) * 1000.0  # detect+align+embed bundled
        total = (time.perf_counter() - t0) * 1000.0
        return TimedResult(
            faces=hits,
            decode_ms=0.0,
            detect_ms=emb_ms,
            align_ms=align_ms,
            embedding_ms=0.0 if not embed else emb_ms,
            total_ms=total,
        )


class YuNetSFacePipeline:
    """OpenCV Zoo: YuNet detector + SFace recognizer."""

    def __init__(self, det_size: tuple[int, int] = (320, 320)):
        self.name = "yunet_sface"
        self.det_size = det_size
        yunet = MODELS / "face_detection_yunet_2023mar.onnx"
        sface = MODELS / "face_recognition_sface_2021dec.onnx"
        if not yunet.exists() or not sface.exists():
            raise FileNotFoundError("YuNet/SFace models missing; run download_models.py")
        self._det = cv2.FaceDetectorYN_create(str(yunet), "", det_size, 0.6, 0.3, 5000)
        self._rec = cv2.FaceRecognizerSF_create(str(sface), "")

    def process(self, image_bgr: np.ndarray, *, embed: bool = True) -> TimedResult:
        t0 = time.perf_counter()
        h, w = image_bgr.shape[:2]
        self._det.setInputSize((w, h))
        t_d0 = time.perf_counter()
        _, faces = self._det.detect(image_bgr)
        t_d1 = time.perf_counter()
        hits: list[FaceHit] = []
        align_ms = 0.0
        emb_ms = 0.0
        if faces is None:
            faces = []
        for row in faces:
            x, y, bw, bh = map(float, row[:4])
            score = float(row[-1])
            # YuNet landmarks: indices 4..13 -> 5 points
            lms = np.array(row[4:14], dtype=np.float32).reshape(5, 2)
            emb = None
            if embed:
                ta = time.perf_counter()
                face_align = self._rec.alignCrop(image_bgr, row)
                ta1 = time.perf_counter()
                feat = self._rec.feature(face_align)
                ta2 = time.perf_counter()
                align_ms += (ta1 - ta) * 1000.0
                emb_ms += (ta2 - ta1) * 1000.0
                emb = l2_normalize(np.asarray(feat, dtype=np.float32).reshape(-1))
            hits.append(
                FaceHit(
                    bbox=(x, y, x + bw, y + bh),
                    score=score,
                    landmarks=lms,
                    embedding=emb,
                )
            )
        total = (time.perf_counter() - t0) * 1000.0
        return TimedResult(
            faces=hits,
            decode_ms=0.0,
            detect_ms=(t_d1 - t_d0) * 1000.0,
            align_ms=align_ms,
            embedding_ms=emb_ms,
            total_ms=total,
        )


class ScrfdOnnxDetector:
    """SCRFD ONNX via InsightFace's official detector wrapper (landmarks + NMS)."""

    def __init__(self, model_path: Path, input_size: tuple[int, int] = (640, 640), conf: float = 0.5):
        from insightface.model_zoo import get_model

        self.model_path = model_path
        self.input_size = input_size
        self.conf = conf
        self._det = get_model(str(model_path))
        self._det.prepare(ctx_id=-1, input_size=input_size)

    def detect(self, image_bgr: np.ndarray) -> tuple[list[FaceHit], float]:
        t0 = time.perf_counter()
        bboxes, kpss = self._det.detect(image_bgr, max_num=0, metric="default")
        hits: list[FaceHit] = []
        if bboxes is None or len(bboxes) == 0:
            return [], (time.perf_counter() - t0) * 1000.0
        for i, row in enumerate(bboxes):
            score = float(row[4])
            if score < self.conf:
                continue
            lms = None
            if kpss is not None:
                lms = np.asarray(kpss[i], dtype=np.float32)
            hits.append(
                FaceHit(
                    bbox=(float(row[0]), float(row[1]), float(row[2]), float(row[3])),
                    score=score,
                    landmarks=lms,
                )
            )
        return hits, (time.perf_counter() - t0) * 1000.0


class ArcFaceOnnxRecognizer:
    def __init__(self, model_path: Path, input_size: int = 112):
        self.session = _ort_session(model_path)
        self.input_name = self.session.get_inputs()[0].name
        self.input_size = input_size

    def embed(self, face_bgr_112: np.ndarray) -> np.ndarray:
        blob = cv2.dnn.blobFromImage(
            face_bgr_112,
            1.0 / 127.5,
            (self.input_size, self.input_size),
            (127.5, 127.5, 127.5),
            swapRB=True,
        )
        out = self.session.run(None, {self.input_name: blob})[0]
        return l2_normalize(out.reshape(-1).astype(np.float32))


class SFaceRecognizer:
    def __init__(self, model_path: Path | None = None):
        path = model_path or (MODELS / "face_recognition_sface_2021dec.onnx")
        self._rec = cv2.FaceRecognizerSF_create(str(path), "")

    def embed_from_aligned(self, face_bgr: np.ndarray) -> np.ndarray:
        # SFace expects its own alignCrop usually; if already 112 aligned ArcFace-style,
        # still run feature() on the crop.
        feat = self._rec.feature(face_bgr)
        return l2_normalize(np.asarray(feat, dtype=np.float32).reshape(-1))

    def embed_with_row(self, image_bgr: np.ndarray, face_row: np.ndarray) -> tuple[np.ndarray, float, float]:
        ta = time.perf_counter()
        aligned = self._rec.alignCrop(image_bgr, face_row)
        ta1 = time.perf_counter()
        feat = self._rec.feature(aligned)
        ta2 = time.perf_counter()
        return (
            l2_normalize(np.asarray(feat, dtype=np.float32).reshape(-1)),
            (ta1 - ta) * 1000.0,
            (ta2 - ta1) * 1000.0,
        )


class HybridScrfdPipeline:
    """SCRFD ONNX detector + (SFace | MobileFaceNet/ArcFace ONNX) recognizer."""

    def __init__(
        self,
        name: str,
        det_path: Path,
        rec_kind: str,
        rec_path: Path,
        det_size: tuple[int, int] = (640, 640),
    ):
        self.name = name
        self.detector = ScrfdOnnxDetector(det_path, input_size=det_size)
        self.rec_kind = rec_kind
        if rec_kind == "sface":
            self.sface = SFaceRecognizer(rec_path)
            self.arc = None
        else:
            self.sface = None
            self.arc = ArcFaceOnnxRecognizer(rec_path)

    def process(self, image_bgr: np.ndarray, *, embed: bool = True) -> TimedResult:
        t0 = time.perf_counter()
        hits, det_ms = self.detector.detect(image_bgr)
        align_ms = 0.0
        emb_ms = 0.0
        out: list[FaceHit] = []
        for hit in hits:
            emb = None
            if embed and hit.landmarks is not None:
                if self.rec_kind == "sface":
                    assert self.sface is not None
                    # Official SFace path: FaceRecognizerSF.alignCrop on a YuNet-shaped row.
                    x1, y1, x2, y2 = hit.bbox
                    lms = hit.landmarks.reshape(-1)
                    row = np.array(
                        [
                            x1,
                            y1,
                            x2 - x1,
                            y2 - y1,
                            *lms.tolist(),
                            hit.score,
                        ],
                        dtype=np.float32,
                    )
                    emb, a_ms, e_ms = self.sface.embed_with_row(image_bgr, row)
                    align_ms += a_ms
                    emb_ms += e_ms
                else:
                    ta = time.perf_counter()
                    aligned = align_face(image_bgr, hit.landmarks, 112)
                    ta1 = time.perf_counter()
                    assert self.arc is not None
                    emb = self.arc.embed(aligned)
                    ta2 = time.perf_counter()
                    align_ms += (ta1 - ta) * 1000.0
                    emb_ms += (ta2 - ta1) * 1000.0
            out.append(
                FaceHit(
                    bbox=hit.bbox,
                    score=hit.score,
                    landmarks=hit.landmarks,
                    embedding=emb,
                )
            )
        total = (time.perf_counter() - t0) * 1000.0
        return TimedResult(
            faces=out,
            decode_ms=0.0,
            detect_ms=det_ms,
            align_ms=align_ms,
            embedding_ms=emb_ms,
            total_ms=total,
        )


def resolve_model_paths() -> dict[str, Path]:
    home = Path.home() / ".insightface" / "models"
    paths = {
        "yunet": MODELS / "face_detection_yunet_2023mar.onnx",
        "sface": MODELS / "face_recognition_sface_2021dec.onnx",
        "det_10g": home / "buffalo_l" / "det_10g.onnx",
        "w600k_r50": home / "buffalo_l" / "w600k_r50.onnx",
        "det_500m": home / "buffalo_s" / "det_500m.onnx",
        "w600k_mbf": home / "buffalo_s" / "w600k_mbf.onnx",
        "det_2.5g": MODELS / "det_2.5g.onnx",
    }
    search_roots = [
        MODELS,
        MODELS / "buffalo_s",
        MODELS / "buffalo_l",
        home / "buffalo_s",
        home / "buffalo_l",
        home / "buffalo_sc",
    ]
    for key, p in list(paths.items()):
        if p.exists():
            continue
        for root in search_roots:
            alt = root / Path(p).name
            if alt.exists():
                paths[key] = alt
                break
    return paths


def build_pipelines() -> list[Pipeline]:
    """Build all available pipelines for this Mac."""
    paths = resolve_model_paths()
    pipes: list[Pipeline] = []
    print("Resolved model paths:")
    for k, v in paths.items():
        print(f"  {k}: {v} ({'OK' if v.exists() else 'MISSING'})")

    # Baseline: our production architecture
    try:
        pipes.append(
            InsightFacePipeline(
                "buffalo_l",
                det_size=(640, 640),
                label="ours_buffalo_l_scrfd10g_arcface_r50",
            )
        )
    except Exception as exc:  # noqa: BLE001
        print(f"skip ours buffalo_l: {exc}")

    try:
        pipes.append(YuNetSFacePipeline(det_size=(320, 320)))
    except Exception as exc:  # noqa: BLE001
        print(f"skip yunet_sface: {exc}")

    if paths["det_500m"].exists() and paths["sface"].exists():
        pipes.append(
            HybridScrfdPipeline(
                "scrfd0.5gf_sface",
                paths["det_500m"],
                "sface",
                paths["sface"],
                det_size=(640, 640),
            )
        )
    if paths["det_500m"].exists() and paths["w600k_mbf"].exists():
        pipes.append(
            HybridScrfdPipeline(
                "scrfd0.5gf_mobilefacenet",
                paths["det_500m"],
                "mbf",
                paths["w600k_mbf"],
                det_size=(640, 640),
            )
        )
    if paths["det_2.5g"].exists() and paths["w600k_mbf"].exists():
        pipes.append(
            HybridScrfdPipeline(
                "scrfd2.5gf_mobilefacenet",
                paths["det_2.5g"],
                "mbf",
                paths["w600k_mbf"],
                det_size=(640, 640),
            )
        )
    else:
        print("NOTE: SCRFD-2.5GF (det_2.5g.onnx) not present — that row will be skipped.")
        # Closest available bracket: SCRFD-10G detector + MobileFaceNet recognizer
        if paths["det_10g"].exists() and paths["w600k_mbf"].exists():
            pipes.append(
                HybridScrfdPipeline(
                    "scrfd10g_mobilefacenet_proxy_for_2.5gf",
                    paths["det_10g"],
                    "mbf",
                    paths["w600k_mbf"],
                    det_size=(640, 640),
                )
            )

    # Packaged FaceAnalysis buffalo_s reference
    try:
        if (Path.home() / ".insightface" / "models" / "buffalo_s" / "det_500m.onnx").exists():
            pipes.append(
                InsightFacePipeline(
                    "buffalo_s",
                    det_size=(640, 640),
                    label="insightface_buffalo_s_scrfd0.5_mbf",
                )
            )
    except Exception as exc:  # noqa: BLE001
        print(f"skip buffalo_s FaceAnalysis: {exc}")

    return pipes
