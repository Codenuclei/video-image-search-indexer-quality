"""Crafted hooks should still rank exact transcript lines via original_text."""

from __future__ import annotations

from app.routers.carousel_script import (
    TimedPick,
    _cue_relevance,
    _hook_token_set,
    _plan_hook_oneline_spans_heuristic,
)
from app.search.carousel_pipeline import (
    _dedupe_topic_tree_hooks,
    _force_non_verbatim_hook,
    _hook_is_readable,
    _hooks_from_topic_tree,
    enforce_non_verbatim_hooks,
    heuristic_craft_hooks,
)


def test_topic_tree_hooks_are_unique_by_text_and_parent_can_be_empty():
    tree = [
        {
            "id": "topic_1",
            "hooks": [{"id": "h1", "text": "Want customer trust? Guarantee quality."}],
            "subtopics": [
                {
                    "id": "sub_1",
                    "hooks": [
                        {"id": "h1", "text": "Want customer trust? Guarantee quality."},
                        {"id": "h2", "text": "Big brands earn trust."},
                    ],
                }
            ],
        }
    ]
    normalized = _dedupe_topic_tree_hooks(tree)
    hooks = _hooks_from_topic_tree(normalized)
    assert len(hooks) == 2
    assert not normalized[0]["hooks"]
    assert len(normalized[0]["subtopics"][0]["hooks"]) == 2


def test_original_text_boosts_on_topic_cue_relevance():
    crafted = TimedPick(
        id="1",
        text="Why tier-two shoppers changed e-commerce",
        start_sec=20,
        end_sec=40,
        topic_text="e-commerce growth",
        original_text="India's e-commerce boom is being driven by tier-two cities",
    )
    without_seed = TimedPick(
        id="2",
        text="Why tier-two shoppers changed e-commerce",
        start_sec=20,
        end_sec=40,
        topic_text="e-commerce growth",
    )
    on_topic = "India's e-commerce boom is being driven by tier-two cities today"
    filler = "Please like and subscribe to our channel for more interviews"

    toks_with = _hook_token_set(crafted)
    toks_without = _hook_token_set(without_seed)
    assert _cue_relevance(on_topic, toks_with) > _cue_relevance(filler, toks_with)
    assert _cue_relevance(on_topic, toks_with) > _cue_relevance(on_topic, toks_without)


def test_heuristic_prefers_hook_span_and_relevant_lines():
    cues = [
        (10.0, 14.0, "Welcome back to another founder conversation tonight."),
        (20.0, 24.0, "India's e-commerce boom is being driven by tier-two cities."),
        (25.0, 29.0, "Tier-two shoppers now trust online marketplaces more."),
        (30.0, 34.0, "Value commerce is winning over discount-only models."),
        (35.0, 39.0, "Snapdeal focused on everyday essentials for these buyers."),
        (80.0, 84.0, "Please like and subscribe to our channel for more."),
        (90.0, 94.0, "Thanks for watching this interview until the end."),
    ]
    hook = TimedPick(
        id="1",
        text="Why tier-two shoppers changed e-commerce",
        start_sec=20,
        end_sec=40,
        topic_text="tier-two e-commerce",
        original_text="India's e-commerce boom is being driven by tier-two cities",
    )
    spans = _plan_hook_oneline_spans_heuristic(
        cues,
        hook_start=20.0,
        hook_end=40.0,
        min_slides=3,
        max_slides=6,
        hook=hook,
    )
    assert len(spans) >= 3
    # Picks should sit near the spoken hook window, not the CTA filler.
    starts = [float(s["start_sec"]) for s in spans]
    assert all(s < 70 for s in starts)


