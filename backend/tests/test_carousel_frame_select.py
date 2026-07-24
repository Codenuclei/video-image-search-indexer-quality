"""Unit tests for Instagram carousel frame candidate sampling + fallback."""

from app.search.carousel_frame_select import (
    build_frame_candidates,
    heuristic_frame_ts,
    pick_ready_from_ranked,
    sample_candidate_timestamps,
)


def test_heuristic_frame_ts_mid_span():
    assert heuristic_frame_ts(10.0, 20.0) == 15.0
    assert heuristic_frame_ts(5.0, None) == 5.0
    assert heuristic_frame_ts(8.0, 8.0) == 8.0


def test_sample_candidate_timestamps_includes_heuristic_and_caps():
    stamps = sample_candidate_timestamps(10.0, 14.0, max_candidates=5)
    assert 12.0 in stamps  # mid-span heuristic
    assert stamps[0] == 10.0
    assert stamps[-1] == 14.0
    assert len(stamps) <= 5
    assert stamps == sorted(stamps)
    # Unique
    assert len(stamps) == len(set(stamps))


def test_sample_candidate_timestamps_zero_duration():
    assert sample_candidate_timestamps(3.5, 3.5) == [3.5]
    assert sample_candidate_timestamps(3.5, None) == [3.5]


def test_sample_long_span_capped_at_eight():
    stamps = sample_candidate_timestamps(0.0, 30.0, max_candidates=8, step_sec=0.5)
    assert len(stamps) <= 8
    assert 0.0 in stamps
    assert 30.0 in stamps
    assert 15.0 in stamps


def test_build_frame_candidates_labels_heuristic():
    cands = build_frame_candidates("abc123", 10.0, 20.0, max_candidates=6)
    assert cands
    heuristics = [c for c in cands if c.label == "heuristic"]
    assert len(heuristics) == 1
    assert heuristics[0].timestamp_sec == 15.0
    assert all(c.preview_url and "frame?ts=" in c.preview_url for c in cands)
    assert [c.index for c in cands] == list(range(len(cands)))


def test_pick_ready_from_ranked_prefers_first_ready():
    # order best→worst: 2, 0, 1 — only 0 and 1 ready → pick 0
    idx, source, ready = pick_ready_from_ranked(
        order=[2, 0, 1],
        ready=[True, True, False],
        n=3,
        heuristic_index=1,
    )
    assert idx == 0
    assert source == "ai"
    assert ready is True


def test_pick_ready_from_ranked_fallback_when_none_ready():
    idx, source, ready = pick_ready_from_ranked(
        order=[2, 0, 1],
        ready=[False, False, False],
        n=3,
        heuristic_index=1,
    )
    assert idx == 2
    assert source == "fallback"
    assert ready is False


def test_pick_ready_from_ranked_no_flags_uses_top():
    idx, source, ready = pick_ready_from_ranked(
        order=[2, 1, 0],
        ready=None,
        n=3,
        heuristic_index=0,
    )
    assert idx == 2
    assert source == "ai"
    assert ready is True


def test_pick_ready_from_ranked_empty_order_uses_heuristic():
    idx, source, ready = pick_ready_from_ranked(
        order=[],
        ready=None,
        n=4,
        heuristic_index=2,
    )
    assert idx == 2
    assert source == "heuristic"
    assert ready is False


def test_pick_ready_fills_missing_indices_after_order():
    # Partial order still walks remaining indices for readiness.
    idx, source, ready = pick_ready_from_ranked(
        order=[1],
        ready=[True, False, True],
        n=3,
        heuristic_index=0,
    )
    # 1 not ready → next in filled order is 0 (ready)
    assert idx == 0
    assert source == "ai"
    assert ready is True
