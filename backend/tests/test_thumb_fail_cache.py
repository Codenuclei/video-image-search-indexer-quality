"""Unit tests for face/video-frame thumbnail negative cache helpers."""
from __future__ import annotations

from time import monotonic

from app.reid import thumb_fail_cache as cache


def setup_function() -> None:
    cache.clear_fail_caches()


def teardown_function() -> None:
    cache.clear_fail_caches()


def test_face_fail_put_and_get() -> None:
    assert cache.face_fail_cached(42) is False
    cache.face_fail_put(42, ttl=60)
    assert cache.face_fail_cached(42) is True


def test_face_fail_expires() -> None:
    cache.face_fail_put(7, ttl=60)
    cache._FACE_FAIL_CACHE[7] = monotonic() - 1
    assert cache.face_fail_cached(7) is False
    assert 7 not in cache._FACE_FAIL_CACHE


def test_frame_fail_key_rounds_timestamp() -> None:
    cache.frame_fail_put("drive-abc", 12.3456, ttl=60)
    assert cache.frame_fail_cached("drive-abc", 12.3459) is True
    assert cache.frame_fail_cached("drive-abc", 12.300) is False


def test_fail_cache_evicts_oldest() -> None:
    original_max = cache._MAX_ENTRIES
    cache._MAX_ENTRIES = 3
    try:
        for i in range(5):
            cache.face_fail_put(i, ttl=60)
        assert len(cache._FACE_FAIL_CACHE) == 3
        assert cache.face_fail_cached(0) is False
        assert cache.face_fail_cached(1) is False
        assert cache.face_fail_cached(2) is True
        assert cache.face_fail_cached(4) is True
    finally:
        cache._MAX_ENTRIES = original_max
        cache.clear_fail_caches()
