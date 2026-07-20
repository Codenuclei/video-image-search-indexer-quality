from types import SimpleNamespace

from app.config import Settings
from app.video.youtube_cache import _suffix_for_drive_file, video_cache_path
from app.video.youtube_registry import parse_youtube_video_id, youtube_drive_id


def test_parse_youtube_video_id():
    assert parse_youtube_video_id("rWUWfj_PqmM") == "rWUWfj_PqmM"
    assert parse_youtube_video_id("https://www.youtube.com/watch?v=j2lcnmLGSxQ") == "j2lcnmLGSxQ"
    assert parse_youtube_video_id("https://youtu.be/ICi-rgwvj_o") == "ICi-rgwvj_o"
    assert parse_youtube_video_id("not-a-url") is None


def test_youtube_drive_id():
    assert youtube_drive_id("rWUWfj_PqmM") == "yt:rWUWfj_PqmM"


def test_youtube_cache_path_ignores_periods_in_title():
    settings = Settings(video_cache_dir="./data/videos")
    drive_file = SimpleNamespace(
        id="yt:1kf9JSxA5J0",
        name="A Day at Physics Wallah's Office - Part-1 | Ep. #5 ft. Guest @PhysicsWallah [1kf9JSxA5J0]",
        mime_type="video/youtube",
        source="youtube",
    )
    assert _suffix_for_drive_file(drive_file) == ".webm"
    path = video_cache_path(settings, drive_file)
    assert path.name == "yt:1kf9JSxA5J0.webm"
    assert "Ep." not in path.name
    assert "@PhysicsWallah" not in path.name
