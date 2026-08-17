from __future__ import annotations

import json

import pytest

from app.routers.carousel_script import _themes_transcript_hash
from app.search.carousel_pipeline import (
    _condense_transcript_outline,
    _parse_themes_json,
    _theme_quality_issues,
    build_harmonized_themes,
)


def _themes_json() -> str:
    return json.dumps(
        [
            {
                "theme_id": "theme_1",
                "title": "Reframing Customer Acquisition",
                "start_sec": 0,
                "end_sec": 100,
                "summary": "The speaker explains why acquisition depends on identifying the real customer.",
            },
            {
                "theme_id": "theme_2",
                "title": "Distribution Across Asian Markets",
                "start_sec": 100,
                "end_sec": 200,
                "summary": "The discussion compares expansion mechanics across distinct regional markets.",
            },
            {
                "theme_id": "theme_3",
                "title": "Trust as a Growth Moat",
                "start_sec": 200,
                "end_sec": 300,
                "summary": "The speaker argues that product quality turns brand trust into durable growth.",
            },
        ]
    )


def test_theme_quality_rejects_raw_transcript_fragments() -> None:
    issues = _theme_quality_issues(
        [
            {
                "title": "Now as you reposition Snapdeal who's the customer",
                "summary": ">> there is a 200 million customer pool >> if I talk about tier 2",
                "start_sec": 0,
                "end_sec": 120,
            },
            {
                "title": "go after the space",
                "summary": "and what it is today",
                "start_sec": 120,
                "end_sec": 240,
            },
            {
                "title": "even if I buy it from XYZ platform",
                "summary": "the platform is the brand >> so you have to build trust",
                "start_sec": 240,
                "end_sec": 360,
            },
        ]
    )

    assert any("raw speech" in issue for issue in issues)
    assert any("copied, fragmentary" in issue for issue in issues)


def test_condensed_outline_samples_the_end_of_long_transcript() -> None:
    cues = [
        (float(i), float(i + 1), f"Discussion point {i} " + ("detail " * 30))
        for i in range(120)
    ]
    cues[-1] = (119.0, 120.0, "FINAL_MARKER foreign market expansion strategy")

    outline = _condense_transcript_outline(cues, max_chars=2_000)

    assert "FINAL_MARKER" in outline
    assert len(outline) <= 2_000


def test_theme_parser_accepts_wrapped_array() -> None:
    parsed = _parse_themes_json(json.dumps({"themes": json.loads(_themes_json())}))

    assert len(parsed) == 3
    assert parsed[0]["title"] == "Reframing Customer Acquisition"


def test_theme_transcript_hash_includes_long_transcript_tail() -> None:
    cues = [
        (float(i), float(i + 1), f"Discussion point {i} " + ("detail " * 30))
        for i in range(120)
    ]
    changed = list(cues)
    changed[-1] = (119.0, 120.0, "A completely different closing strategy")

    assert _themes_transcript_hash(cues) != _themes_transcript_hash(changed)


@pytest.mark.asyncio
async def test_theme_generation_reads_every_chunk_and_synthesizes(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_complete(**kwargs):
        prompt = str(kwargs["prompt"])
        calls.append(prompt)
        return _themes_json(), "openrouter"

    monkeypatch.setattr(
        "app.search.carousel_pipeline._llm_complete_json",
        fake_complete,
    )
    cues = [
        (float(i), float(i + 1), f"Business discussion {i} " + ("detail " * 25))
        for i in range(160)
    ]
    cues[-1] = (159.0, 160.0, "FINAL_MARKER international distribution strategy")

    themes, source, warning = await build_harmonized_themes(
        cues=cues,
        video_name="Founder interview",
        api_key="",
        model="",
        openrouter_api_key="configured",
        openrouter_model="test-model",
        provider="openrouter",
    )

    chunk_prompts = [prompt for prompt in calls if "transcript chunk" in prompt]
    assert len(chunk_prompts) > 1
    assert any("FINAL_MARKER" in prompt for prompt in chunk_prompts)
    assert any("FINAL theme map" in prompt for prompt in calls)
    assert len(themes) == 3
    assert source == "openrouter"
    assert warning is None
