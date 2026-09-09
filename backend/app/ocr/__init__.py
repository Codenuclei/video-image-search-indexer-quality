"""Local OCR helpers. Optional — never imported by production /search path."""
from app.ocr.rapidocr_engine import (
    OCR_MODEL_VERSION,
    BBox,
    OcrBox,
    box_center_in,
    box_iou,
    classify_ocr_region,
    normalize_ocr_text,
    run_rapidocr_bgr,
    torso_from_face,
)

__all__ = [
    "OCR_MODEL_VERSION",
    "BBox",
    "OcrBox",
    "box_center_in",
    "box_iou",
    "classify_ocr_region",
    "normalize_ocr_text",
    "run_rapidocr_bgr",
    "torso_from_face",
]
