"""Regression: topics-only extract must not prune the tree for empty hooks."""

from __future__ import annotations

from app.search.carousel_pipeline import _drop_empty_hook_sections


def test_drop_empty_hook_sections_wipes_topics_only_tree() -> None:
    """Topics-only clears hooks on purpose; the drop helper would erase the tree.

    The extract router must skip this prune when include_hooks=False.
    """
    tree = [
        {
            "id": "topic_1",
            "text": "Network then hustle then automation",
            "hooks": [],
            "subtopics": [
                {"id": "sub_1", "text": "Warm network first", "hooks": []},
            ],
        },
        {
            "id": "topic_2",
            "text": "1-3-10-50 framework",
            "hooks": [],
            "subtopics": [],
        },
    ]
    assert _drop_empty_hook_sections(tree) == []


def test_drop_empty_hook_sections_keeps_sections_with_hooks() -> None:
    tree = [
        {
            "id": "topic_1",
            "text": "Network first",
            "hooks": [{"id": "h1", "text": "Start with warm intros"}],
            "subtopics": [],
        },
        {
            "id": "topic_2",
            "text": "Empty leftover",
            "hooks": [],
            "subtopics": [],
        },
    ]
    kept = _drop_empty_hook_sections(tree)
    assert len(kept) == 1
    assert kept[0]["id"] == "topic_1"
