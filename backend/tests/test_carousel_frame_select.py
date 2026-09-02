"""Unit tests for Instagram carousel frame candidate sampling + fallback."""

import pytest

from app.search.carousel_frame_select import (
    FrameCandidate,
    _parse_grouped_rank_response,
    build_frame_candidates,
    choose_adjacent_diverse_candidate,
    front_face_score,
    focal_point_for_slide,
    gemini_rank_batch_limit,
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


def test_gemini_rank_batch_limit_scales_and_caps():
    assert gemini_rank_batch_limit(0, 5) == 0
    assert gemini_rank_batch_limit(5, 5) == 1
    assert gemini_rank_batch_limit(20, 5) == 4
    assert gemini_rank_batch_limit(50, 5) == 8
    assert gemini_rank_batch_limit(50, 5, max_batches=3) == 3
    assert gemini_rank_batch_limit(20, 5, max_batches=0) == 0
    # 24-slide deck at 6 per request needs 4 batches, not the old cap of 3.
    assert gemini_rank_batch_limit(24, 6) == 4


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


def test_adjacent_duplicate_swaps_to_best_quality_safe_alternate():
    candidates = [
        FrameCandidate(
            0,
            1.0,
            "sample",
            quality_score=100.0,
            front_face=0.4,
            perceptual_hash="0" * 64,
        ),
        FrameCandidate(
            1,
            2.0,
            "sample",
            quality_score=90.0,
            front_face=0.36,
            perceptual_hash="1" * 64,
        ),
    ]
    choice, swapped = choose_adjacent_diverse_candidate(
        candidates,
        [0, 1],
        0,
        "0" * 64,
    )
    assert (choice, swapped) == (1, True)


def test_adjacent_duplicate_keeps_best_when_alternate_harms_quality():
    candidates = [
        FrameCandidate(
            0,
            1.0,
            "sample",
            quality_score=100.0,
            front_face=0.4,
            perceptual_hash="0" * 64,
        ),
        FrameCandidate(
            1,
            2.0,
            "sample",
            quality_score=40.0,
            front_face=0.1,
            perceptual_hash="1" * 64,
        ),
    ]
    choice, swapped = choose_adjacent_diverse_candidate(
        candidates,
        [0, 1],
        0,
        "0" * 64,
    )
    assert (choice, swapped) == (0, False)


def test_build_cache_first_candidates_prefers_disk_frames(tmp_path):
    from app.search.carousel_frame_select import build_cache_first_candidates

    fid = "vid123"
    frames = tmp_path / "video" / fid
    frames.mkdir(parents=True)
    for ts in (10.0, 11.0, 12.0, 13.0):
        (frames / f"{ts:.3f}.jpg").write_bytes(b"x" * 64)

    cands = build_cache_first_candidates(
        fid, 10.0, 14.0, thumbnail_dir=str(tmp_path), max_candidates=4
    )
    assert cands
    assert any(c.label == "heuristic" for c in cands)
    # Mid-span heuristic is 12.0; disk frames in span should dominate.
    stamps = {c.timestamp_sec for c in cands}
    assert stamps & {10.0, 11.0, 12.0, 13.0}


def test_list_cached_timestamps_in_span(tmp_path):
    from app.search.carousel_frame_select import list_cached_timestamps_in_span

    fid = "vid456"
    frames = tmp_path / "video" / fid
    frames.mkdir(parents=True)
    (frames / "5.000.jpg").write_bytes(b"a")
    (frames / "6.500.jpg").write_bytes(b"b")
    (frames / "20.000.jpg").write_bytes(b"c")
    found = list_cached_timestamps_in_span(str(tmp_path), fid, 5.0, 7.0)
    assert 5.0 in found
    assert 6.5 in found
    assert 20.0 not in found


def test_cached_frame_index_reuses_one_directory_scan(tmp_path, monkeypatch):
    from pathlib import Path

    from app.search.carousel_frame_select import (
        build_cache_first_candidates,
        index_cached_video_frames,
        load_cached_frame_bytes,
        nearest_cached_frame,
    )

    fid = "single-scan"
    frames = tmp_path / "video" / fid
    frames.mkdir(parents=True)
    for ts in (1.0, 2.0, 3.0, 4.0):
        (frames / f"{ts:.3f}.jpg").write_bytes(b"jpeg")

    original_glob = Path.glob
    scans = 0

    def counting_glob(path, pattern):
        nonlocal scans
        scans += 1
        return original_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", counting_glob)
    frame_index = index_cached_video_frames(str(tmp_path), {fid})[fid]

    for start in (1.0, 2.0, 3.0):
        candidates = build_cache_first_candidates(
            fid,
            start,
            start + 1.0,
            thumbnail_dir=str(tmp_path),
            max_candidates=4,
            cached_frames=frame_index,
        )
        assert candidates
        assert load_cached_frame_bytes(
            str(tmp_path),
            fid,
            candidates[0].timestamp_sec,
            cached_frames=frame_index,
        )
        assert nearest_cached_frame(
            str(tmp_path), fid, start, cached_frames=frame_index
        )

    assert scans == 1


@pytest.mark.asyncio
async def test_polish_returns_structured_candidate_items(tmp_path, monkeypatch):
    from app.search.carousel_frame_select import polish_slides_instagram_frames

    fid = "cand-vid"
    frames = tmp_path / "video" / fid
    frames.mkdir(parents=True)
    for ts in (1.0, 1.5, 2.0, 2.5):
        (frames / f"{ts:.3f}.jpg").write_bytes(b"jpeg-bytes")

    monkeypatch.setattr(
        "app.search.carousel_frame_select.filter_frame_candidates_by_quality",
        lambda images, **_k: (list(range(len(images))), {"rejected": {}}),
    )
    monkeypatch.setattr(
        "app.search.carousel_frame_select.score_frame_quality",
        lambda _img, **_kwargs: {"score": 50.0, "phash": "0" * 64},
    )
    monkeypatch.setattr(
        "app.llm.carousel_llm.vision_ready",
        lambda *_a, **_k: True,
    )

    slides = [
        {
            "drive_file_id": fid,
            "timestamp_sec": 1.0,
            "end_timestamp_sec": 3.0,
            "transcript_text": "Hello world",
            "hook_line": "Hello world",
        }
    ]
    out = await polish_slides_instagram_frames(
        slides,
        thumbnail_dir=str(tmp_path),
        api_key="unused",
        model="unused",
        prefer_local=True,
        max_rank_batches=0,
        ensure_frame=None,
    )
    assert len(out) == 1
    items = out[0]["frame_candidate_items"]
    assert isinstance(items, list) and items
    assert all("preview_url" in item and "ar=4x5" in item["preview_url"] for item in items)
    assert any(item.get("selected") for item in items)
    assert out[0]["frame_quality"]["rank_source"] == "local"
    assert "ar=4x5" in (out[0]["preview_url"] or "")


@pytest.mark.asyncio
async def test_polish_preserves_manual_frame(tmp_path, monkeypatch):
    from app.search.carousel_frame_select import polish_slides_instagram_frames

    monkeypatch.setattr(
        "app.llm.carousel_llm.vision_ready",
        lambda *_a, **_k: True,
    )
    slides = [
        {
            "drive_file_id": "manual-vid",
            "timestamp_sec": 1.0,
            "end_timestamp_sec": 3.0,
            "transcript_text": "Hello",
            "frame_ts": 1.75,
            "preview_url": "/media/video/manual-vid/frame?ts=1.750&cache_only=1&ar=4x5",
            "frame_source": "manual",
            "frame_candidate_items": [
                {
                    "frame_ts": 1.75,
                    "preview_url": "/media/video/manual-vid/frame?ts=1.750&cache_only=1&ar=4x5",
                    "label": "manual",
                    "order": 0,
                    "selected": True,
                }
            ],
        }
    ]
    out = await polish_slides_instagram_frames(
        slides,
        thumbnail_dir=str(tmp_path),
        api_key="unused",
        model="unused",
        prefer_local=True,
        max_rank_batches=2,
    )
    assert out[0]["frame_source"] == "manual"
    assert out[0]["frame_ts"] == 1.75
    assert out[0]["frame_quality"]["rank_source"] == "manual"



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


def test_fast_quality_path_skips_expensive_pixelation(monkeypatch):
    import numpy as np

    from app.search.carousel_frame_select import score_frame_quality

    monkeypatch.setattr(
        "app.video.pixelation.evaluate_pixelation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("pixelation analysis must be skipped")
        ),
    )
    image = np.tile(np.arange(0, 256, dtype=np.uint8), (160, 1))

    result = score_frame_quality(
        _jpeg_from_gray(image),
        check_pixelation=False,
    )

    assert not str(result.get("reject") or "").startswith("error:")


def test_quality_filter_scores_each_image_once_and_exposes_scores(monkeypatch):
    from app.search import carousel_frame_select as frame_select

    calls = 0

    def fake_score(_image):
        nonlocal calls
        calls += 1
        return {
            "reject": None,
            "score": float(calls),
            "phash": f"{calls:064b}",
        }

    monkeypatch.setattr(frame_select, "score_frame_quality", fake_score)
    scores: list[dict] = []
    kept, _stats = frame_select.filter_frame_candidates_by_quality(
        [b"a", b"b", b"c"],
        max_keep=3,
        min_keep=1,
        quality_scores_out=scores,
    )

    assert calls == 3
    assert len(scores) == 3
    assert kept
