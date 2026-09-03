"""Combined-topic hook craft (2–4 total, not per-topic)."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_craft_hooks_combined_not_per_topic(monkeypatch):
    from app.search import carousel_pipeline as pipe

    topics = [
        {
            "id": "topic_1",
            "text": "Warm network first",
            "start_sec": 10.0,
            "end_sec": 40.0,
            "explanation": "Use warm intros before tools.",
        },
        {
            "id": "topic_2",
            "text": "Show up in person",
            "start_sec": 40.0,
            "end_sec": 80.0,
            "explanation": "Close early customers face to face.",
        },
    ]
    cues = [
        (12.0, 18.0, "Don't buy prospecting tools until you have customers."),
        (22.0, 30.0, "Your warm network is hundreds of second degree connections."),
        (45.0, 55.0, "YC founders close by being in the same room."),
        (60.0, 70.0, "Show up in person and ask for the sale."),
    ]

    seed = [
        {
            "text": "Don't buy prospecting tools until you have customers.",
            "start_sec": 12.0,
            "end_sec": 18.0,
        },
        {
            "text": "YC founders close by being in the same room.",
            "start_sec": 45.0,
            "end_sec": 55.0,
        },
        {
            "text": "Your warm network is hundreds of second degree connections.",
            "start_sec": 22.0,
            "end_sec": 30.0,
        },
    ]

    async def fake_llm(**kwargs):
        assert len(kwargs["topics"]) == 2
        assert kwargs["max_hooks"] == 4
        return [
            {
                **seed[0],
                "text": "Warm intros beat cold tools every time",
                "original_text": seed[0]["text"],
                "verbatim": False,
                "combined_topics": True,
            },
            {
                **seed[1],
                "text": "Close your first customers face to face",
                "original_text": seed[1]["text"],
                "verbatim": False,
                "combined_topics": True,
            },
            {
                **seed[2],
                "text": "Network first then tools never reverse that order",
                "original_text": seed[2]["text"],
                "verbatim": False,
                "combined_topics": True,
            },
        ]

    monkeypatch.setattr(pipe, "_llm_has_any_key", lambda **k: True)
    monkeypatch.setattr(pipe, "_llm_hooks_for_combined_topics", fake_llm)
    monkeypatch.setattr(
        pipe,
        "enforce_non_verbatim_hooks",
        lambda hooks, corpus, theme_title="": (list(hooks), {}),
    )
    monkeypatch.setattr(pipe, "_pick_contextual_hooks", lambda stitched: list(seed))
    monkeypatch.setattr(pipe, "_emergency_hook_candidates", lambda *a, **k: list(seed))
    monkeypatch.setattr(pipe, "_stitch_complete_utterances", lambda window: list(seed))
    monkeypatch.setattr(
        pipe,
        "_cues_for_topic_ranges",
        lambda pool, topic, fallback_start=0, fallback_end=None: list(cues),
    )
    monkeypatch.setattr(pipe, "_dedupe_hook_list", lambda hooks: list(hooks))

    result = await pipe.craft_hooks_for_selected_topics_async(
        cues,
        selected_topics=topics,
        theme_title="Founder GTM",
        min_hooks=2,
        max_hooks=4,
    )

    hooks = result["hooks"]
    assert 2 <= len(hooks) <= 4
    assert result["source"] == "selected_topics_combined"
    assert result["combined_topic_count"] == 2
    for node in result["topic_tree"]:
        assert node.get("hooks") == []
    assert all(h.get("combined_topics") is True for h in hooks)
    assert all(h.get("topic_id") is None for h in hooks)
