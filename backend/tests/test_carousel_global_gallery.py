"""Global ready-carousel gallery endpoint — filters, pagination, safe previews."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.routers.carousel_script import (
    CarouselArtifactCopyBody,
    _save_carousel_copy_impl,
    _safe_carousel_preview_url,
    get_carousel_save,
    list_ready_carousels_global,
)


def test_safe_preview_from_carousels_slides():
    assert (
        _safe_carousel_preview_url(
            {
                "carousels": [
                    {
                        "slides": [
                            {"preview_url": "/media/video/vid/frame?ts=1.0&cache_only=1"},
                        ]
                    }
                ]
            }
        )
        == "/media/video/vid/frame?ts=1.0&cache_only=1"
    )


def test_safe_preview_rejects_external_and_malformed():
    assert _safe_carousel_preview_url({"carousels": [{"slides": [{"preview_url": "https://evil.example/x.jpg"}]}]}) is None
    assert _safe_carousel_preview_url(None) is None
    assert _safe_carousel_preview_url("not-a-dict") is None
    assert _safe_carousel_preview_url({"carousels": "broken"}) is None
    assert _safe_carousel_preview_url({"slides": [{"preview_url": 123}]}) is None
    # Nested junk must never raise
    assert (
        _safe_carousel_preview_url(
            {
                "carousels": [None, {"slides": [None, {"panels": [{"preview_url": "/media/ok"}]}]}],
            }
        )
        == "/media/ok"
    )


def test_safe_preview_from_layouts_and_top_level_slides():
    assert (
        _safe_carousel_preview_url(
            {
                "layouts": {
                    "single_1": {
                        "carousels": [
                            {"slides": [{"preview_url": "/search/carousel/x"}]},
                        ]
                    }
                }
            }
        )
        == "/search/carousel/x"
    )
    assert (
        _safe_carousel_preview_url({"slides": [{"preview_url": "/media/video/a/frame?ts=2"}]})
        == "/media/video/a/frame?ts=2"
    )


def _save_row(
    *,
    id: int,
    drive_file_id: str,
    created_at: datetime,
    label: str = "Save",
    payload: dict | None = None,
    kind: str = "carousel",
    status: str = "ready",
    copy_version: int = 1,
    layout_mode: str = "single_1",
    source: str = "generation",
):
    return SimpleNamespace(
        id=id,
        drive_file_id=drive_file_id,
        kind=kind,
        status=status,
        label=label,
        created_at=created_at,
        source=source,
        layout_mode=layout_mode,
        copy_version=copy_version,
        payload=payload if payload is not None else {},
    )


@pytest.mark.asyncio
async def test_list_ready_carousels_filters_and_lightweight():
    t1 = datetime(2026, 3, 2, tzinfo=timezone.utc)
    t2 = datetime(2026, 3, 1, tzinfo=timezone.utc)
    ready = _save_row(
        id=10,
        drive_file_id="vid-a",
        created_at=t1,
        label="Ready A",
        payload={
            "carousels": [
                {
                    "slides": [
                        {"preview_url": "/media/video/vid-a/frame?ts=1"},
                        {"preview_url": "https://cdn.example/skip.jpg"},
                    ]
                }
            ],
            "huge": "x" * 1000,
        },
        copy_version=3,
    )
    older = _save_row(
        id=9,
        drive_file_id="vid-b",
        created_at=t2,
        label="Ready B",
        payload={"slides": [{"preview_url": "/media/video/vid-b/frame?ts=2"}]},
        copy_version=1,
    )
    # Malformed payload must still list
    broken = _save_row(
        id=8,
        drive_file_id="vid-c",
        created_at=datetime(2026, 2, 28, tzinfo=timezone.utc),
        label="Broken",
        payload=None,  # type: ignore[arg-type]
    )

    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=SimpleNamespace(
            all=lambda: [
                (ready, "Talk A.mp4", "/Drive/Talk A.mp4"),
                (older, "Talk B.mp4", "/Drive/Talk B.mp4"),
                (broken, None, None),
            ]
        )
    )

    res = await list_ready_carousels_global(limit=10, offset=0, session=session)

    assert res["has_more"] is False
    assert res["limit"] == 10
    assert res["offset"] == 0
    assert len(res["items"]) == 3
    first = res["items"][0]
    assert first["id"] == 10
    assert first["video_name"] == "Talk A.mp4"
    assert first["copy_version"] == 3
    assert first["preview_url"] == "/media/video/vid-a/frame?ts=1"
    assert first["slide_count"] == 2
    assert "payload" not in first
    assert "huge" not in first
    assert res["items"][2]["preview_url"] is None
    assert res["items"][2]["video_name"] is None


@pytest.mark.asyncio
async def test_list_ready_carousels_pagination_has_more():
    rows = []
    for i in range(3):
        rows.append(
            (
                _save_row(
                    id=100 - i,
                    drive_file_id=f"v{i}",
                    created_at=datetime(2026, 1, 10 - i, tzinfo=timezone.utc),
                    label=f"L{i}",
                    payload={},
                ),
                f"V{i}.mp4",
                f"/V{i}",
            )
        )
    session = AsyncMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(all=lambda: rows))

    res = await list_ready_carousels_global(limit=2, offset=0, session=session)
    assert len(res["items"]) == 2
    assert res["has_more"] is True
    assert [i["id"] for i in res["items"]] == [100, 99]


@pytest.mark.asyncio
async def test_exact_save_read_rejects_video_mismatch():
    row = _save_row(
        id=12,
        drive_file_id="video-a",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    row.theme_key = ""
    row.model = None
    row.transcript_hash = None
    row.input_hash = None
    row.algorithm_version = "p0"
    session = SimpleNamespace(get=AsyncMock(return_value=row))

    with pytest.raises(HTTPException) as exc:
        await get_carousel_save(12, drive_file_id="video-b", session=session)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_copy_uses_exact_base_and_updates_nested_carousel():
    current = _save_row(
        id=21,
        drive_file_id="video-a",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        copy_version=4,
        payload={
            "carousels": [
                {"id": "first", "slides": [{"caption": "unchanged"}]},
                {"id": "chosen", "slides": [{"caption": "old"}]},
            ],
            "layouts": {
                "split_2": {
                    "carousels": [
                        {"id": "first", "slides": [{"caption": "unchanged"}]},
                        {"id": "chosen", "slides": [{"caption": "old"}]},
                    ]
                }
            },
        },
    )
    current.algorithm_version = "p0"
    current.input_hash = "old"
    scalar = AsyncMock(side_effect=[current, None])
    added: list[object] = []
    session = SimpleNamespace(
        scalar=scalar,
        add=MagicMock(side_effect=added.append),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )
    body = CarouselArtifactCopyBody(
        drive_file_id="video-a",
        save_id=21,
        carousel_id="chosen",
        layout_mode="split_2",
        slides=[{"caption": "edited"}],
    )

    await _save_carousel_copy_impl(body, session=session)

    replacement = added[0]
    assert replacement.copy_version == 5
    assert replacement.payload["carousels"][0]["slides"][0]["caption"] == "unchanged"
    assert replacement.payload["carousels"][1]["slides"][0]["caption"] == "edited"
    assert (
        replacement.payload["layouts"]["split_2"]["carousels"][1]["slides"][0]["caption"]
        == "edited"
    )
