import io

import numpy as np
import pytest
from PIL import Image

from app.pipelines.common import (
    bytes_to_jpeg_bytes,
    decode_image_bgr,
    needs_jpeg_normalization,
    open_image_rgb,
    register_image_plugins,
)


def _png_bytes() -> bytes:
    img = Image.new("RGB", (8, 8), color=(120, 40, 200))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_register_image_plugins_is_idempotent():
    register_image_plugins()
    register_image_plugins()


def test_decode_image_bgr_from_png():
    bgr = decode_image_bgr(_png_bytes())
    assert bgr.shape == (8, 8, 3)
    assert bgr.dtype == np.uint8


def test_bytes_to_jpeg_bytes_produces_jpeg():
    jpeg = bytes_to_jpeg_bytes(_png_bytes())
    assert jpeg[:2] == b"\xff\xd8"
    assert jpeg[-2:] == b"\xff\xd9"


def test_open_image_rgb_from_png():
    rgb = open_image_rgb(_png_bytes())
    assert rgb.size == (8, 8)
    assert rgb.mode == "RGB"


@pytest.mark.parametrize(
    ("mime", "name", "expected"),
    [
        ("image/jpeg", "photo.jpg", False),
        ("image/png", "photo.png", False),
        ("image/heic", "iphone.heic", True),
        ("image/avif", "photo.avif", True),
        ("image/tiff", "scan.tiff", True),
        ("image/bmp", "old.bmp", True),
        ("application/octet-stream", "DSC0001.ARW", True),
        ("", "photo.HEIC", True),
        ("", "scan.TIF", True),
    ],
)
def test_needs_jpeg_normalization(mime: str, name: str, expected: bool):
    assert needs_jpeg_normalization(mime, name) is expected
