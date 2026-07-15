from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

import numpy as np

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DetectedFace:
    bbox_x: float
    bbox_y: float
    bbox_width: float
    bbox_height: float
    confidence: float
    embedding: list[float]
    thumbnail_jpeg: bytes


class FaceEngine:
    _lock = threading.Lock()

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._app = None

    def _ensure_loaded(self) -> None:
        if self._app is not None:
            return
        with self._lock:
            if self._app is not None:
                return
            from insightface.app import FaceAnalysis

            logger.info("Loading InsightFace model %s", self._settings.insightface_model_name)
            app = FaceAnalysis(name=self._settings.insightface_model_name, providers=self._settings.insightface_providers)
            app.prepare(ctx_id=0, det_size=self._settings.face_detection_size)
            self._app = app

    def detect_faces(self, image_bgr: np.ndarray) -> list[DetectedFace]:
        self._ensure_loaded()
        assert self._app is not None

        with self._lock:
            faces = self._app.get(image_bgr)
        results: list[DetectedFace] = []
        for face in faces:
            confidence = float(getattr(face, "det_score", 0.0) or 0.0)
            if confidence < self._settings.min_detection_confidence:
                continue
            bbox = face.bbox.astype(float)
            x1, y1, x2, y2 = bbox
            embedding = face.normed_embedding
            if embedding is None:
                continue
            thumb = b""
            try:
                import cv2

                h, w = image_bgr.shape[:2]
                ix1, iy1 = max(0, int(x1)), max(0, int(y1))
                ix2, iy2 = min(w, int(x2)), min(h, int(y2))
                crop = image_bgr[iy1:iy2, ix1:ix2]
                if crop.size > 0:
                    ok, buf = cv2.imencode(".jpg", crop)
                    if ok:
                        thumb = buf.tobytes()
            except Exception:  # noqa: BLE001
                pass

            results.append(
                DetectedFace(
                    bbox_x=float(x1),
                    bbox_y=float(y1),
                    bbox_width=float(x2 - x1),
                    bbox_height=float(y2 - y1),
                    confidence=confidence,
                    embedding=embedding.astype(float).tolist(),
                    thumbnail_jpeg=thumb,
                )
            )
        return results


_engine: FaceEngine | None = None


def get_face_engine() -> FaceEngine:
    global _engine
    if _engine is None:
        _engine = FaceEngine()
    return _engine
