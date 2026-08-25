"""Focused tests for deterministic Instagram carousel quality finalization."""

from __future__ import annotations

import pytest

from app.routers import carousel_script
from app.routers.carousel_script import (
    TimedPick,
    _carousel_selection_hash,
    _enforce_slides_match_transcript,
    _faces_near_slide,
    _strip_slide_ranking_fields,
    repair_duplicate_slides,
)
from app.search.carousel_pipeline import (
    apply_carousel_quality_pass,
    find_duplicate_slide_pairs,
    polish_slides_instagram_copy,
)


def _slide(text: str, start: float) -> dict:
    return {
        "transcript_text": text,
        "hook_line": text,
        "caption": text,
        "snippet": text,
        "timestamp_sec": start,
        "end_timestamp_sec": start + 2.0,
        "drive_file_id": "video-1",
    }


def test_quality_pass_scores_repairs_and_preserves_grounding() -> None:
    long_source = (
        "This is the useful opening sentence. "
        "These additional transcript words make the mobile caption unnecessarily "
        "long while remaining fully grounded in the same timed source segment."
    )
    carousel = {
        "slides": [
            _slide("Why does this simple system work?", 1.0),
            _slide(long_source, 3.0),
            _slide("Why does this simple system work?", 5.0),
            _slide("So start with the smallest useful step.", 7.0),
        ]
    }

    repaired, summary = apply_carousel_quality_pass([carousel])
    result = repaired[0]
    report = result["quality_report"]

    assert report["grounding"] == "transcript_locked"
    assert "duplicate_ideas" in report["issues"]
    assert report["duplicate_pairs"] == [[0, 2]]
    assert result["slides"][1]["transcript_text"] == "This is the useful opening sentence."
    assert result["slides"][1]["transcript_text"] in long_source
    assert result["slides"][1]["timestamp_sec"] == 3.0
    assert result["slides"][1]["end_timestamp_sec"] == 5.0
    assert summary["carousel_count"] == 1
    assert summary["repair_count"] == 1


def test_quality_pass_adds_valid_highlights_without_copy_polish() -> None:
    repaired, _ = apply_carousel_quality_pass(
        [{"slides": [_slide("Build the smallest useful system first.", 1.0)]}]
    )
    slide = repaired[0]["slides"][0]
    words = slide["transcript_text"].split()

    assert slide["highlight"]
    assert slide["highlight_words"] == [words[i] for i in slide["highlight"]]
    assert slide["transcript_text"] == "Build the smallest useful system first."


def test_quality_repairs_remain_compatible_with_transcript_guard() -> None:
    source = (
        "Start with one grounded example. "
        "Then keep adding transcript detail until the caption is too long for a phone."
    )
    repaired, _ = apply_carousel_quality_pass(
        [{"slides": [_slide(source, 10.0)]}]
    )
    guard = _enforce_slides_match_transcript(
        repaired,
        [(10.0, 12.0, source)],
    )

    assert guard["snapped"] == 0
    assert guard["ok"] == 1
    assert repaired[0]["slides"][0]["transcript_verified"] is True


@pytest.mark.asyncio
async def test_copy_finalizer_rejects_invented_llm_claim(monkeypatch) -> None:
    async def invented_response(**_kwargs):
        return (
            '{"slides":[{"i":0,"text":"Revenue grew 400 percent overnight",'
            '"highlight":[1]}]}',
            "claude",
        )

    monkeypatch.setattr(
        "app.search.carousel_pipeline._llm_complete_json",
        invented_response,
    )
    source = "The team tested one small change."
    polished, _ = await polish_slides_instagram_copy(
        [_slide(source, 1.0)],
        api_key="configured",
        model="test-model",
    )

    assert polished[0]["transcript_text"] == source
    assert polished[0]["copy_source"] == "claude+transcript_locked"
    assert "Revenue" not in polished[0]["transcript_text"]


@pytest.mark.asyncio
async def test_copy_finalizer_keeps_grounded_rewrite(monkeypatch) -> None:
    async def crafted_response(**_kwargs):
        return (
            '{"slides":[{"i":0,"text":"The team tested one small change.",'
            '"highlight":[1,2]}]}',
            "claude",
        )

    monkeypatch.setattr(
        "app.search.carousel_pipeline._llm_complete_json",
        crafted_response,
    )
    source = "the team tested one small change [music] and then kept talking"
    polished, _ = await polish_slides_instagram_copy(
        [_slide(source, 1.0)],
        api_key="configured",
        model="test-model",
    )

    assert polished[0]["transcript_text"] == "The team tested one small change."
    assert polished[0]["original_text"] == source
    assert polished[0]["copy_source"] == "claude"


def test_transcript_guard_keeps_grounded_crafted_copy() -> None:
    source = "The country is extremely profitable reaching a market value of 42 billion US"
    crafted = "India's ghee market is worth $42 billion."
    carousels = [
        {
            "slides": [
                {
                    **_slide(crafted, 8.0),
                    "original_text": source,
                }
            ]
        }
    ]
    guard = _enforce_slides_match_transcript(
        carousels,
        [(8.0, 19.0, source)],
    )
    assert guard["snapped"] == 0
    assert guard["ok"] == 1
    assert carousels[0]["slides"][0]["transcript_text"] == crafted


