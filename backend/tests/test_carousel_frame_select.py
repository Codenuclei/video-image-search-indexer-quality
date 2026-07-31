"""Unit tests for Instagram carousel frame candidate sampling + fallback."""

import pytest

from app.search.carousel_frame_select import (
    FrameCandidate,
    _parse_grouped_rank_response,
    build_frame_candidates,
    front_face_score,
    focal_point_for_slide,
    heuristic_frame_ts,
    pick_ready_from_ranked,
    sample_candidate_timestamps,
)


def test_grouped_rank_response_maps_slide_orders_and_ready_flags():
    groups = [
        [FrameCandidate(0, 1.0, "sample"), FrameCandidate(1, 2.0, "heuristic")],
        [FrameCandidate(0, 4.0, "sample"), FrameCandidate(1, 5.0, "sample")],
    ]
    parsed = _parse_grouped_rank_response(
        '{"slides":[{"slide":1,"order":[1,0],"ready":[false,true]},'
        '{"slide":0,"order":[0,1],"ready":[true,false]}]}',
        groups,
    )
    assert parsed[0] == ([0, 1], [True, False])
    assert parsed[1] == ([1, 0], [False, True])


def test_front_face_score_prefers_low_yaw_confident_face():
    front = {
        "yaw": 4,
        "detection_confidence": 0.95,
        "bbox_width": 0.3,
        "bbox_height": 0.4,
    }
    profile = {
        "yaw": 70,
        "detection_confidence": 0.95,
        "bbox_width": 0.3,
        "bbox_height": 0.4,
    }
    assert front_face_score(front) > front_face_score(profile)


def test_front_face_score_rejects_edge_profile_and_focal_point_is_normalized():
    edge_profile = {
        "yaw": 75,
        "bbox_x": 0.0,
        "bbox_y": 0.1,
        "bbox_width": 0.4,
        "bbox_height": 0.5,
        "detection_confidence": 0.99,
        "frame_timestamp": 2.0,
    }
    centered_front = {
        "yaw": 2,
        "bbox_x": 0.3,
        "bbox_y": 0.2,
        "bbox_width": 0.3,
        "bbox_height": 0.4,
        "detection_confidence": 0.9,
        "frame_timestamp": 2.0,
    }
    slide = {"faces": [edge_profile, centered_front]}
    focal_x, focal_y, score = focal_point_for_slide(slide, 2.0)
    assert focal_x == pytest.approx(0.45)
    assert focal_y == pytest.approx(0.368)
    assert score == front_face_score(centered_front)


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


def _jpeg_from_gray(gray, quality: int = 90) -> bytes:
    import cv2

    ok, buf = cv2.imencode(".jpg", gray, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    assert ok
    return buf.tobytes()


def test_filter_rejects_pixelated_keeps_clean():
    """Integration: quality filter drops mosaics, keeps normal frames."""
    import cv2
    import numpy as np
    from app.search.carousel_frame_select import (
        filter_frame_candidates_by_quality,
        score_frame_quality,
    )

    rng = np.random.default_rng(0)
    # High-contrast sharp synthetic (passes Laplacian gate, fails only when mosaicked).
    clean = np.zeros((240, 320), dtype=np.uint8)
    clean[:] = 110
    for y in range(0, 240, 20):
        clean[y : y + 10, :] = 180
    for x in range(0, 320, 24):
        clean[:, x : x + 8] = np.clip(clean[:, x : x + 8].astype(np.int16) + 40, 0, 255).astype(
            np.uint8
        )
    for _ in range(12):
        cv2.circle(
            clean,
            (int(rng.integers(20, 300)), int(rng.integers(20, 220))),
            int(rng.integers(8, 28)),
            int(rng.integers(30, 230)),
            thickness=2,
        )
    clean = cv2.GaussianBlur(clean, (3, 3), 0)
    small = cv2.resize(clean, (40, 30), interpolation=cv2.INTER_AREA)
    pix = cv2.resize(small, (320, 240), interpolation=cv2.INTER_NEAREST)

    images = [_jpeg_from_gray(clean), _jpeg_from_gray(pix), _jpeg_from_gray(clean)]
    q_clean = score_frame_quality(images[0])
    q_pix = score_frame_quality(images[1])
    assert q_clean.get("reject") is None, q_clean
    assert q_pix.get("reject") == "pixelated", q_pix

    kept, stats = filter_frame_candidates_by_quality(images, max_keep=3, min_keep=1)
    assert 1 not in kept
    assert stats.get("rejected", {}).get("pixelated", 0) >= 1
    assert any(i in kept for i in (0, 2))
