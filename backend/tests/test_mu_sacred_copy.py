"""MU-sacred action verb + slide craft brief guards."""

from __future__ import annotations

import pytest


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


@pytest.mark.asyncio
async def test_copy_guard_restores_only_a_grounded_sacred_action(monkeypatch):
    from app.search.carousel_pipeline import polish_slides_instagram_copy

    async def response_without_action(**_kwargs):
        return (
            '{"slides":[{"i":0,"text":"The team finally found product market fit",'
            '"highlight":[4,5,6]}]}',
            "claude",
        )

    monkeypatch.setattr(
        "app.search.carousel_pipeline._llm_complete_json",
        response_without_action,
    )
    source = (
        "After two careful experiments, we built the first working prototype. "
        "Then the audience asked unrelated questions for several minutes."
    )
    polished, _provider = await polish_slides_instagram_copy(
        [{"transcript_text": source}],
        api_key="configured",
        model="test-model",
    )

    assert "built" in polished[0]["transcript_text"].lower()
    assert "unrelated questions" not in polished[0]["transcript_text"].lower()
    assert polished[0]["highlight_words"] == ["built"]
    assert polished[0]["copy_source"].endswith("+mu_action_grounded")
    assert polished[0]["mu_action_verb_used"] is True


@pytest.mark.asyncio
async def test_copy_guard_never_invents_an_unsupported_action(monkeypatch):
    from app.search.carousel_pipeline import polish_slides_instagram_copy

    async def response_without_action(**_kwargs):
        return (
            '{"slides":[{"i":0,"text":"The audience understood the core idea",'
            '"highlight":[1]}]}',
            "claude",
        )

    monkeypatch.setattr(
        "app.search.carousel_pipeline._llm_complete_json",
        response_without_action,
    )
    polished, _provider = await polish_slides_instagram_copy(
        [{"transcript_text": "The audience understood the core idea clearly."}],
        api_key="configured",
        model="test-model",
    )

    assert polished[0]["transcript_text"] == "The audience understood the core idea"
    assert polished[0]["mu_action_verb_used"] is False
