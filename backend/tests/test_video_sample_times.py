"""Unit tests for video face timestamp merge / downsample caps."""
from __future__ import annotations

from app.pipelines.video import _evenly_downsample_times, _merge_sample_times


def test_merge_sample_times_never_exceeds_max_frames() -> None:
    # Many cue starts would previously push past max_frames + len(cues).
    cues = [float(i) for i in range(0, 200, 2)]
    times = _merge_sample_times(
        duration=300.0,
        interval_sec=1.0,
        max_frames=50,
        cue_starts=cues,
    )
    assert len(times) <= 50
    assert times == sorted(times)
    assert times[0] == 0.0


def test_merge_sample_times_keeps_all_when_under_cap() -> None:
    times = _merge_sample_times(
        duration=10.0,
        interval_sec=5.0,
        max_frames=20,
        cue_starts=[1.5, 3.0],
    )
    assert len(times) <= 20
    assert 0.0 in times
    assert 1.5 in times
    assert 3.0 in times


def test_evenly_downsample_times_caps_and_spans() -> None:
    src = [float(i) for i in range(100)]
    out = _evenly_downsample_times(src, 10)
    assert len(out) <= 10
    assert out[0] == 0.0
    assert out[-1] == 99.0


def test_evenly_downsample_empty_and_passthrough() -> None:
    assert _evenly_downsample_times([], 5) == []
    assert _evenly_downsample_times([1.0, 2.0], 0) == []
    assert _evenly_downsample_times([1.0, 2.0, 3.0], 10) == [1.0, 2.0, 3.0]
