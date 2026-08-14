"""English transcript stitch / validate / translate (no network)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.video.english_transcript import (
    EnglishTranscriptError,
    TimedSentence,
    _parse_translation_json,
    stitch_complete_sentences,
    validate_english_sentences,
)


def test_stitch_complete_sentences_merges_fragments_preserves_timestamps():
    cues = [
        (1.0, 2.0, "Hello everyone"),
        (2.0, 3.5, "welcome to the show."),
        (4.0, 5.0, "Today we discuss"),
        (5.0, 7.0, "important topics in depth."),
    ]
    out = stitch_complete_sentences(cues)
    assert len(out) == 2
    assert out[0].start_sec == 1.0
    assert out[0].end_sec == 3.5
    assert out[0].text == "Hello everyone welcome to the show."
    assert out[1].start_sec == 4.0
    assert out[1].end_sec == 7.0
    assert "important topics" in out[1].text


def test_stitch_does_not_alter_words():
    cues = [
        (0.0, 1.0, "The quick brown"),
        (1.0, 2.5, "fox jumps over the lazy dog."),
    ]
    out = stitch_complete_sentences(cues)
    assert len(out) == 1
    assert out[0].text == "The quick brown fox jumps over the lazy dog."


def test_validate_english_sentences_rejects_non_english():
    with pytest.raises(EnglishTranscriptError, match="reliable English"):
        validate_english_sentences(
            [
                TimedSentence(
                    start_sec=0.0,
                    end_sec=2.0,
                    text="यह एक पूरा वाक्य है जो हिंदी में लिखा गया है।",
                )
            ]
        )


def test_validate_english_sentences_rejects_incomplete():
    with pytest.raises(EnglishTranscriptError, match="incomplete"):
        validate_english_sentences(
            [TimedSentence(start_sec=0.0, end_sec=1.0, text="and then")]
        )


def test_validate_english_sentences_accepts_complete_english():
    validate_english_sentences(
        [
            TimedSentence(
                start_sec=0.0,
                end_sec=3.0,
                text="This is a complete English sentence about the product.",
            )
        ]
    )


def test_parse_translation_json_object_and_nulls():
    raw = '{"segments": [{"i": 0, "text": "Hello there everyone."}, {"i": 1, "text": null}]}'
    by_i = _parse_translation_json(raw, expected=2)
    assert by_i[0] == "Hello there everyone."
    assert by_i[1] is None


def test_parse_translation_json_rejects_garbage():
    with pytest.raises(EnglishTranscriptError, match="couldn’t translate|try again"):
        _parse_translation_json("not json at all", expected=1)


@pytest.mark.asyncio
async def test_translate_sentences_fails_closed_on_missing_item():
    from app.video.english_transcript import _translate_sentences_llm

    sentences = [
        TimedSentence(start_sec=0.0, end_sec=2.0, text="नमस्ते दोस्तों आज हम बात करेंगे।"),
        TimedSentence(start_sec=2.0, end_sec=4.0, text="यह दूसरी पूरी लाइन है दोस्तों।"),
    ]
    fake_pack = {
        "provider": "gemini",
        "api_key": "g",
        "model": "gemini-2.5-flash",
        "claude_api_key": "",
        "claude_model": "",
        "openrouter_api_key": "",
        "openrouter_model": "",
        "openrouter_base_url": "",
    }
    # Only first segment translated → must raise, not invent the second.
    llm_raw = '{"segments": [{"i": 0, "text": "Hello friends, today we will talk."}]}'

    with (
        patch(
            "app.llm.carousel_llm.resolve_carousel_llm",
            return_value=fake_pack,
        ),
        patch(
            "app.search.carousel_pipeline._llm_has_any_key",
            return_value=True,
        ),
        patch(
            "app.search.carousel_pipeline._llm_complete_json",
            new=AsyncMock(return_value=(llm_raw, "gemini")),
        ),
    ):
        with pytest.raises(EnglishTranscriptError, match="couldn’t translate|try again"):
            await _translate_sentences_llm(sentences, provider="gemini")


@pytest.mark.asyncio
async def test_translate_sentences_preserves_timestamps():
    from app.video.english_transcript import _translate_sentences_llm

    sentences = [
        TimedSentence(start_sec=10.5, end_sec=14.0, text="यह एक पूरा वाक्य है दोस्तों आज।"),
    ]
    fake_pack = {
        "provider": "gemini",
        "api_key": "g",
        "model": "gemini-2.5-flash",
        "claude_api_key": "",
        "claude_model": "",
        "openrouter_api_key": "",
        "openrouter_model": "",
        "openrouter_base_url": "",
    }
    llm_raw = (
        '{"segments": [{"i": 0, "text": '
        '"This is a complete sentence friends today."}]}'
    )

    with (
        patch(
            "app.llm.carousel_llm.resolve_carousel_llm",
            return_value=fake_pack,
        ),
        patch(
            "app.search.carousel_pipeline._llm_has_any_key",
            return_value=True,
        ),
        patch(
            "app.search.carousel_pipeline._llm_complete_json",
            new=AsyncMock(return_value=(llm_raw, "gemini")),
        ),
    ):
        out, provider = await _translate_sentences_llm(sentences, provider="gemini")

    assert provider == "gemini"
    assert len(out) == 1
    assert out[0].start_sec == 10.5
    assert out[0].end_sec == 14.0
    assert out[0].text.startswith("This is a complete")
