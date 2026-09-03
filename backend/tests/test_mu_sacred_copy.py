"""MU-sacred action verb + slide craft brief guards."""

from __future__ import annotations


def test_mu_sacred_verbs_present_in_hook_and_slide_briefs():
    from app.search.carousel_pipeline import (
        SLIDE_COPY_PROMPT_VERSION,
        _HOOK_CRAFT_BRIEF,
        _MU_SACRED_ACTION_VERBS,
        _SLIDE_CRAFT_BRIEF,
    )

    required = [
        "Built",
        "Shipped",
        "Created",
        "Explored",
        "Experimented",
        "Failed",
        "Raised",
        "Invested",
    ]
    for verb in required:
        assert verb in _MU_SACRED_ACTION_VERBS
        assert verb in _HOOK_CRAFT_BRIEF or "MU-SACRED ACTION VERB" in _HOOK_CRAFT_BRIEF
        assert "MU-SACRED ACTION VERB" in _SLIDE_CRAFT_BRIEF
    assert "MU-SACRED ACTION VERB" in _HOOK_CRAFT_BRIEF
    assert _MU_SACRED_ACTION_VERBS.split(",")[0].strip() in _HOOK_CRAFT_BRIEF
    assert "mu-sacred" in SLIDE_COPY_PROMPT_VERSION or "sacred" in SLIDE_COPY_PROMPT_VERSION
