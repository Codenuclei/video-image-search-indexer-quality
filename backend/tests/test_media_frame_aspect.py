"""Carousel frame serving must produce Instagram 4:5 portrait crops."""

from __future__ import annotations

from PIL import Image

from app.routers.media import _ensure_portrait_crop


def _write_jpeg(path, width: int, height: int) -> None:
    Image.new("RGB", (width, height), (40, 90, 160)).save(path, "JPEG")


def test_reel_frame_is_cropped_to_4x5(tmp_path) -> None:
    source = tmp_path / "1.000.jpg"
    variant = tmp_path / "4x5" / "1.000.jpg"
    _write_jpeg(source, 1080, 1920)

    result = _ensure_portrait_crop(source, variant)

    assert result == variant
    with Image.open(result) as im:
        width, height = im.size
    assert abs(width / height - 4 / 5) < 0.01


def test_landscape_frame_is_cropped_to_4x5(tmp_path) -> None:
    source = tmp_path / "2.000.jpg"
    variant = tmp_path / "4x5" / "2.000.jpg"
    _write_jpeg(source, 1920, 1080)

    result = _ensure_portrait_crop(source, variant)

    assert result == variant
    with Image.open(result) as im:
        width, height = im.size
    assert abs(width / height - 4 / 5) < 0.01
    assert height == 1080


def test_already_portrait_frame_is_served_as_is(tmp_path) -> None:
    source = tmp_path / "3.000.jpg"
    variant = tmp_path / "4x5" / "3.000.jpg"
    _write_jpeg(source, 1080, 1350)

    result = _ensure_portrait_crop(source, variant)

    assert result == source
    assert not variant.exists()


def test_crop_is_cached_and_reused(tmp_path) -> None:
    source = tmp_path / "4.000.jpg"
    variant = tmp_path / "4x5" / "4.000.jpg"
    _write_jpeg(source, 1080, 1920)

    first = _ensure_portrait_crop(source, variant)
    first_mtime = first.stat().st_mtime_ns
    second = _ensure_portrait_crop(source, variant)

    assert first == second == variant
    assert second.stat().st_mtime_ns == first_mtime
