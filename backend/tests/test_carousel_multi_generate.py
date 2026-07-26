"""Multi-carousel generation: one carousel per topic + mixed multi-topic sets."""

from app.routers.carousel_script import (
    TimedPick,
    _beats_for_mixed_topics,
    _beats_for_topic,
    _mixed_topic_groups,
)


def _topic(text: str, start: float, end: float | None = None) -> TimedPick:
    return TimedPick(id=text.lower().replace(" ", "_"), text=text, start_sec=start, end_sec=end)


def test_per_topic_carousel_has_multiple_slides():
    topic = _topic("Student-First Philosophy", 10, 30)
    hooks = [
        _topic("Students decide what we build", 12, 15),
        _topic("Every roadmap starts in a classroom", 20, 24),
    ]
    moments = _beats_for_topic(
        topic=topic,
        hooks=hooks,
        themes=[],
        video_id="file1",
        video_name="lecture.mp4",
        min_slides=3,
        max_slides=6,
    )
    assert len(moments) >= 3
    assert all(m.drive_file_id == "file1" for m in moments)
    assert [m.timestamp_sec for m in moments] == sorted(m.timestamp_sec for m in moments)
    # Frames must be span-aligned URLs, not a single static image.
    assert len({m.preview_url for m in moments}) == len(moments)


def test_per_topic_pads_when_no_hooks():
    moments = _beats_for_topic(
        topic=_topic("Research Impact", 100, 140),
        hooks=[],
        themes=[],
        video_id="file1",
        video_name="talk.mp4",
        min_slides=4,
        max_slides=6,
    )
    assert len(moments) >= 4
    texts = [m.snippet for m in moments]
    assert len(set(texts)) == len(texts)


def test_per_topic_slides_are_spread_out():
    """Padding beats must not stack near-identical frames."""
    moments = _beats_for_topic(
        topic=_topic("Campus Culture", 50, 54),
        hooks=[],
        themes=[],
        video_id="file1",
        video_name="talk.mp4",
        min_slides=4,
        max_slides=6,
    )
    stamps = sorted(m.timestamp_sec for m in moments)
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(moments) >= 4
    assert all(g >= 1.5 for g in gaps)


def test_mixed_groups_combine_topics():
    seeds = [
        _topic("Campus Culture", 0, 20),
        _topic("Career Pathways", 30, 50),
        _topic("Research Impact", 60, 80),
    ]
    groups = _mixed_topic_groups(seeds)
    assert groups
    assert any(len(g) >= 2 for g in groups)
    # Full-set narrative included for 3 topics
    assert any(len(g) == 3 for g in groups)


def test_mixed_carousel_multi_slide():
    group = [_topic("Campus Culture", 0, 20), _topic("Career Pathways", 30, 50)]
    hooks = [_topic("Culture is a hiring decision", 5, 9)]
    moments = _beats_for_mixed_topics(
        group=group,
        hooks=hooks,
        video_id="file1",
        video_name="talk.mp4",
        min_slides=3,
        max_slides=6,
    )
    assert len(moments) >= 3
    kinds = {m.match_type for m in moments}
    assert "topic" in kinds
