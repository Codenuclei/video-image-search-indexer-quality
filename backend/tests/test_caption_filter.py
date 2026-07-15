from app.gemini.caption_filter import _parse_booleans


def test_parse_booleans_array():
    assert _parse_booleans("[true, false, true]", 3) == [True, False, True]


def test_parse_booleans_rejects_wrong_length():
    assert _parse_booleans("[true, false]", 3) is None


def test_parse_booleans_finds_array_in_noise():
    assert _parse_booleans('note {"x":1} [true, false, true] done', 3) == [True, False, True]

