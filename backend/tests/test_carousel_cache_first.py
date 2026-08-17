"""Strict cache-first gates for themes / extract / generate — no silent Gemini."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.routers.carousel_script import (
    CarouselGenerateRequest,
    PipelineThemesRequest,
    TimedPick,
    _carousel_selection_hash,
    _extract_theme_key,
    carousel_pipeline_generate,
    carousel_pipeline_themes,
    PipelineThemeSlice,
)
from app.search.carousel_pipeline import THEME_PROMPT_VERSION


def _cues():
    return [(0.0, 1.0, "hello world")]


@pytest.mark.asyncio
async def test_themes_cache_miss_without_generate_never_calls_llm():
    drive = SimpleNamespace(id="vid1", name="Talk.mp4")
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
    )

    with (
        patch(
            "app.routers.carousel_script._load_video_cues",
            new=AsyncMock(return_value=(drive, _cues())),
        ),
        patch(
            "app.routers.carousel_script.build_harmonized_themes",
            new=AsyncMock(side_effect=AssertionError("Gemini must not run")),
        ) as themes_llm,
        patch("app.routers.carousel_script.get_settings") as gs,
    ):
        gs.return_value = SimpleNamespace(
            gemini_model="gemini-test",
            gemini_api_key="x",
            anthropic_api_key="",
            claude_api_key="",
            claude_model="",
        )
        body = PipelineThemesRequest(drive_file_id="vid1", force=False, generate=False)
        res = await carousel_pipeline_themes(body, session)

    assert res["cache_hit"] is False
    assert res["generated"] is False
    assert res["themes"] == []
    assert "No cached themes" in (res.get("message") or "")
    themes_llm.assert_not_called()


@pytest.mark.asyncio
async def test_themes_cache_hit_returns_saved_without_llm():
    drive = SimpleNamespace(id="vid1", name="Talk.mp4")
    saved_themes = [
        {
            "theme_id": "t1",
            "title": "Intro",
            "start_sec": 0,
            "end_sec": 10,
            "summary": "Opening",
        }
    ]
    row = SimpleNamespace(
        id=99,
        model=f"gemini-test:{THEME_PROMPT_VERSION}",
        source="saved",
        payload={"themes": saved_themes, "cue_count": 1, "source": "saved"},
    )
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [row]))
    )

    with (
        patch(
            "app.routers.carousel_script._load_video_cues",
            new=AsyncMock(return_value=(drive, _cues())),
        ),
        patch(
            "app.routers.carousel_script.build_harmonized_themes",
            new=AsyncMock(side_effect=AssertionError("Gemini must not run")),
        ),
        patch(
            "app.routers.carousel_script.carousel_llm_cache_id",
            return_value="gemini-test",
        ),
        patch("app.routers.carousel_script.get_settings") as gs,
    ):
        gs.return_value = SimpleNamespace(
            gemini_model="gemini-test",
            gemini_api_key="x",
            anthropic_api_key="",
            claude_api_key="",
            claude_model="",
        )
        body = PipelineThemesRequest(drive_file_id="vid1", force=False, generate=False)
        res = await carousel_pipeline_themes(body, session)

    assert res["cache_hit"] is True
    assert res["generated"] is False
    assert res["save_id"] == 99
    assert len(res["themes"]) == 1


def test_generate_rejects_multiple_hooks_at_schema():
    with pytest.raises(ValidationError):
        CarouselGenerateRequest(
            drive_file_id="vid1",
            hooks=[
                TimedPick(text="Hook A", start_sec=1.0),
                TimedPick(text="Hook B", start_sec=2.0),
            ],
            generate=True,
        )


def test_selection_hash_stable_for_same_hook():
    a = TimedPick(text="Hello World", start_sec=1.0, end_sec=2.0, id="h1")
    b = TimedPick(text="  hello   world ", start_sec=1.0, end_sec=2.0, id="h1")
    h1 = _carousel_selection_hash(
        drive_file_id="vid",
        hooks=[a],
        topics=[],
        min_slides=6,
        max_slides=10,
        select_images=False,
    )
    h2 = _carousel_selection_hash(
        drive_file_id="vid",
        hooks=[b],
        topics=[],
        min_slides=6,
        max_slides=10,
        select_images=False,
    )
    assert h1 == h2
    assert len(h1) == 64


def test_extract_theme_key_joins_ids():
    key = _extract_theme_key(
        [
            PipelineThemeSlice(theme_id="a", title="A", start_sec=0),
            PipelineThemeSlice(theme_id="b", title="B", start_sec=10),
        ]
    )
    assert key == "a|b"


@pytest.mark.asyncio
async def test_generate_cache_miss_without_generate_skips_llm():
    session = AsyncMock()
    session.get = AsyncMock(return_value=SimpleNamespace(name="Talk.mp4"))
    session.scalar = AsyncMock(return_value=None)

    body = CarouselGenerateRequest(
        drive_file_id="vid1",
        hooks=[TimedPick(text="Only hook", start_sec=1.0, end_sec=3.0)],
        force=False,
        generate=False,
    )
    with patch(
        "app.routers.carousel_script._build_hook_carousels",
        new=AsyncMock(side_effect=AssertionError("must not build")),
    ):
        res = await carousel_pipeline_generate(body, session)

    assert res["cache_hit"] is False
    assert res["generated"] is False
    assert res["carousels"] == []
    assert "No cached carousel" in (res.get("message") or "")
