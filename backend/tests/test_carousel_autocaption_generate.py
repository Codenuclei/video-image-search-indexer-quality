"""Carousel generation must work on lowercase, unpunctuated ASR transcripts."""

import pytest

from app.routers.carousel_script import (
    _RELAXED_CUE_LINES,
    TimedPick,
    _build_hook_carousels,
    _cue_corpus_needs_relaxed_lines,
    _line_complete_enough,
    _line_starts_clean,
)

# Shape of a YouTube auto-caption track: no terminators, no sentence casing.
AUTOCAPTION_CUES = [
    (0.5, 2.4, "question with connectivity and listening"),
    (2.4, 4.9, "to lecture after lecture really prepare"),
    (4.9, 6.9, "you for the true business world"),
    (6.9, 9.2, "students are trained by real founders"),
    (9.2, 11.8, "different this is masters union a next"),
    (11.8, 14.4, "generation business school in delhi"),
    (14.4, 17.0, "learning happens both inside and outside"),
    (17.0, 19.5, "the classroom where classes are taught"),
    (19.5, 22.0, "by operators who run real companies"),
    (22.0, 24.6, "every student manages a live portfolio"),
    (24.6, 27.2, "capital is real and losses are real"),
    (27.2, 29.8, "placements come from that track record"),
    (29.8, 32.4, "recruiters see proof instead of grades"),
    (32.4, 35.0, "alumni now lead teams across industries"),
]

PUNCTUATED_CUES = [
    (0.5, 3.0, "Ghee is more than a food."),
    (3.0, 6.0, "It has been a tradition in India."),
    (6.0, 9.0, "The business is extremely profitable."),
    (9.0, 12.0, "Margins reach eighty percent in some cases."),
]


@pytest.fixture(autouse=True)
def _reset_relaxed_lines():
    token = _RELAXED_CUE_LINES.set(False)
    yield
    _RELAXED_CUE_LINES.reset(token)


def test_autocaption_corpus_is_detected():
    assert _cue_corpus_needs_relaxed_lines(AUTOCAPTION_CUES)
    assert not _cue_corpus_needs_relaxed_lines(PUNCTUATED_CUES)


def test_strict_gates_reject_every_autocaption_line():
    """Regression guard: this starvation is what returned zero carousels."""
    assert not any(
        _line_starts_clean(text) and _line_complete_enough(text)
        for _s, _e, text in AUTOCAPTION_CUES
    )


def test_relaxed_gates_accept_autocaption_lines_but_not_fragments():
    _RELAXED_CUE_LINES.set(True)
    assert _line_starts_clean("students are trained by real founders")
    assert _line_complete_enough("students are trained by real founders")
    # Bare clause continuations stay rejected so slides read as one idea.
    assert not _line_starts_clean("to lecture after lecture really prepare")
    assert not _line_starts_clean("you for the true business world")
    assert not _line_starts_clean("and it all fits on one screen")
    assert not _line_starts_clean("And it all fits on one screen")


def test_punctuated_transcript_keeps_strict_line_rules():
    assert _line_complete_enough("It has been a tradition in India.")
    assert not _line_complete_enough("it has been a tradition in India")


@pytest.mark.asyncio
async def test_autocaption_transcript_builds_carousel():
    hooks = [
        TimedPick(
            id="hook_1",
            text="masters union trains students with real operators",
            start_sec=9.2,
            end_sec=14.4,
        )
    ]
    kwargs = dict(
        unique_hooks=hooks,
        cue_corpus=AUTOCAPTION_CUES,
        drive_file_id="yt:test",
        video_name="Life At Masters Union.mp4",
        min_slides=6,
        max_slides=8,
        select_images=False,
        api_key=None,
        model=None,
    )

    starved = await _build_hook_carousels(**kwargs)
    assert starved == []

    _RELAXED_CUE_LINES.set(True)
    built = await _build_hook_carousels(**kwargs)
    assert len(built) == 1
    slides = built[0]["slides"]
    assert len(slides) >= 2
    assert all((s.get("transcript_text") or "").strip() for s in slides)
    # Slides stay exact transcript text and keep deferred images.
    cue_texts = {text for _s, _e, text in AUTOCAPTION_CUES}
    assert any(
        any(s["transcript_text"] in cue or cue in s["transcript_text"] for cue in cue_texts)
        for s in slides
    )
    assert all(s.get("frame_source") == "deferred" for s in slides)


@pytest.mark.asyncio
async def test_oneline_span_plan_honors_selected_llm(monkeypatch):
    """Cut planning must call the studio LLM router, not a Gemini-only path."""
    import json

    from app.routers.carousel_script import _plan_hook_oneline_spans

    captured: dict[str, object] = {}

    async def fake_complete_json(**kwargs):
        captured["provider"] = kwargs.get("provider")
        captured["claude_api_key"] = kwargs.get("claude_api_key")
        captured["model"] = kwargs.get("model")
        catalog = [
            {"i": 10, "s": 9.2, "e": 11.8, "t": "different this is masters union a next"},
            {"i": 11, "s": 11.8, "e": 14.4, "t": "generation business school in delhi"},
            {"i": 12, "s": 14.4, "e": 17.0, "t": "learning happens both inside and outside"},
            {"i": 17, "s": 22.0, "e": 24.6, "t": "every student manages a live portfolio"},
        ]
        payload = {
            "spans": [
                {"cue_i": row["i"], "start_sec": row["s"], "end_sec": row["e"]}
                for row in catalog
            ]
        }
        return json.dumps(payload), "claude"

    monkeypatch.setattr(
        "app.search.carousel_pipeline._llm_complete_json",
        fake_complete_json,
    )

    plan = await _plan_hook_oneline_spans(
        cues=PUNCTUATED_CUES + AUTOCAPTION_CUES,
        hook=TimedPick(
            id="hook_1",
            text="masters union trains students with real operators",
            start_sec=9.2,
            end_sec=14.4,
        ),
        min_slides=3,
        max_slides=6,
        llm={
            "provider": "claude",
            "api_key": "",
            "model": "gemini-2.5-pro",
            "claude_api_key": "sk-ant-test",
            "claude_model": "claude-sonnet-4-5-20250929",
            "openrouter_api_key": "",
            "openrouter_model": "",
            "openrouter_base_url": "",
        },
    )
    assert captured.get("provider") == "claude"
    assert captured.get("claude_api_key") == "sk-ant-test"
    assert str(plan.get("source") or "").startswith("claude")
    assert len(plan.get("spans") or []) >= 2

