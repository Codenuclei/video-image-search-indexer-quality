"""
Person / body detection via Ultralytics YOLOv8n (COCO class 0 = person).

This replaces the old face→anthropometry guess with a real detector that
returns proved bounding boxes. Pattern follows the common YOLOv8+OpenCV
person-detection examples (ultralytics `yolov8n.pt`, `classes=[0]`).

Falls back to OpenCV HOG people detector if ultralytics is unavailable,
so the lab can still draw boxes without a GPU / torch install.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

logger = logging.getLogger(__name__)

# COCO person class id
_PERSON_CLASS = 0


@dataclass
class PersonBox:
    x: float
    y: float
    width: float
    height: float
    confidence: float
    backend: str  # "yolov8n" | "opencv_hog"


@lru_cache(maxsize=1)
def _yolo_model():
    from ultralytics import YOLO

    # Auto-downloads yolov8n.pt (~6MB) on first use.
    return YOLO("yolov8n.pt")


def yolov8_available() -> bool:
    try:
        import ultralytics  # noqa: F401

        return True
    except ImportError:
        return False


def detect_persons_bgr(
    image_bgr: np.ndarray,
    *,
    conf: float = 0.35,
) -> list[PersonBox]:
    """Detect people and return axis-aligned boxes in image pixel coords."""
    if image_bgr is None or image_bgr.size == 0:
        return []
    if yolov8_available():
        try:
            return _detect_yolo(image_bgr, conf=conf)
        except Exception as exc:  # noqa: BLE001
            logger.warning("YOLOv8 person detect failed, falling back to HOG: %s", exc)
    return _detect_hog(image_bgr)


def _detect_yolo(image_bgr: np.ndarray, *, conf: float) -> list[PersonBox]:
    model = _yolo_model()
    results = model.predict(
        source=image_bgr,
        classes=[_PERSON_CLASS],
        conf=conf,
        verbose=False,
        imgsz=640,
    )
    out: list[PersonBox] = []
    for r in results:
        if r.boxes is None:
            continue
        for box in r.boxes:
            xyxy = box.xyxy[0].tolist()
            x1, y1, x2, y2 = float(xyxy[0]), float(xyxy[1]), float(xyxy[2]), float(xyxy[3])
            out.append(
                PersonBox(
                    x=x1,
                    y=y1,
                    width=max(0.0, x2 - x1),
                    height=max(0.0, y2 - y1),
                    confidence=float(box.conf[0]),
                    backend="yolov8n",
                )
            )
    return out


def _detect_hog(image_bgr: np.ndarray) -> list[PersonBox]:
    """OpenCV HOG default people detector — slower / less accurate, no extra deps."""
    import cv2

    hog = cv2.HOGDescriptor()
    hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
    # HOG prefers larger people; downscale huge images for speed.
    h, w = image_bgr.shape[:2]
    scale = 1.0
    work = image_bgr
    if max(h, w) > 1280:
        scale = 1280 / float(max(h, w))
        work = cv2.resize(image_bgr, (int(w * scale), int(h * scale)))
    rects, weights = hog.detectMultiScale(work, winStride=(8, 8), padding=(8, 8), scale=1.05)
    out: list[PersonBox] = []
    inv = 1.0 / scale
    for (x, y, bw, bh), weight in zip(rects, weights):
        out.append(
            PersonBox(
                x=float(x) * inv,
                y=float(y) * inv,
                width=float(bw) * inv,
                height=float(bh) * inv,
                confidence=float(weight) if np.ndim(weight) == 0 else float(weight[0]),
                backend="opencv_hog",
            )
        )
    return out


def face_center_in_box(face_x: float, face_y: float, face_w: float, face_h: float, box: PersonBox) -> bool:
    cx = face_x + face_w / 2
    cy = face_y + face_h / 2
    return box.x <= cx <= box.x + box.width and box.y <= cy <= box.y + box.height


def best_person_for_face(
    face_x: float,
    face_y: float,
    face_w: float,
    face_h: float,
    persons: list[PersonBox],
) -> PersonBox | None:
    """Pick the smallest person box whose interior contains the face center."""
    containing = [
        p
        for p in persons
        if face_center_in_box(face_x, face_y, face_w, face_h, p) and p.width * p.height > 0
    ]
    if not containing:
        return None
    return min(containing, key=lambda p: p.width * p.height)


def draw_proof(
    image_bgr: np.ndarray,
    persons: list[PersonBox],
    faces: list[tuple[float, float, float, float]] | None = None,
) -> np.ndarray:
    """Return a copy with person (green) and face (sky) bounding boxes drawn."""
    import cv2

    canvas = image_bgr.copy()
    for i, p in enumerate(persons):
        x1, y1 = int(p.x), int(p.y)
        x2, y2 = int(p.x + p.width), int(p.y + p.height)
        cv2.rectangle(canvas, (x1, y1), (x2, y2), (40, 180, 60), 3)
        label = f"person {p.confidence:.2f} [{p.backend}]"
        cv2.putText(
            canvas,
            label,
            (x1, max(20, y1 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (40, 180, 60),
            2,
            cv2.LINE_AA,
        )
    if faces:
        for fx, fy, fw, fh in faces:
            x1, y1, x2, y2 = int(fx), int(fy), int(fx + fw), int(fy + fh)
            cv2.rectangle(canvas, (x1, y1), (x2, y2), (220, 160, 40), 2)
    return canvas
