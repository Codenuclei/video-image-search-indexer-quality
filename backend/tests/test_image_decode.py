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


def test_open_image_rgb_applies_exif_orientation():
    source = Image.new("RGB", (40, 20), color=(120, 40, 200))
    exif = Image.Exif()
    exif[274] = 6  # Rotate encoded landscape pixels 90° clockwise for display.
    buf = io.BytesIO()
    source.save(buf, format="JPEG", exif=exif)

    rgb = open_image_rgb(buf.getvalue(), file_name="portrait.jpg")

    assert rgb.size == (20, 40)
    assert rgb.getexif().get(274) is None


def test_looks_like_svg_from_name_and_bytes():
    from app.pipelines.common import looks_like_svg

    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"></svg>'
    assert looks_like_svg(svg, file_name="poster.svg")
    assert looks_like_svg(svg, file_name="x.bin")
    assert not looks_like_svg(_png_bytes(), file_name="photo.png")
    from app.pipelines.common import svg_bytes_complete

    assert svg_bytes_complete(svg)
    assert not svg_bytes_complete(b'<svg xmlns="http://www.w3.org/2000/svg"><g')
    nested_but_truncated = (
        b'<svg xmlns="http://www.w3.org/2000/svg"><g>'
        + b'<svg width="1" height="1"></svg>'
        + b'<g id="unclosed"'
    )
    assert not svg_bytes_complete(nested_but_truncated)
    assert svg_bytes_complete(svg + b"\n  ")


def test_decode_svg_when_rsvg_available():
    import shutil

    from app.pipelines.common import decode_image_bgr

    if shutil.which("rsvg-convert") is None:
        pytest.skip("rsvg-convert not installed")
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16">'
        b'<rect width="16" height="16" fill="#ff0000"/></svg>'
    )
    bgr = decode_image_bgr(svg, file_name="poster.svg")
    assert bgr.ndim == 3 and bgr.shape[2] == 3


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
        ("image/svg+xml", "poster.svg", True),
        ("application/octet-stream", "poster.SVG", True),
    ],
)
def test_needs_jpeg_normalization(mime: str, name: str, expected: bool):
    assert needs_jpeg_normalization(mime, name) is expected
