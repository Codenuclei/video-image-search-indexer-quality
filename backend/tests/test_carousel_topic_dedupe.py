"""Topic semantic / heuristic dedupe for carousel extract."""

from app.search.carousel_pipeline import (
    heuristic_craft_hooks,
    heuristic_topic_dedupe,
    _nearly_verbatim,
)


def test_heuristic_dedupes_student_first_variants():
    topics = [
        {"id": "topic_1", "text": "Student-First Philosophy", "start_sec": 10},
        {"id": "topic_2", "text": "Student-Centric Decisions", "start_sec": 20},
        {"id": "topic_3", "text": "Faculty Mentorship", "start_sec": 30},
    ]
    out = heuristic_topic_dedupe(topics)
    labels = [t["text"].lower() for t in out]
    assert len(out) == 2
    assert any("student" in x for x in labels)
    assert any("faculty" in x or "mentorship" in x for x in labels)


def test_heuristic_keeps_distinct_topics():
    topics = [
        {"text": "Campus Culture", "start_sec": 0},
        {"text": "Career Pathways", "start_sec": 10},
        {"text": "Research Impact", "start_sec": 20},
    ]
    out = heuristic_topic_dedupe(topics)
    assert len(out) == 3


def test_nearly_verbatim_detects_copies():
    spoken = "We always put students at the center of every decision we make"
    crafted = "We always put students at the center of every decision we make today"
    assert _nearly_verbatim(crafted, spoken)
    assert not _nearly_verbatim("Students come first — always.", spoken)


def test_heuristic_craft_hooks_compresses_dumps():
    hooks = [
        {
            "id": "hook_1",
            "text": (
                "so you know we always put students at the center of every decision "
                "we make and that changes how we hire and build products every day"
            ),
            "start_sec": 10,
            "end_sec": 20,
            "verbatim": True,
        }
    ]
    out = heuristic_craft_hooks(hooks, theme_title="Student-First")
    assert len(out) == 1
    assert out[0]["analysed"] is True
    assert out[0]["verbatim"] is False
    assert out[0]["start_sec"] == 10
    assert len(out[0]["text"].split()) <= 18
    assert out[0]["text"].lower() != hooks[0]["text"].lower()
