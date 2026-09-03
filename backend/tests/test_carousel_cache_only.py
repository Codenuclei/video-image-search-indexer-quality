import pytest
from pathlib import Path

from app.routers import media
from app.routers.carousel_script import (
    _attach_layout_panels,
    _frame_preview_url,
    _layout_carousels,
    _pick_split_frame_timestamps,
)


@pytest.mark.asyncio
async def test_cache_only_hdr_miss_does_not_invent(tmp_path, monkeypatch):
    class Settings:
        thumbnail_dir = str(tmp_path)

    frames = tmp_path / "video" / "drive-id"
    frames.mkdir(parents=True)
    (frames / "12.500.jpg").write_bytes(b"source-only")

    async def fail_extract(*args, **kwargs):
        raise AssertionError("cache-only HDR must not extract")

    monkeypatch.setattr(media, "get_settings", lambda: Settings())
    monkeypatch.setattr(media, "_extract_frame_on_demand", fail_extract)

    with pytest.raises(media.HTTPException) as exc:
        await media.get_video_frame(
            "drive-id",
            ts=12.5,
            cache_only=True,
            variant="hdr",
            session=None,
        )

    assert exc.value.status_code == 404
    assert "HDR" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_cache_only_hdr_serves_prebuilt(tmp_path, monkeypatch):
    class Settings:
        thumbnail_dir = str(tmp_path)

    frames = tmp_path / "video" / "drive-id"
    hdr = frames / "hdr"
    frames.mkdir(parents=True)
    hdr.mkdir(parents=True)
    (frames / "12.500.jpg").write_bytes(b"source")
    (hdr / "12.500.jpg").write_bytes(b"hdr-bytes")

    monkeypatch.setattr(media, "get_settings", lambda: Settings())

    response = await media.get_video_frame(
        "drive-id",
        ts=12.5,
        cache_only=True,
        variant="hdr",
        session=None,
    )
    assert Path(response.path).name == "12.500.jpg"
    assert "hdr" in str(response.path)


@pytest.mark.asyncio
async def test_cache_only_does_not_serve_nearest_neighbour(tmp_path, monkeypatch):
    """Split panels use distinct ts URLs; nearest±5s must not collapse them."""
    class Settings:
        thumbnail_dir = str(tmp_path)

    frames = tmp_path / "video" / "drive-id"
    frames.mkdir(parents=True)
    (frames / "15.000.jpg").write_bytes(b"left-panel-bytes")

    async def fail_extract(*args, **kwargs):
        raise AssertionError("cache-only must not extract")

    monkeypatch.setattr(media, "get_settings", lambda: Settings())
    monkeypatch.setattr(media, "_extract_frame_on_demand", fail_extract)

    with pytest.raises(media.HTTPException) as exc:
        await media.get_video_frame(
            "drive-id",
            ts=20.0,
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
    assert split[0]["preview_url"] != split[1]["preview_url"]
    assert all(
        {"frame_ts", "preview_url", "caption", "focal_x", "focal_y", "front_face_score"}
        <= set(panel)
        for panel in split
    )


def test_pick_split_timestamps_prefers_far_cached_frames(tmp_path):
    frames = tmp_path / "video" / "drive-id"
    frames.mkdir(parents=True)
    (frames / "10.000.jpg").write_bytes(b"a")
    (frames / "15.000.jpg").write_bytes(b"b")
    (frames / "20.000.jpg").write_bytes(b"c")

    left, right = _pick_split_frame_timestamps(
        selected_ts=15.0,
        start=10.0,
        end_f=20.0,
        drive_file_id="drive-id",
        thumbnail_dir=str(tmp_path),
    )
    assert left != right
    assert abs(right - left) >= 0.45
    assert {left, right} <= {10.0, 15.0, 20.0}


def test_split_one_sentence_caption_is_not_duplicated():
    carousels = [{
        "slides": [{
            "drive_file_id": "drive-id",
            "timestamp_sec": 10.0,
            "end_timestamp_sec": 20.0,
            "frame_ts": 15.0,
            "transcript_text": (
                "Based on the name, you can figure out probably involves dots."
            ),
        }]
    }]
    _attach_layout_panels(carousels)
    split = _layout_carousels(carousels, split=True)[0]["slides"][0]["panels"]
    captions = [(p.get("caption") or "").strip() for p in split]
    assert captions[0] != captions[1] or not captions[1]
    assert sum(1 for c in captions if c) == 1
