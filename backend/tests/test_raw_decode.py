from app.pipelines.common import _extract_largest_embedded_jpeg


def test_extract_largest_embedded_jpeg_picks_biggest():
    small = b"\xff\xd8" + b"x" * 100 + b"\xff\xd9"
    large = b"\xff\xd8" + b"y" * 50_000 + b"\xff\xd9"
    raw = b"HEADER" + small + b"GAP" + large + b"TAIL"
    out = _extract_largest_embedded_jpeg(raw, min_size=1000)
    assert out == large
