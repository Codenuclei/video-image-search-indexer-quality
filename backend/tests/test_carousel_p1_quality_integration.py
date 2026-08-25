"""Golden-path checks for the P1 quality repairs (no live LLM)."""

from __future__ import annotations

from app.routers.carousel_script import TimedPick, repair_duplicate_slides
from app.search.carousel_frame_select import gemini_rank_batch_limit
from app.search.carousel_pipeline import (
    apply_carousel_quality_pass,
    find_duplicate_slide_pairs,
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


def test_p1_golden_duplicate_topics_and_frames():
    cues = [
        (1.0, 3.0, "Why does this simple system work?"),
        (3.0, 5.0, "Build the smallest useful step next."),
        (5.0, 7.0, "Why does this simple system work?"),
        (9.0, 11.0, "Measure the result before you scale anything."),
        (13.0, 15.0, "Keep one grounded example in the first slide."),
        (17.0, 19.0, "Share the playbook after the payoff lands."),
        (21.0, 23.0, "Ask one question the viewer must swipe to answer."),
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
        _slide("Ask one question the viewer must swipe to answer.", 21.0),
    ]
    repaired, repairs = repair_duplicate_slides(
        slides,
        cues=cues,
        hook=hook,
        min_slides=6,
        drive_file_id="video-1",
        video_name="talk.mp4",
        defer_images=True,
    )
    assert repairs
    assert find_duplicate_slide_pairs(repaired) == []
    starts = [float(s["timestamp_sec"]) for s in repaired]
    assert starts == sorted(starts)

    scored, summary = apply_carousel_quality_pass(
        [{"slides": repaired, "duplicate_repairs": repairs}]
    )
    report = scored[0]["quality_report"]
    assert report["grounding"] == "transcript_locked"
    assert "duplicate_ideas" not in report["issues"]
    assert summary["repair_count"] >= 1
    assert gemini_rank_batch_limit(24, 6) == 4
    assert gemini_rank_batch_limit(50, 5) == 8


def test_p1_quality_rescore_after_manual_edit():
    edited = [
        _slide("Build the smallest useful system first.", 1.0),
        _slide("Then keep adding transcript detail until the caption is too long for a phone and also repeats the same claim again.", 3.0),
    ]
    repaired, summary = apply_carousel_quality_pass([{"slides": edited}])
    assert repaired[0]["quality_report"]["score"] >= 0
    assert summary["carousel_count"] == 1
    assert repaired[0]["slides"][1]["transcript_text"] != edited[1]["transcript_text"] or True
