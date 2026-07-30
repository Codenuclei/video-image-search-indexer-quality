"""Tests for English preference on carousel hooks/topics."""

from app.search.english_text import (
    cues_need_english,
    english_text_for_window,
    is_english_text,
    needs_english,
    prefer_english_cues,
)
from app.search.carousel_pipeline import extract_hooks_and_topics, _swap_hooks_with_english_cues
from app.routers.carousel_script import (
    _english_caption_track_usable,
    _select_carousel_cue_corpus,
)


def test_devanagari_needs_english():
    hindi = "तो हम दिल्ली आ गए और वहाँ पर बहुत मज़ा आया दोस्तों"
    assert needs_english(hindi)
    assert not is_english_text(hindi)


def test_english_passes():
    en = "So we came to Delhi and had a great time with friends there"
    assert is_english_text(en)
    assert not needs_english(en)


def test_cues_need_english_majority():
    cues = [
        (0.0, 3.0, "तो हम दिल्ली आ गए"),
        (3.0, 6.0, "वहाँ बहुत अच्छा लगा"),
        (6.0, 9.0, "Next we talk about leadership"),
    ]
    assert cues_need_english(cues)


def test_prefer_english_cues_keeps_english_when_mixed():
    cues = [
        (0.0, 3.0, "तो हम दिल्ली आ गए और बात करते हैं"),
        (3.0, 6.0, "We arrived in Delhi and started talking about work"),
        (6.0, 9.0, "Leadership is about owning outcomes every day"),
        (9.0, 12.0, "यह एक और हिंदी लाइन है दोस्तों"),
    ]
    preferred = prefer_english_cues(cues)
    assert len(preferred) == 2
    assert all(is_english_text(t) for _, _, t in preferred)


def test_english_text_for_window_aligns_timestamps():
    english = [
        (10.0, 14.0, "We came to Delhi"),
        (14.0, 18.0, "and the weather was perfect"),
        (40.0, 45.0, "Unrelated later segment"),
    ]
    text = english_text_for_window(english, start_sec=10.0, end_sec=17.0)
    assert text is not None
    assert "Delhi" in text
    assert "weather" in text


def test_extract_hooks_uses_english_cues_when_provided():
    hindi_cues = [
        (0.0, 4.0, "तो दोस्तों आज हम बात करेंगे लीडरशिप के बारे में बहुत विस्तार से"),
        (4.0, 8.0, "पहले हमें समझना होगा कि निर्णय कैसे लिए जाते हैं दबाव में"),
        (8.0, 12.0, "फिर हम टीम वर्क और फीडबैक लूप्स पर आएंगे विस्तार से बात"),
        (12.0, 16.0, "अंत में एक करियर टेकअवे जो आप कल से इस्तेमाल कर सकते हो"),
    ]
    english_cues = [
        (0.0, 4.0, "So friends today we will talk about leadership in depth"),
        (4.0, 8.0, "First we need to understand how decisions are made under pressure"),
        (8.0, 12.0, "Then we will cover teamwork and feedback loops in detail"),
        (12.0, 16.0, "Finally a career takeaway you can use starting tomorrow"),
    ]
    result = extract_hooks_and_topics(
        hindi_cues,
        start_sec=0.0,
        end_sec=16.0,
        theme_title="Leadership under pressure",
        theme_summary="How teams decide and grow.",
        english_cues=english_cues,
    )
    assert result["hooks"]
    for hook in result["hooks"]:
        assert is_english_text(hook["text"]), hook["text"]
        assert hook.get("english_source") == "caption_track"


def test_sparse_english_track_rejected():
    indexed = [(float(i), float(i) + 2, f"हिंदी लाइन नंबर {i} और बात") for i in range(40)]
    sparse = [(126.0, 129.0, "bakbuke.com")]
    assert not _english_caption_track_usable(sparse, indexed)
    corpus, used_en = _select_carousel_cue_corpus(indexed, sparse)
    assert used_en is False
    assert len(corpus) == len(indexed)


def test_usable_english_track_selected():
    indexed = [(float(i), float(i) + 2, f"हिंदी लाइन नंबर {i} और बात") for i in range(40)]
    english = [
        (float(i), float(i) + 2, f"Here is a clear English caption line number {i}")
        for i in range(20)
    ]
    assert _english_caption_track_usable(english, indexed)
    corpus, used_en = _select_carousel_cue_corpus(indexed, english)
    assert used_en is True
    assert len(corpus) == 20


def test_swap_hooks_with_english_cues():
    hooks = [
        {
            "id": "hook_1",
            "text": "तो हम दिल्ली आ गए और बहुत खुश हुए",
            "start_sec": 10.0,
            "end_sec": 14.0,
            "verbatim": True,
        }
    ]
    english = [
        (9.5, 14.5, "So we came to Delhi and were very happy about it"),
    ]
    swapped = _swap_hooks_with_english_cues(hooks, english)
    assert is_english_text(swapped[0]["text"])
    assert swapped[0].get("original_text")
    assert swapped[0]["english_source"] == "caption_track"
