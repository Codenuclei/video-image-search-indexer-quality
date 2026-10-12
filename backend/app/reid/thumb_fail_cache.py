"""Short-lived negative cache for face/video-frame thumbnail failures."""
from __future__ import annotations

from collections import OrderedDict
from time import monotonic

_MAX_ENTRIES = 500
_DEFAULT_TTL_SEC = 300.0

# face_id → monotonic expiry
_FACE_FAIL_CACHE: OrderedDict[int, float] = OrderedDict()
# (drive_file_id, round(ts, 3)) → monotonic expiry
_FRAME_FAIL_CACHE: OrderedDict[tuple[str, float], float] = OrderedDict()


def _fail_get(cache: OrderedDict, key: object) -> bool:
    """Return True if *key* is still in the fail cache (not expired)."""
    expiry = cache.get(key)
    if expiry is None:
        return False
    if monotonic() >= float(expiry):
        cache.pop(key, None)
        return False
    # Refresh LRU order
    cache.move_to_end(key)
    return True


def _fail_put(cache: OrderedDict, key: object, ttl: float = _DEFAULT_TTL_SEC) -> None:
    cache[key] = monotonic() + float(ttl)
    cache.move_to_end(key)
    while len(cache) > _MAX_ENTRIES:
        cache.popitem(last=False)


def face_fail_cached(face_id: int) -> bool:
    return _fail_get(_FACE_FAIL_CACHE, int(face_id))


def face_fail_put(face_id: int, ttl: float = _DEFAULT_TTL_SEC) -> None:
    _fail_put(_FACE_FAIL_CACHE, int(face_id), ttl=ttl)


def frame_fail_key(drive_file_id: str, ts: float) -> tuple[str, float]:
    return (str(drive_file_id), round(float(ts), 3))


def frame_fail_cached(drive_file_id: str, ts: float) -> bool:
    return _fail_get(_FRAME_FAIL_CACHE, frame_fail_key(drive_file_id, ts))


def frame_fail_put(drive_file_id: str, ts: float, ttl: float = _DEFAULT_TTL_SEC) -> None:
    _fail_put(_FRAME_FAIL_CACHE, frame_fail_key(drive_file_id, ts), ttl=ttl)


def clear_fail_caches() -> None:
    """Test helper."""
    _FACE_FAIL_CACHE.clear()
    _FRAME_FAIL_CACHE.clear()
