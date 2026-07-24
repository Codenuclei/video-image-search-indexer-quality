"""Unit tests for transcript topic fallback / parsing helpers."""

from app.search.transcript_topics import (
    compact_transcript,
    fallback_topics_from_cues,
    _parse_topics_json,
)


def test_fallback_topics_has_timestamps_and_explanations():
    cues = [
        (0.0, 5.0, "Welcome to the leadership workshop today."),
        (5.0, 12.0, "We will cover decision making under pressure."),
        (12.0, 20.0, "Next we discuss feedback loops in teams."),
        (20.0, 28.0, "Finally a career takeaway you can use tomorrow."),
        (28.0, 35.0, "Questions from the audience are welcome."),
        (35.0, 42.0, "Closing remarks and resources on the site."),
        (42.0, 50.0, "Thanks for joining this session everyone."),
        (50.0, 58.0, "See you next week for collaboration skills."),
    ]
    topics = fallback_topics_from_cues(cues, max_topics=4)
    assert len(topics) >= 2
    for topic in topics:
        assert topic["title"]
        assert "start_sec" in topic
        assert topic["explanation"]
        assert isinstance(topic.get("subtopics"), list)


def test_compact_transcript_includes_timestamps():
    text = compact_transcript([(10.0, 15.0, "hello world"), (20.0, None, "next line")])
    assert "[0:10–0:15] hello world" in text
    assert "[0:20] next line" in text


def test_parse_topics_json_normalizes_shape():
    raw = """
    [
      {
        "title": "Opening",
        "start_sec": 0,
        "end_sec": 30,
        "explanation": "Sets the stage.",
        "subtopics": [
          {"title": "Hook", "start_sec": 0, "end_sec": 10, "explanation": "Grabs attention."}
        ]
      }
    ]
    """
    topics = _parse_topics_json(raw)
    assert len(topics) == 1
    assert topics[0]["title"] == "Opening"
    assert topics[0]["start_sec"] == 0.0
    assert topics[0]["explanation"] == "Sets the stage."
    assert len(topics[0]["subtopics"]) == 1
    assert topics[0]["subtopics"][0]["title"] == "Hook"
