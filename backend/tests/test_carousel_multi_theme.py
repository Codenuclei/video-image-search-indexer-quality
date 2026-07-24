"""Tests for multi-theme extract merge and topic timestamp spreading."""

from app.search.carousel_pipeline import (
    _spread_topic_spans,
    extract_hooks_and_topics,
    merge_preview_windows,
    merge_theme_extracts,
)


def test_merge_theme_extracts_sorts_and_dedupes():
    a = {
        "hooks": [
            {"id": "hook_1", "text": "Later hook about teams", "start_sec": 120.0, "end_sec": 125.0},
            {"id": "hook_2", "text": "Shared hook text", "start_sec": 10.0, "end_sec": 14.0},
        ],
        "topics": [
            {"id": "topic_1", "text": "Late topic", "start_sec": 130.0, "end_sec": 140.0},
        ],
        "any_translated": False,
        "english_source": "indexed",
    }
    b = {
        "hooks": [
            {"id": "hook_1", "text": "Early hook about leadership", "start_sec": 5.0, "end_sec": 9.0},
            {"id": "hook_2", "text": "Shared hook text", "start_sec": 11.0, "end_sec": 15.0},
        ],
        "topics": [
            {"id": "topic_1", "text": "Early topic", "start_sec": 8.0, "end_sec": 18.0},
            {"id": "topic_2", "text": "Late topic", "start_sec": 131.0, "end_sec": 141.0},
        ],
        "any_translated": True,
        "english_source": "caption_track",
    }
    merged = merge_theme_extracts([a, b])
    hook_texts = [h["text"] for h in merged["hooks"]]
    topic_texts = [t["text"] for t in merged["topics"]]
    assert hook_texts == [
        "Early hook about leadership",
        "Shared hook text",
        "Later hook about teams",
    ]
    assert topic_texts == ["Early topic", "Late topic"]
    assert merged["any_translated"] is True
    assert [h["id"] for h in merged["hooks"]] == ["hook_1", "hook_2", "hook_3"]


def test_spread_topic_spans_not_all_same_start():
    hooks = [
        {"start_sec": 55.0, "end_sec": 60.0},
        {"start_sec": 80.0, "end_sec": 90.0},
        {"start_sec": 110.0, "end_sec": 120.0},
    ]
    spans = _spread_topic_spans(
        3,
        theme_start=50.0,
        theme_end=130.0,
        hooks=hooks,
    )
    starts = [s for s, _ in spans]
    assert len(set(round(s, 1) for s in starts)) >= 2
    assert starts[0] < starts[-1]


def test_merge_preview_windows_unions_ranges():
    cues = [
        (10.0, 12.0, "First window line about leadership"),
        (40.0, 42.0, "Second window line about teams"),
        (90.0, 92.0, "Outside both windows"),
    ]
    rows = merge_preview_windows(cues, [(8.0, 15.0), (38.0, 45.0)], limit=10)
    texts = [r["text"] for r in rows]
    assert any("leadership" in t for t in texts)
    assert any("teams" in t for t in texts)
    assert not any("Outside" in t for t in texts)


def test_extract_topics_spread_across_theme_window():
    cues = [
        (0.0, 4.0, "So friends today we will talk about leadership in depth"),
        (4.0, 8.0, "First we need to understand how decisions are made under pressure"),
        (8.0, 12.0, "Then we will cover teamwork and feedback loops in detail"),
        (12.0, 16.0, "Finally a career takeaway you can use starting tomorrow"),
        (55.0, 60.0, "Another section talks about hiring mistakes people make"),
        (60.0, 65.0, "Interview loops fail when feedback is delayed for weeks"),
        (65.0, 70.0, "Close with a practical checklist for your next hire"),
    ]
    result = extract_hooks_and_topics(
        cues,
        start_sec=55.0,
        end_sec=70.0,
        theme_title="Hiring loops",
        theme_summary="How interview feedback breaks hiring quality.",
    )
    starts = [float(t["start_sec"]) for t in result["topics"]]
    assert starts
    assert min(starts) >= 50.0
    # Not every topic stamped at the same second.
    if len(starts) > 1:
        assert max(starts) - min(starts) > 0.5
