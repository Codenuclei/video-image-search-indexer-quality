"""Per-video event-photo folder links and face-matched candidate ordering."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from PIL import Image

from app.config import Settings
from app.db.models import (
    CarouselEventPhotoFolder,
    DriveFile,
    DriveFileStatus,
    Face,
    FaceEmbedding,
    IndexedFolder,
    Media,
    MediaType,
)
from tests.conftest import requires_postgres


def _unit_vector(index: int = 0) -> list[float]:
    values = [0.0] * 512
    values[index] = 1.0
    return values


def test_event_photo_derivative_is_cached_4x5(tmp_path):
    from app.search.carousel_event_photos import ensure_event_photo_variant

    settings = Settings(
        thumbnail_dir=str(tmp_path / "thumbs"),
        media_cache_dir=str(tmp_path / "media"),
    )
    source = tmp_path / "media" / "photo.jpg"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (1600, 900), (20, 40, 60)).save(source, "JPEG")
    photo = DriveFile(
        id="photo-1",
        name="photo.jpg",
        mime_type="image/jpeg",
        path="/event/photo.jpg",
        status=DriveFileStatus.PROCESSED,
        cache_rel_path="photo.jpg",
        root_folder_id="event-root",
    )

    first = ensure_event_photo_variant(photo, settings)
    second = ensure_event_photo_variant(photo, settings)

    assert first is not None and first.is_file()
    assert second == first
    with Image.open(first) as image:
        assert image.width / image.height == pytest.approx(4 / 5, rel=0.01)
        assert image.width <= 1080
        assert image.height <= 1350


@pytest.mark.asyncio
async def test_event_photos_precede_video_fallback_and_stay_text_first(monkeypatch):
    from app.search import carousel_event_photos as event_photos

    async def fake_match(*_args, **_kwargs):
        return [
            {
                "asset_type": "event_photo",
                "photo_drive_file_id": "photo-1",
                "preview_url": "/media/event-photo/photo-1",
                "selected": False,
                "recommended": True,
            }
        ]

    monkeypatch.setattr(event_photos, "match_event_photos", fake_match)
    slide = {
        "identity_association": {"mode": "speaker", "identity_id": "id_0"},
        "frame_candidate_items": [
            {
                "asset_type": "video_frame",
                "frame_ts": 4.0,
                "preview_url": "/media/video/v/frame?ts=4.000&cache_only=1",
                "selected": False,
                "recommended": True,
            }
        ],
    }
    catalog = {
        "identities": [{"id": "id_0", "centroid": _unit_vector()}],
    }

    out, summary = await event_photos.apply_event_photo_matches(
        object(),  # fake matcher ignores the session
        [slide],
        folder_id="event-root",
        catalog=catalog,
    )

    items = out[0]["frame_candidate_items"]
    assert items[0]["asset_type"] == "event_photo"
    assert items[1]["asset_type"] == "video_frame"
    assert items[1]["recommended"] is False
    assert out[0]["preview_url"] is None
    assert out[0]["frame_ts"] is None
    assert out[0]["layout_role"] == "cover"
    assert not any(item["selected"] for item in items)
    assert summary["matched_slides"] == 1


@pytest.mark.asyncio
async def test_body_slide_gets_two_distinct_event_photo_panels(monkeypatch):
    from app.search import carousel_event_photos as event_photos

    async def fake_match(*_args, **_kwargs):
        return [
            {
                "asset_type": "event_photo",
                "photo_drive_file_id": "photo-1",
                "preview_url": "/media/event-photo/photo-1",
                "selected": False,
                "recommended": True,
            },
            {
                "asset_type": "event_photo",
                "photo_drive_file_id": "photo-2",
                "preview_url": "/media/event-photo/photo-2",
                "selected": False,
                "recommended": False,
            },
        ]

    monkeypatch.setattr(event_photos, "match_event_photos", fake_match)
    slides = [
        {
            "identity_association": {"mode": "speaker", "identity_id": "id_0"},
            "frame_candidate_items": [],
        },
        {
            "identity_association": {"mode": "speaker", "identity_id": "id_0"},
            "frame_candidate_items": [],
        },
    ]
    out, _ = await event_photos.apply_event_photo_matches(
        object(),
        slides,
        folder_id="event-root",
        catalog={"identities": [{"id": "id_0", "centroid": _unit_vector()}]},
    )
    assert out[1]["layout_role"] == "body"
    assert [panel["photo_drive_file_id"] for panel in out[1]["panels"]] == [
        "photo-1",
        "photo-2",
    ]
    assert out[1]["preview_url"] is None


@pytest.mark.asyncio
async def test_explicit_picker_selection_is_untouched(monkeypatch):
    from app.search import carousel_event_photos as event_photos

    async def should_not_match(*_args, **_kwargs):
        raise AssertionError("manual selection must bypass matching")

    monkeypatch.setattr(event_photos, "match_event_photos", should_not_match)
    selected = {
        "preview_url": "/media/event-photo/chosen",
        "frame_ts": None,
        "frame_source": "event_photo",
        "frame_candidate_items": [
            {
                "asset_type": "event_photo",
                "photo_drive_file_id": "chosen",
                "preview_url": "/media/event-photo/chosen",
                "selected": True,
            }
        ],
        "identity_association": {"mode": "speaker", "identity_id": "id_0"},
    }
    out, _ = await event_photos.apply_event_photo_matches(
        object(),
        [selected],
        folder_id="event-root",
        catalog={"identities": [{"id": "id_0", "centroid": _unit_vector()}]},
    )
    assert out[0]["preview_url"] == selected["preview_url"]
    assert out[0]["frame_candidate_items"] == selected["frame_candidate_items"]
    assert out[0]["layout_role"] == "cover"


@requires_postgres
@pytest.mark.asyncio
async def test_matcher_scopes_faces_to_linked_root(db_session, tmp_path):
    from app.search.carousel_event_photos import match_event_photos

    settings = Settings(
        thumbnail_dir=str(tmp_path / "thumbs"),
        media_cache_dir=str(tmp_path / "media"),
    )
    for folder_id in ("event-root", "other-root"):
        db_session.add(
            IndexedFolder(
                id=folder_id,
                name=folder_id,
                drive_url=f"https://drive.google.com/drive/folders/{folder_id}",
            )
        )
    for file_id, root_id, embedding in (
        ("inside", "event-root", _unit_vector(0)),
        ("outside", "other-root", _unit_vector(0)),
        ("wrong-person", "event-root", _unit_vector(1)),
    ):
        source = tmp_path / "media" / f"{file_id}.jpg"
        source.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (800, 1000), (50, 60, 70)).save(source, "JPEG")
        drive_file = DriveFile(
            id=file_id,
            name=f"{file_id}.jpg",
            mime_type="image/jpeg",
            path=f"/{root_id}/{file_id}.jpg",
            status=DriveFileStatus.PROCESSED,
            cache_rel_path=f"{file_id}.jpg",
            root_folder_id=root_id,
        )
        media = Media(drive_file=drive_file, type=MediaType.IMAGE)
        face = Face(
            media=media,
            bbox_x=100,
            bbox_y=100,
            bbox_width=200,
            bbox_height=250,
            detection_confidence=0.95,
        )
        face.embedding = FaceEmbedding(embedding=embedding)
        db_session.add(drive_file)
    await db_session.commit()

    matches = await match_event_photos(
        db_session,
        folder_id="event-root",
        query_embedding=_unit_vector(0),
        settings=settings,
    )

    assert [item["photo_drive_file_id"] for item in matches] == ["inside"]


@requires_postgres
@pytest.mark.asyncio
async def test_link_api_is_per_video_and_does_not_select_drive_folder(db_session, monkeypatch):
    from fastapi import BackgroundTasks

    from app.db.models import DriveUser
    from app.routers.carousel_script import (
        CarouselEventPhotoFolderRequest,
        carousel_link_event_photo_folder,
    )

    async def fake_prepare(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "app.routers.carousel_script._prepare_event_photo_folder",
        fake_prepare,
    )

    now = datetime.now(timezone.utc)
    user = DriveUser(
        id="user-1",
        email="user@example.com",
        access_token="token",
        selected_folder_id="primary-root",
        selected_folder_name="Primary",
        token_expiry=now,
    )
    folder = IndexedFolder(
        id="event-root",
        name="Event",
        drive_url="https://drive.google.com/drive/folders/event-root",
    )
    video = DriveFile(
        id="video-1",
        name="video.mp4",
        mime_type="video/mp4",
        path="/video.mp4",
        status=DriveFileStatus.PROCESSED,
    )
    db_session.add_all([user, folder, video])
    await db_session.commit()

    background = BackgroundTasks()
    status = await carousel_link_event_photo_folder(
        "video-1",
        CarouselEventPhotoFolderRequest(folder_id="event-root"),
        background,
        db_session,
    )
    await db_session.refresh(user)

    assert status["linked"] is True
    assert status["folder"]["id"] == "event-root"
    assert status["indexing_state"] == "preparing"
    assert user.selected_folder_id == "primary-root"
    link = await db_session.get(CarouselEventPhotoFolder, "video-1")
    assert link is not None and link.folder_id == "event-root"
    assert link.indexing_state == "preparing"
