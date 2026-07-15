from app.video.youtube_registry import parse_youtube_video_id, youtube_drive_id


def test_parse_youtube_video_id():
    assert parse_youtube_video_id("rWUWfj_PqmM") == "rWUWfj_PqmM"
    assert parse_youtube_video_id("https://www.youtube.com/watch?v=j2lcnmLGSxQ") == "j2lcnmLGSxQ"
    assert parse_youtube_video_id("https://youtu.be/ICi-rgwvj_o") == "ICi-rgwvj_o"
    assert parse_youtube_video_id("not-a-url") is None


def test_youtube_drive_id():
    assert youtube_drive_id("rWUWfj_PqmM") == "yt:rWUWfj_PqmM"
