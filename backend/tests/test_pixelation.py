"""Unit tests for lightweight pixelation / blockiness detector."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from app.video.pixelation import (
    PIXELATION_SCORE_THRESHOLD,
    is_pixelated,
    is_pixelated_bytes,
    pixelation_score,
)


def _make_photo_like(h: int = 240, w: int = 320, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = cv2.GaussianBlur(rng.integers(40, 200, (h, w), dtype=np.uint8), (15, 15), 0)
    for _ in range(8):
        cv2.line(
            img,
            (int(rng.integers(0, w)), int(rng.integers(0, h))),
            (int(rng.integers(0, w)), int(rng.integers(0, h))),
            int(rng.integers(20, 240)),
            thickness=int(rng.integers(2, 6)),
        )
    return cv2.GaussianBlur(img, (5, 5), 0)


def _make_faceish(h: int = 240, w: int = 320, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = np.clip(
        np.full((h, w), 180, np.int16) + rng.integers(-5, 5, (h, w)),
        0,
        255,
    ).astype(np.uint8)
    cv2.ellipse(img, (w // 2, h // 2), (70, 90), 0, 0, 360, 140, -1)
    cv2.circle(img, (w // 2 - 25, h // 2 - 20), 10, 40, -1)
    cv2.circle(img, (w // 2 + 25, h // 2 - 20), 10, 40, -1)
    cv2.ellipse(img, (w // 2, h // 2 + 30), (25, 12), 0, 0, 180, 80, 2)
    return cv2.GaussianBlur(img, (3, 3), 0)


def _pixelate(gray: np.ndarray, factor: int) -> np.ndarray:
    h, w = gray.shape
    small = cv2.resize(
        gray,
        (max(1, w // factor), max(1, h // factor)),
        interpolation=cv2.INTER_AREA,
    )
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)


def _to_bgr(gray: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def _jpeg_bytes(gray: np.ndarray, quality: int = 85) -> bytes:
    ok, buf = cv2.imencode(
        ".jpg", gray, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
    )
    assert ok
    return buf.tobytes()


@pytest.mark.parametrize("factor", [4, 6, 8, 12, 16])
def test_rejects_synthetic_pixelation(factor: int):
    clean = _make_photo_like(seed=7)
    pix = _pixelate(clean, factor)
    assert is_pixelated(_to_bgr(pix)) is True
    assert pixelation_score(_to_bgr(pix)) >= PIXELATION_SCORE_THRESHOLD


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_keeps_clean_photos(seed: int):
    img = _to_bgr(_make_photo_like(seed=seed))
    assert is_pixelated(img) is False


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_keeps_face_like_frames(seed: int):
    img = _to_bgr(_make_faceish(seed=seed))
    assert is_pixelated(img) is False


def test_rejects_pixelated_face():
    pix = _pixelate(_make_faceish(seed=0), 8)
    assert is_pixelated(_to_bgr(pix)) is True


@pytest.mark.parametrize("quality", [10, 25, 40])
def test_keeps_moderate_jpeg_compression(quality: int):
    """JPEG blockiness alone must not trip the pixelation reject gate."""
    gray = _make_photo_like(seed=3)
    ok, buf = cv2.imencode(
        ".jpg", gray, [int(cv2.IMWRITE_JPEG_QUALITY), quality]
    )
    assert ok
    decoded = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
    assert decoded is not None
    assert is_pixelated(_to_bgr(decoded)) is False


def test_is_pixelated_bytes_roundtrip():
    pix = _pixelate(_make_photo_like(seed=11), 8)
    flag, details = is_pixelated_bytes(_jpeg_bytes(pix))
    assert flag is True
    assert details["pixelated"] is True
    assert details["score"] >= PIXELATION_SCORE_THRESHOLD

    clean_flag, clean_details = is_pixelated_bytes(
        _jpeg_bytes(_make_photo_like(seed=11))
    )
    assert clean_flag is False
    assert clean_details["pixelated"] is False


def test_score_orders_pixelated_above_clean():
    clean = _to_bgr(_make_photo_like(seed=5))
    pix = _to_bgr(_pixelate(_make_photo_like(seed=5), 8))
    assert pixelation_score(pix) > pixelation_score(clean) + 0.15


def test_missing_bytes_not_pixelated():
    flag, details = is_pixelated_bytes(None)
    assert flag is False
    assert details["skipped"] == "missing"


def _censorship_patch(h: int = 280, w: int = 360, seed: int = 2) -> np.ndarray:
    """Clean photo with an intentional block-mosaic rectangle (face/text censor)."""
    base = _make_photo_like(h=h, w=w, seed=seed)
    y0, x0, ph, pw = 70, 90, 96, 120
    patch = _pixelate(base[y0 : y0 + ph, x0 : x0 + pw], 10)
    out = base.copy()
    out[y0 : y0 + ph, x0 : x0 + pw] = patch
    return out


def test_rejects_localized_censorship_mosaic():
    """Intentional block censorship over a region must be screened out."""
    img = _to_bgr(_censorship_patch())
    assert is_pixelated(img) is True


def test_macroblock_score_higher_on_pixelated():
    from app.video.pixelation import pixelation_details

    clean = pixelation_details(_to_bgr(_make_photo_like(seed=4)))
    pix = pixelation_details(_to_bgr(_pixelate(_make_photo_like(seed=4), 10)))
    assert pix["macroblock_score"] >= clean["macroblock_score"]
    assert pix["pixelated"] if "pixelated" in pix else True
    assert is_pixelated(_to_bgr(_pixelate(_make_photo_like(seed=4), 10))) is True
