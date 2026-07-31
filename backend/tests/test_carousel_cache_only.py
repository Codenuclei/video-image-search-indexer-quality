import pytest

from app.routers import media
from app.routers.carousel_script import _attach_layout_panels, _frame_preview_url, _layout_carousels


@pytest.mark.asyncio
async def test_cache_only_frame_miss_never_extracts(tmp_path, monkeypatch):
    class Settings:
        thumbnail_dir = str(tmp_path)

    async def fail_extract(*args, **kwargs):
        raise AssertionError("cache-only frame path must not extract")

    monkeypatch.setattr(media, "get_settings", lambda: Settings())
    monkeypatch.setattr(media, "_extract_frame_on_demand", fail_extract)

    with pytest.raises(media.HTTPException) as exc:
        await media.get_video_frame(
            "drive-id",
            ts=12.5,
            cache_only=True,
            session=None,
        )

    assert exc.value.status_code == 404


def test_carousel_preview_urls_are_cache_only():
    assert "cache_only=1" in (_frame_preview_url("drive-id", 1.0, 3.0) or "")


def test_split_layout_has_two_distinct_panels_with_focal_metadata():
    carousels = [{
        "slides": [{
            "drive_file_id": "drive-id",
            "timestamp_sec": 10.0,
            "end_timestamp_sec": 20.0,
            "frame_ts": 15.0,
            "transcript_text": "Is trust earned? Consistent quality makes it durable.",
        }]
    }]
    _attach_layout_panels(carousels)
    split = _layout_carousels(carousels, split=True)[0]["slides"][0]["panels"]
    assert len(split) == 2
    assert split[0]["frame_ts"] != split[1]["frame_ts"]
    assert all(
        {"frame_ts", "preview_url", "caption", "focal_x", "focal_y", "front_face_score"}
        <= set(panel)
        for panel in split
    )