def test_algorithm_version_and_polish_copy_change_cache_identity(monkeypatch) -> None:
    hook = TimedPick(text="A grounded hook", start_sec=1.0, end_sec=3.0)

    def selection_hash(*, polish_copy: bool = False) -> str:
        return _carousel_selection_hash(
            drive_file_id="video-1",
            hooks=[hook],
            topics=[],
            min_slides=6,
            max_slides=10,
            select_images=False,
            polish_copy=polish_copy,
        )

    base = selection_hash()
    assert base != selection_hash(polish_copy=True)
    assert "v3-quality-diversity" in carousel_script.CAROUSEL_ALGORITHM_VERSION

    monkeypatch.setattr(carousel_script, "CAROUSEL_ALGORITHM_VERSION", "future-v4")
    assert base != selection_hash()


def test_find_duplicate_slide_pairs_detects_later_repeat() -> None:
    slides = [
        _slide("Why does this simple system work?", 1.0),
        _slide("Build the smallest useful step next.", 3.0),
        _slide("Why does this simple system work?", 5.0),
    ]
    assert find_duplicate_slide_pairs(slides) == [[0, 2]]


def test_repair_duplicate_slides_replaces_later_copy() -> None:
    cues = [
        (1.0, 3.0, "Why does this simple system work?"),
        (3.0, 5.0, "Build the smallest useful step next."),
        (5.0, 7.0, "Why does this simple system work?"),
        (9.0, 11.0, "Measure the result before you scale anything."),
        (13.0, 15.0, "Keep one grounded example in the first slide."),
        (17.0, 19.0, "Share the playbook after the payoff lands."),
    ]
    hook = TimedPick(
        text="Why does this simple system work",
        start_sec=1.0,
        end_sec=8.0,
        original_text="Why does this simple system work?",
    )
    slides = [
        _slide("Why does this simple system work?", 1.0),
        _slide("Build the smallest useful step next.", 3.0),
        _slide("Why does this simple system work?", 5.0),
        _slide("Keep one grounded example in the first slide.", 13.0),
        _slide("Share the playbook after the payoff lands.", 17.0),
        _slide("Measure the result before you scale anything.", 9.0),
    ]
    repaired, repairs = repair_duplicate_slides(
        slides,
        cues=cues,
        hook=hook,
        min_slides=4,
        drive_file_id="video-1",
        video_name="video.mp4",
        defer_images=True,
    )
    texts = [s["transcript_text"] for s in repaired]
    assert any("replaced_duplicate" in item or "dropped_duplicate" in item for item in repairs)
    assert find_duplicate_slide_pairs(repaired) == []
    assert len(repaired) >= 4
    starts = [float(s["timestamp_sec"]) for s in repaired]
    assert starts == sorted(starts)
    assert texts.count("Why does this simple system work?") <= 1


def test_repair_duplicate_slides_keeps_minimum_when_no_replacement() -> None:
    cues = [
        (1.0, 3.0, "Why does this simple system work?"),
        (3.0, 5.0, "Why does this simple system work?"),
    ]
    hook = TimedPick(text="simple system", start_sec=1.0, end_sec=5.0)
    slides = [
        _slide("Why does this simple system work?", 1.0),
        _slide("Why does this simple system work?", 3.0),
    ]
    repaired, _repairs = repair_duplicate_slides(
        slides,
        cues=cues,
        hook=hook,
        min_slides=2,
        drive_file_id="video-1",
        video_name="video.mp4",
        defer_images=True,
    )
    assert len(repaired) == 2


def test_quality_pass_records_prior_duplicate_repairs() -> None:
    carousel = {
        "slides": [
            _slide("Build the smallest useful system first.", 1.0),
            _slide("Then measure what changed.", 3.0),
        ],
        "duplicate_repairs": ["slide_3:replaced_duplicate"],
    }
    repaired, _ = apply_carousel_quality_pass([carousel])
    assert "slide_3:replaced_duplicate" in repaired[0]["quality_report"]["repairs"]


def test_select_images_keeps_only_nearby_faces_and_strips_them() -> None:
    faces = [
        {"timestamp_sec": 1.0, "bbox_x": 0.1},
        {"timestamp_sec": 120.0, "bbox_x": 0.9},
    ]
    near = _faces_near_slide(faces, {"timestamp_sec": 2.0, "end_timestamp_sec": 4.0})
    assert [f["timestamp_sec"] for f in near] == [1.0]
    cleaned = _strip_slide_ranking_fields(
        {"transcript_text": "Build the system", "faces": near, "face_detections": near}
    )
    assert "faces" not in cleaned
    assert "face_detections" not in cleaned
    assert cleaned["transcript_text"] == "Build the system"
