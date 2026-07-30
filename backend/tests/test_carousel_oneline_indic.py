"""Indic / Hindi one-liner validators for exact carousel cuts."""

from app.routers.carousel_script import (
    _line_complete_enough,
    _line_starts_clean,
    _trim_to_oneline,
)


def test_devanagari_starts_clean():
    assert _line_starts_clean("यह एक बड़ा प्लेटफॉर्म है।")
    assert _line_starts_clean("Physics Wallah पर 6 मिलियन स्टूडेंट्स हैं।")
    assert not _line_starts_clean("and then we continue.")


def test_danda_counts_as_sentence_end():
    line = "यह एक बड़ा प्लेटफॉर्म है।"
    assert _line_complete_enough(line)
    assert _line_complete_enough("YouTube का फेवरेट लर्निंग प्लेटफॉर्म।")


def test_indic_without_terminator_ok():
    """Physics Wallah-style romanized Devanagari cues often omit . / ।"""
    assert _line_starts_clean("8000 प्लस आवर्स ऑफ टीचिंग कंटेंट ऑनलाइन")
    assert _line_complete_enough("8000 प्लस आवर्स ऑफ टीचिंग कंटेंट ऑनलाइन")
    assert _line_complete_enough("अ कम्युनिटी ऑफ 6 मिलियन प्लस स्टूडेंट्स")
    assert _line_starts_clean("youtube's फेवरेट लर्निंग प्लेटफार्म")


def test_english_validators_unchanged():
    assert _line_starts_clean("Welcome to Physics Wallah.")
    assert _line_complete_enough("Welcome to Physics Wallah.")
    assert not _line_complete_enough("Welcome to Physics Wallah")  # no terminator
    assert not _line_starts_clean("youtube is great.")
    assert not _line_complete_enough("and then we keep talking about it more")


def test_trim_respects_danda():
    t = _trim_to_oneline("यह एक बड़ा प्लेटफॉर्म है। अगला वाक्य यहाँ है।")
    assert "।" in t
    assert "अगला" not in t
