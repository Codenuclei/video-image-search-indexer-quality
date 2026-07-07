from app.video.vtt import parse_vtt


def test_parse_vtt_basic():
    content = """WEBVTT

00:00:01.000 --> 00:00:04.000
Hello party people

00:00:05.500 --> 00:00:08.000
Jason waves at the camera
"""
    cues = parse_vtt(content)
    assert len(cues) == 2
    assert cues[0].start_sec == 1.0
    assert "party" in cues[0].text.lower()
    assert cues[1].end_sec == 8.0
