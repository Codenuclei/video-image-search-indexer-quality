"""YOLOE-26 prompt-free open-vocabulary detection (supersedes YOLO-World).

Prompt-free checkpoints (`*-seg-pf.pt`) answer from a built-in 4,585-name
vocabulary. No class list is required — that is the mode for cataloging a
library whose objects you do not already know.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from app.objects.taxonomy import canonicalize_object, taxon_for

logger = logging.getLogger(__name__)

YOLOE_MODEL_VERSION = os.environ.get("YOLOE_MODEL", "yoloe-26s-seg-pf.pt")
YOLOE_CONF = float(os.environ.get("YOLOE_CONF", "0.2"))
YOLOE_IMGSZ = int(os.environ.get("YOLOE_IMGSZ", "640"))


@dataclass(frozen=True)
class DetectedObject:
    label: str
    canonical_label: str
    category: str
    confidence: float
    bbox_x: float
    bbox_y: float
    bbox_width: float
    bbox_height: float


def _device() -> str:
    env = os.environ.get("YOLOE_DEVICE", "").strip()
    if env:
        return env
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
    except Exception:  # noqa: BLE001
        pass
    return "cpu"


@lru_cache(maxsize=1)
def _model():
    from ultralytics import YOLOE

    name = YOLOE_MODEL_VERSION
    logger.info("Loading YOLOE %s on %s", name, _device())
    return YOLOE(name)


def _label_fields(raw: str) -> tuple[str, str, str]:
    raw_l = (raw or "object").strip() or "object"
    canonical = canonicalize_object(raw_l) or raw_l.casefold()
    taxon = taxon_for(canonical)
    category = taxon.category if taxon is not None else "open_vocab"
    return raw_l[:96], canonical[:96], category[:48]


def detect_objects_bgr(
    image_bgr: np.ndarray,
    *,
    conf: float | None = None,
    imgsz: int | None = None,
) -> list[DetectedObject]:
    """Detect all named objects YOLOE's 4,585-class vocab can fire on."""
    if image_bgr is None or getattr(image_bgr, "size", 0) == 0:
        return []
    model = _model()
    results = model.predict(
        source=image_bgr,
        conf=YOLOE_CONF if conf is None else conf,
        imgsz=YOLOE_IMGSZ if imgsz is None else imgsz,
        verbose=False,
        device=_device(),
    )
    out: list[DetectedObject] = []
    for result in results:
        boxes = getattr(result, "boxes", None)
        if boxes is None:
            continue
        names = getattr(result, "names", None) or {}
        xyxy = boxes.xyxy.cpu().numpy()
        confs = boxes.conf.cpu().numpy()
        clss = boxes.cls.cpu().numpy()
        for i in range(len(xyxy)):
            x1, y1, x2, y2 = (float(v) for v in xyxy[i])
            cls_id = int(clss[i])
            raw = str(names.get(cls_id, cls_id))
            label, canonical, category = _label_fields(raw)
            out.append(
                DetectedObject(
                    label=label,
                    canonical_label=canonical,
                    category=category,
                    confidence=float(confs[i]),
                    bbox_x=x1,
                    bbox_y=y1,
                    bbox_width=max(0.0, x2 - x1),
                    bbox_height=max(0.0, y2 - y1),
                )
            )
    return out
