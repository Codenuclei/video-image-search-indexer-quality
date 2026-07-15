from app.qdrant.image_captions import caption_word_count, is_valid_caption


def test_caption_word_count():
    assert caption_word_count("students holding a large cheque") >= 4
    assert caption_word_count("photo") == 1


def test_valid_caption_requires_min_words():
    assert is_valid_caption("A group of students standing outdoors holding a ceremonial cheque", min_words=4)
    assert not is_valid_caption("photo", min_words=4)
    assert not is_valid_caption("", min_words=4)
    assert not is_valid_caption("   image  ", min_words=4)


def test_valid_caption_accepts_short_when_threshold_low():
    assert is_valid_caption("students outdoors", min_words=2)
