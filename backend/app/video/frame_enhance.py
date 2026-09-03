"""Natural HDR-style frame enhancement (no generated detail).

Conservative OpenCV tone mapping: local contrast, shadow/highlight recovery,
restrained color, and light sharpening. Suitable for Instagram carousel stills.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_JPEG_QUALITY = 90


def enhance_frame_natural_hdr(jpeg_bytes: bytes) -> bytes | None:
    """Return an enhanced JPEG, or ``None`` when decode/enhancement fails."""
    if not jpeg_bytes:
        return None
    try:
        import cv2
        import numpy as np

        cv2.setNumThreads(1)
        arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None or bgr.size == 0:
            return None

        # Work in float LAB for luminance-local contrast without blowing chroma.
        lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
        l_chan, a_chan, b_chan = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        l_eq = clahe.apply(l_chan)

        # Soft shadow lift / highlight tame on the equalized L channel.
        l_f = l_eq.astype(np.float32) / 255.0
        shadows = np.clip((0.42 - l_f) / 0.42, 0.0, 1.0)
        highlights = np.clip((l_f - 0.68) / 0.32, 0.0, 1.0)
        l_f = l_f + shadows * 0.08 - highlights * 0.06
        l_f = np.clip(l_f, 0.0, 1.0)
        l_out = (l_f * 255.0).astype(np.uint8)

        merged = cv2.merge((l_out, a_chan, b_chan))
        enhanced = cv2.cvtColor(merged, cv2.COLOR_LAB2BGR)

        # Mild saturation boost in HSV (restrained).
        hsv = cv2.cvtColor(enhanced, cv2.COLOR_BGR2HSV).astype(np.float32)
        hsv[:, :, 1] = np.clip(hsv[:, :, 1] * 1.08, 0, 255)
        enhanced = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)

        # Unsharp mask for crisp portraits without halos.
        blur = cv2.GaussianBlur(enhanced, (0, 0), 1.1)
        enhanced = cv2.addWeighted(enhanced, 1.22, blur, -0.22, 0)

        ok, buf = cv2.imencode(
            ".jpg",
            enhanced,
            [int(cv2.IMWRITE_JPEG_QUALITY), _JPEG_QUALITY],
        )
        if not ok:
            return None
        return buf.tobytes()
    except Exception as exc:  # noqa: BLE001
        logger.debug("natural HDR enhance failed: %s", str(exc)[:160])
        return None


def ensure_hdr_variant(
    source: Path,
    variant: Path,
    *,
    force: bool = False,
) -> Path | None:
    """Write/reuse a natural-HDR JPEG beside the source frame.

    Returns the variant path when usable, otherwise ``None`` (caller may fall
    back to the original). Invalidates when the source is newer.
    """
    try:
        if not source.is_file() or source.stat().st_size <= 0:
            return None
        if (
            not force
            and variant.is_file()
            and variant.stat().st_mtime >= source.stat().st_mtime
            and variant.stat().st_size > 0
        ):
            return variant
        data = enhance_frame_natural_hdr(source.read_bytes())
        if not data:
            return None
        variant.parent.mkdir(parents=True, exist_ok=True)
        tmp = variant.with_suffix(".partial.jpg")
        tmp.write_bytes(data)
        tmp.replace(variant)
        return variant
    except Exception as exc:  # noqa: BLE001
        logger.debug("ensure_hdr_variant failed %s: %s", source, str(exc)[:160])
        return None


def hdr_variant_path(thumbnail_dir: str, drive_file_id: str, ts: float) -> Path:
    return (
        Path(thumbnail_dir)
        / "video"
        / drive_file_id
        / "hdr"
        / f"{float(ts):.3f}.jpg"
    )


def ensure_hdr_for_timestamp(
    thumbnail_dir: str,
    drive_file_id: str,
    ts: float,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Materialize the HDR derivative for an on-disk source frame."""
    from app.search.carousel_frame_select import cached_frame_path

    source = cached_frame_path(thumbnail_dir, drive_file_id, ts)
    variant = hdr_variant_path(thumbnail_dir, drive_file_id, ts)
    path = ensure_hdr_variant(source, variant, force=force)
    return {
        "ok": path is not None,
        "source": str(source),
        "variant": str(variant) if path is not None else None,
        "frame_ts": round(float(ts), 3),
    }