def test_force_non_verbatim_hooks_are_unique_not_hidden_pattern():
    spoken_windows = [
        "India's e-commerce boom is being driven by tier-two cities today.",
        "Value commerce is winning over discount-only models in Bharat.",
        "Snapdeal focused on everyday essentials for these buyers.",
        "Founders who worship the upload button compound faster.",
        "Campus tours hide the real admissions black box families fear.",
    ]
    used: set[str] = set()
    hooks = []
    for i, spoken in enumerate(spoken_windows):
        h = _force_non_verbatim_hook(
            spoken, theme_title="Founder stories", used=used, salt=i
        )
        assert "hidden pattern" not in h.lower()
        hooks.append(h)
        used.add(" ".join(h.lower().split()))
    heads = [" ".join(h.lower().split()[:4]) for h in hooks]
    assert len(set(heads)) == len(heads)
    assert len(set(h.lower() for h in hooks)) == len(hooks)


def test_enforce_non_verbatim_rewrites_repeated_hidden_pattern_openers():
    corpus = [
        "India's e-commerce boom is being driven by tier-two cities",
        "Value commerce is winning over discount-only models",
        "Snapdeal focused on everyday essentials for these buyers",
    ]
    hooks = [
        {
            "id": "h1",
            "text": "The hidden pattern behind India boom",
            "original_text": corpus[0],
        },
        {
            "id": "h2",
            "text": "The hidden pattern behind Value commerce",
            "original_text": corpus[1],
        },
        {
            "id": "h3",
            "text": "The hidden pattern behind Snapdeal focused",
            "original_text": corpus[2],
        },
    ]
    kept, stats = enforce_non_verbatim_hooks(hooks, corpus, theme_title="Growth")
    assert len(kept) == 3
    texts = [str(h["text"]) for h in kept]
    assert all("hidden pattern" not in t.lower() for t in texts)
    heads = [" ".join(t.lower().split()[:4]) for t in texts]
    assert len(set(heads)) == len(heads)
    assert stats.get("deduped_openings", 0) >= 1 or stats.get("rewritten", 0) >= 0


def test_force_non_verbatim_keeps_money_grammar_not_filler_templates():
    loss_to_profit = (
        "reached 6 kores and from minus 30 40 lakhs to like plus 5 lakhs "
        "plus 10 lakhs so if your entire sales first part or"
    )
    cash_burn = (
        "and a half crores and we used to burn like 40 lakhs 50 lakhs "
        "I think at that point of time and from that 3 cr we"
    )
    used: set[str] = set()
    first = _force_non_verbatim_hook(
        loss_to_profit, theme_title="Founder numbers", used=used, salt=0
    )
    used.add(" ".join(first.lower().split()))
    second = _force_non_verbatim_hook(
        cash_burn, theme_title="Founder numbers", used=used, salt=1
    )

    assert _hook_is_readable(first)
    assert _hook_is_readable(second)
    assert "like plus" not in first.lower()
    assert "burn like" not in second.lower()
    assert "lakh" in first.lower() or "crore" in first.lower()
    assert "lakh" in second.lower() or "burn" in second.lower()


def test_enforce_rewrites_ungrammatical_filler_hooks():
    spoken = (
        "reached 6 kores and from minus 30 40 lakhs to like plus 5 lakhs plus 10 lakhs"
    )
    kept, _stats = enforce_non_verbatim_hooks(
        [
            {
                "id": "h1",
                "text": "Where like plus actually wins",
                "original_text": spoken,
            },
            {
                "id": "h2",
                "text": "What burn like quietly proves",
                "original_text": (
                    "we used to burn like 40 lakhs 50 lakhs I think at that point"
                ),
            },
        ],
        [spoken],
        theme_title="Cash story",
    )
    assert kept
    texts = [str(item["text"]).lower() for item in kept]
    assert all(_hook_is_readable(item["text"]) for item in kept)
    assert all("like plus" not in text for text in texts)
    assert all("burn like" not in text for text in texts)


def test_heuristic_craft_hooks_rejects_filler_glued_headlines():
    out = heuristic_craft_hooks(
        [
            {
                "id": "hook_1",
                "text": (
                    "from minus 30 40 lakhs to like plus 5 lakhs plus 10 lakhs"
                ),
                "start_sec": 448,
                "end_sec": 456,
            }
        ],
        theme_title="Founder story",
    )
    assert out
    assert _hook_is_readable(out[0]["text"])
    assert "like" not in out[0]["text"].lower()
