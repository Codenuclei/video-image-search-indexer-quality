from app.pipelines.common import is_drive_media_candidate, is_indexable_mime


def test_indexable_videos_only(monkeypatch):
    monkeypatch.setenv("VIDEO_INDEXING_ENABLED", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    assert is_indexable_mime("video/mp4")
    assert not is_indexable_mime("application/pdf")
    assert not is_indexable_mime("image/jpeg")
    assert not is_indexable_mime("image/png")
    get_settings.cache_clear()


def test_skip_video_when_indexing_disabled(monkeypatch):
    monkeypatch.setenv("VIDEO_INDEXING_ENABLED", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    assert not is_indexable_mime("video/mp4")
    get_settings.cache_clear()


def test_video_indexable_when_enabled(monkeypatch):
    monkeypatch.setenv("VIDEO_INDEXING_ENABLED", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    assert is_indexable_mime("video/mp4")
    get_settings.cache_clear()


def test_skip_spreadsheet():
    assert not is_indexable_mime("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def test_drive_sync_accepts_videos_not_images():
    assert is_drive_media_candidate("video/mp4", "talk.mp4")
    assert is_drive_media_candidate("video/quicktime", "talk.mov")
    assert not is_drive_media_candidate("image/jpeg", "shot.jpg")
    assert not is_drive_media_candidate("image/heif", "shot.heic")
    assert not is_drive_media_candidate("application/pdf", "deck.pdf")
