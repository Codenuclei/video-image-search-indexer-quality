"""RapidOCR ONNX engine + torso heuristics for brand-on-garment scoring."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

OCR_MODEL_VERSION = "rapidocr-onnx-v2"

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class OcrBox:
    text: str
    confidence: float
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class BBox:
    x: float
    y: float
    w: float
    h: float

    @property
    def x2(self) -> float:
        return self.x + self.w

    @property
    def y2(self) -> float:
        return self.y + self.h


def normalize_ocr_text(text: str) -> str:
    return _WHITESPACE.sub(" ", (text or "").casefold()).strip()


def torso_from_face(face: BBox, *, image_w: float, image_h: float) -> BBox:
    """Approximate upper-torso / garment region from a face box (no YOLO)."""
    cx = face.x + face.w * 0.5
    tw = max(face.w * 2.4, face.w + 16.0)
    th = max(face.h * 2.8, face.h + 24.0)
    tx = cx - tw * 0.5
    ty = face.y + face.h * 0.85
    x = max(0.0, min(tx, max(0.0, image_w - 1.0)))
    y = max(0.0, min(ty, max(0.0, image_h - 1.0)))
    w = max(1.0, min(tw, image_w - x))
    h = max(1.0, min(th, image_h - y))
    return BBox(x=x, y=y, w=w, h=h)


def box_iou(a: BBox, b: BBox) -> float:
    ix1 = max(a.x, b.x)
    iy1 = max(a.y, b.y)
    ix2 = min(a.x2, b.x2)
    iy2 = min(a.y2, b.y2)
    iw = max(0.0, ix2 - ix1)
    ih = max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    union = a.w * a.h + b.w * b.h - inter
    return float(inter / union) if union > 0 else 0.0


def box_center_in(inner: BBox, outer: BBox) -> bool:
    cx = inner.x + inner.w * 0.5
    cy = inner.y + inner.h * 0.5
    return outer.x <= cx <= outer.x2 and outer.y <= cy <= outer.y2


def classify_ocr_region(
    span: BBox,
    *,
    torsos: list[BBox],
    image_h: float,
    image_w: float = 0.0,
) -> str:
    """torso | upper | signage | other — coarse layout for brand association."""
    for torso in torsos:
        if box_center_in(span, torso) or box_iou(span, torso) >= 0.12:
            return "torso"
        # Hats / caps sit above face; allow a band just above torso.
        hat_band = BBox(
            x=torso.x,
            y=max(0.0, torso.y - torso.h * 0.55),
            w=torso.w,
            h=max(1.0, torso.h * 0.55),
        )
        if box_center_in(span, hat_band) or box_iou(span, hat_band) >= 0.12:
            return "upper"
    cy = span.y + span.h * 0.5
    # Top strip / very wide lines are almost always backdrop / banner text.
    if cy < image_h * 0.38:
        return "signage"
    if image_w > 0 and span.w > image_w * 0.35:
        return "signage"
    if span.w > image_h * 0.40:
        return "signage"
    # With faces present but no torso overlap → treat as scene/signage.
    if torsos:
        return "signage"
    return "other"


@lru_cache(maxsize=1)
def _engine() -> Any | None:
    try:
        from rapidocr_onnxruntime import RapidOCR

        return RapidOCR()
    except Exception as exc:  # noqa: BLE001
        logger.warning("RapidOCR unavailable: %s", exc)
        return None


def run_rapidocr_bgr(image_bgr: np.ndarray) -> list[OcrBox]:
    """Run RapidOCR on a BGR image; returns axis-aligned boxes in pixel space."""
    eng = _engine()
    if eng is None or image_bgr is None or getattr(image_bgr, "size", 0) == 0:
        return []
    try:
        # RapidOCR accepts numpy RGB/BGR ndarrays.
        result, _ = eng(image_bgr)
    except Exception as exc:  # noqa: BLE001
        logger.warning("RapidOCR inference failed: %s", exc)
        return []
    if not result:
        return []
    out: list[OcrBox] = []
    for item in result:
        # Typical: [box_points, text, score]
        try:
            pts, text, score = item[0], str(item[1] or ""), float(item[2] or 0.0)
        except Exception:  # noqa: BLE001
            continue
        text = text.strip()
        if not text:
            continue
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
        x1, x2 = min(xs), max(xs)
        y1, y2 = min(ys), max(ys)
        out.append(
            OcrBox(
                text=text,
                confidence=max(0.0, min(1.0, score)),
                x=x1,
                y=y1,
                w=max(1.0, x2 - x1),
                h=max(1.0, y2 - y1),
            )
        )
    return out
