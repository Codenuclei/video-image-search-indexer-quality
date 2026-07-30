"""Per-hook carousels must not share transcript lines or near-identical times."""

from __future__ import annotations

from app.routers.carousel_script import (
    TimedPick,
    _plan_hook_oneline_spans_heuristic,
    _slides_from_exact_spans,
    _top_up_oneline_slides,
)


def _build(cues, hook, reserved_texts, reserved_starts, *, min_slides=6):
    spans = _plan_hook_oneline_spans_heuristic(
        cues,
        hook_start=float(hook.start_sec or 0),
        hook_end=hook.end_sec,
        min_slides=min_slides,
        max_slides=8,
        reserved_texts=reserved_texts,
        reserved_starts=reserved_starts,
        hook=hook,
    )
    slides = _slides_from_exact_spans(
        spans,
        cues=cues,
        drive_file_id="vid",
        video_name="vid.mp4",
        crafted_hook=hook.text,
        defer_images=True,
        reserved_texts=reserved_texts,
        reserved_starts=reserved_starts,
    )
    return _top_up_oneline_slides(
        slides,
        cues=cues,
        hook=hook,
        min_slides=min_slides,
        max_slides=8,
        drive_file_id="vid",
        video_name="vid.mp4",
        defer_images=True,
        reserved_texts=reserved_texts,
        reserved_starts=reserved_starts,
    )


def test_two_hooks_do_not_share_transcript_lines():
    cues = [
        (10.0, 14.0, "Bookface YC social network asking founders."),
        (15.0, 19.0, "First ten customers came from cold email."),
        (20.0, 24.0, "They posted in a private Slack community."),
        (25.0, 29.0, "Warm intros beat spray and pray outreach."),
        (42.0, 46.0, "Bookface YC internal asking founders first customers."),
        (49.0, 53.0, "You can do it from your laptop today."),
        (64.0, 68.0, "LinkedIn outreach to a legacy industry works."),
        (80.0, 84.0, "They picked up the phone and closed."),
        (100.0, 104.0, "Are they on Reddit or somewhere else?"),
        (110.0, 114.0, "Do they pick up the phone when you call?"),
        (130.0, 134.0, "Stop guessing where customers hang out."),
        (140.0, 144.0, "Ideal buyers live in niche forums."),
        (150.0, 154.0, "Map communities before you write copy."),
        (160.0, 164.0, "Reddit threads reveal real buying language."),
        (170.0, 174.0, "Hang where they already trust strangers."),
        (180.0, 184.0, "Listen first then ask for the intro."),
    ]
    h1 = TimedPick(
        id="1",
        text="Unlock tactics for first 10 customers",
        start_sec=10,
        end_sec=30,
        topic_text="first customers",
    )
    h2 = TimedPick(
        id="2",
        text="Where does ideal customer really hang out",
        start_sec=100,
        end_sec=180,
        topic_text="customer hangouts",
    )
    reserved_texts: set[str] = set()
    reserved_starts: set[float] = set()
    slides1 = _build(cues, h1, reserved_texts, reserved_starts)
    for s in slides1:
        reserved_texts.add((s.get("transcript_text") or "").strip().lower())
        reserved_starts.add(round(float(s.get("timestamp_sec") or 0), 1))
    slides2 = _build(cues, h2, reserved_texts, reserved_starts)

    assert len(slides1) >= 6
    assert len(slides2) >= 6
    t1 = {(s.get("transcript_text") or "").strip().lower() for s in slides1}
    t2 = {(s.get("transcript_text") or "").strip().lower() for s in slides2}
    assert not (t1 & t2)

    # Within a hook, starts must be spaced (≥2s one-idea-per-slide).
    for slides in (slides1, slides2):
        starts = sorted(float(s.get("timestamp_sec") or 0) for s in slides)
        for a, b in zip(starts, starts[1:]):
            assert b - a >= 1.9
