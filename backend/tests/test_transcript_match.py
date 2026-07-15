from app.search.transcript_match import score_transcript_match
from app.video.youtube_transcript import _json3_to_cues, youtube_id_from_filename


def test_youtube_id_from_filename():
    assert youtube_id_from_filename("The New Way [rWUWfj_PqmM].webm") == "rWUWfj_PqmM"
    assert youtube_id_from_filename("no id here.webm") is None


def test_json3_to_cues():
    data = {
        "events": [
            {"tStartMs": 1000, "dDurationMs": 3000, "segs": [{"utf8": "Hello "}, {"utf8": "world"}]},
            {"tStartMs": 5000, "dDurationMs": 2000, "segs": [{"utf8": "startup"}]},
        ]
    }
    cues = _json3_to_cues(data)
    assert len(cues) == 2
    assert cues[0].start_sec == 1.0
    assert "Hello world" in cues[0].text
    assert cues[1].text == "startup"


def test_score_transcript_match_phrase():
    scored = score_transcript_match("He made millions selling game clothes", "game clothes")
    assert scored is not None
    score, kind = scored
    assert score == 1.0
    assert kind == "phrase"


def test_score_transcript_match_all_words():
    scored = score_transcript_match("first customers are hard to find", "first customers")
    assert scored is not None
    score, kind = scored
    assert score >= 0.9
    assert kind == "all_words"


def test_score_transcript_match_no_hit():
    assert score_transcript_match("unrelated topic here", "footwear factory") is None
