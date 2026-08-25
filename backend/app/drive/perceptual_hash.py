"""Fast CPU dHash for cross-resolution visual duplicate detection."""
from __future__ import annotations

import cv2
import numpy as np

# 9×8 gray → 64 horizontal gradient bits (standard difference hash).
_DHASH_SIZE = (9, 8)


def dhash_int_from_gray(gray: np.ndarray) -> int:
    """64-bit difference hash from a single-channel uint8 image."""
    resized = cv2.resize(gray, _DHASH_SIZE, interpolation=cv2.INTER_AREA)
    diff = resized[:, 1:] > resized[:, :-1]
    bits = 0
    for i, v in enumerate(diff.flatten()):
        if v:
            bits |= 1 << i
    return bits


def dhash_int_from_bgr(bgr: np.ndarray) -> int:
    if bgr.ndim == 2:
        gray = bgr
    else:
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return dhash_int_from_gray(gray)


def dhash_hex_from_bgr(bgr: np.ndarray) -> str:
    return f"{dhash_int_from_bgr(bgr):016x}"


def hamming_hex(a: str | None, b: str | None) -> int:
    """Hamming distance between two 16-char hex dHash strings."""
    if not a or not b or len(a) != 16 or len(b) != 16:
        return 64
    return (int(a, 16) ^ int(b, 16)).bit_count()
