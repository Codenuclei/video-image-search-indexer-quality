"""Person-only queries skip captions; person+action still allows them."""

from unittest.mock import AsyncMock, patch

import pytest

from app.search.local import (
    find_person_names_in_query,
    is_action_query,
    is_weak_person_visual,
    normalize_person_key,
    resolve_search_context,
    strip_person_names,
)


KNOWN = ["Pratham Mittal", "Alice"]


def _caption_decision(query: str, *, captions_toggle: bool = True) -> dict:
    """Mirror search.py person-focused caption override."""
    matched = find_person_names_in_query(query, KNOWN)
    visual = strip_person_names(query, matched) if matched else query
    person_action = bool(matched and is_action_query(visual or query))
    person_focused = bool(
        matched
        and is_weak_person_visual(visual, matched)
    )
    use_captions = bool(captions_toggle or person_action)
    if person_focused and not person_action:
        use_captions = False
    return {
        "matched": matched,
        "visual": visual,
        "person_focused": person_focused,
        "person_action": person_action,
        "use_captions": use_captions,
    }


def test_normalize_person_key_collapses_whitespace_and_case():
    assert normalize_person_key("  Pratham   Mittal ") == "pratham mittal"
    assert normalize_person_key("PRATHAM MITTAL") == normalize_person_key("pratham mittal")


def test_find_person_names_case_and_flexible_whitespace():
    assert find_person_names_in_query("pratham mittal", KNOWN) == ["Pratham Mittal"]
    assert find_person_names_in_query("Pratham  Mittal", KNOWN) == ["Pratham Mittal"]
    assert find_person_names_in_query("PRATHAM\tMITTAL", KNOWN) == ["Pratham Mittal"]


def test_strip_person_names_flexible_whitespace():
    assert strip_person_names("Pratham  Mittal cooking", ["Pratham Mittal"]) == "cooking"
    assert strip_person_names("pratham mittal", ["Pratham Mittal"]) == ""


def test_person_only_with_captions_toggle_forces_captions_off():
    for q in ("Pratham Mittal", "pratham mittal", "Pratham  Mittal"):
        decision = _caption_decision(q, captions_toggle=True)
        assert decision["matched"] == ["Pratham Mittal"]
        assert decision["person_focused"] is True
        assert decision["person_action"] is False
        assert decision["use_captions"] is False


def test_person_plus_action_keeps_captions():
    decision = _caption_decision("Pratham Mittal cooking", captions_toggle=False)
    assert decision["matched"] == ["Pratham Mittal"]
    assert decision["visual"] == "cooking"
    assert decision["person_action"] is True
    assert decision["use_captions"] is True


def test_non_person_query_honors_captions_toggle():
    decision = _caption_decision("students cooking", captions_toggle=True)
    assert decision["matched"] == []
    assert decision["use_captions"] is True

    decision_off = _caption_decision("students cooking", captions_toggle=False)
    assert decision_off["use_captions"] is False


@pytest.mark.asyncio
async def test_resolve_search_context_person_param_normalizes_whitespace():
    session = AsyncMock()
    with patch(
        "app.search.local.known_person_names",
        new=AsyncMock(return_value=KNOWN),
    ):
        persons, visual, _ = await resolve_search_context(
            session, "photos", "pratham  mittal"
        )
    assert persons == ["Pratham Mittal"]
    assert "pratham" not in visual.casefold()


@pytest.mark.asyncio
async def test_resolve_search_context_query_flexible_whitespace():
    session = AsyncMock()
    with patch(
        "app.search.local.known_person_names",
        new=AsyncMock(return_value=KNOWN),
    ):
        persons, visual, _ = await resolve_search_context(
            session, "Pratham  Mittal", None
        )
    assert persons == ["Pratham Mittal"]
    assert is_weak_person_visual(visual, persons)
