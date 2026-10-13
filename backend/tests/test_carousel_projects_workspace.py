"""Draft upsert, projects aggregation, and finished-copy clears draft."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.routers.carousel_script import (
    CarouselArtifactCopyBody,
    CarouselDraftUpsertBody,
    DRAFT_PAYLOAD_MAX_BYTES,
    SAVE_KIND_CAROUSEL,
    SAVE_KIND_DRAFT,
    _clear_draft_for_video,
    _draft_phase,
    _save_carousel_copy_impl,
    delete_carousel_draft,
    get_carousel_draft,
    list_carousel_projects,
    upsert_carousel_draft,
)


def _save_row(
    *,
    id: int,
    drive_file_id: str,
    kind: str,
    created_at: datetime | None = None,
    label: str = "Draft",
    payload: dict | None = None,
    status: str = "ready",
    copy_version: int = 1,
    layout_mode: str = "single_1",
    source: str = "draft_autosave",
    theme_key: str = "draft",
):
    return SimpleNamespace(
        id=id,
        drive_file_id=drive_file_id,
        kind=kind,
        status=status,
        label=label,
        created_at=created_at or datetime(2026, 3, 1, tzinfo=timezone.utc),
        source=source,
        layout_mode=layout_mode,
        copy_version=copy_version,
        theme_key=theme_key,
        algorithm_version="p0",
        payload=payload if payload is not None else {},
    )


def test_draft_phase_safe():
    assert _draft_phase({"phase": 3}) == 3
    assert _draft_phase({"phase": "5"}) == 5
    assert _draft_phase({"phase": 0}) is None
    assert _draft_phase({"phase": True}) is None
    assert _draft_phase(None) is None
    assert _draft_phase("broken") is None


@pytest.mark.asyncio
async def test_upsert_draft_creates_then_updates_same_row():
    added: list[object] = []
    existing = _save_row(
        id=11,
        drive_file_id="vid-1",
        kind=SAVE_KIND_DRAFT,
        payload={"phase": 1, "updated_at": "2026-01-01T00:00:00+00:00"},
    )

    # First call: no existing rows → insert
    session_create = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
        ),
        add=MagicMock(side_effect=added.append),
        commit=AsyncMock(),
        refresh=AsyncMock(
            side_effect=lambda row: setattr(row, "id", 42)
            or setattr(row, "created_at", datetime(2026, 3, 2, tzinfo=timezone.utc))
        ),
        delete=AsyncMock(),
    )
    body = CarouselDraftUpsertBody(
        drive_file_id="vid-1",
        phase=2,
        selected_hooks=["h1"],
        client_updated_at="2026-03-02T10:00:00Z",
    )
    res = await upsert_carousel_draft(body, session=session_create)
    assert res["id"] == 42
    assert res["drive_file_id"] == "vid-1"
    assert res["updated_at"]
    assert len(added) == 1
    created = added[0]
    assert created.kind == SAVE_KIND_DRAFT
    assert created.source == "draft_autosave"
    assert created.theme_key == "draft"
    assert created.status == "ready"
    assert created.payload["phase"] == 2
    assert created.payload["selected_hooks"] == ["h1"]

    # Second call: update existing in place (uniqueness)
    session_update = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [existing])
            )
        ),
        add=MagicMock(),
        commit=AsyncMock(),
        refresh=AsyncMock(),
        delete=AsyncMock(),
    )
    body2 = CarouselDraftUpsertBody(
        drive_file_id="vid-1",
        phase=4,
        selected_topics=["t1"],
        label="My draft",
    )
    res2 = await upsert_carousel_draft(body2, session=session_update)
    assert res2["id"] == 11
    assert existing.payload["phase"] == 4
    assert existing.payload["selected_topics"] == ["t1"]
    assert existing.label == "My draft"
    session_update.add.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_draft_collapses_duplicate_rows():
    primary = _save_row(id=1, drive_file_id="vid-x", kind=SAVE_KIND_DRAFT)
    duplicate = _save_row(id=2, drive_file_id="vid-x", kind=SAVE_KIND_DRAFT)
    deleted: list[object] = []
    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [primary, duplicate])
            )
        ),
        add=MagicMock(),
        commit=AsyncMock(),
        refresh=AsyncMock(),
        delete=AsyncMock(side_effect=deleted.append),
    )
    await upsert_carousel_draft(
        CarouselDraftUpsertBody(drive_file_id="vid-x", phase=3),
        session=session,
    )
    assert deleted == [duplicate]
    assert primary.payload["phase"] == 3


@pytest.mark.asyncio
async def test_get_and_delete_draft():
    row = _save_row(
        id=7,
        drive_file_id="vid-g",
        kind=SAVE_KIND_DRAFT,
        payload={"phase": 3, "updated_at": "2026-03-05T12:00:00+00:00", "hooks": []},
    )
    session_get = SimpleNamespace(
        scalar=AsyncMock(return_value=row),
    )
    got = await get_carousel_draft("vid-g", session=session_get)
    assert got["id"] == 7
    assert got["payload"]["phase"] == 3
    assert got["updated_at"] == "2026-03-05T12:00:00+00:00"

    session_missing = SimpleNamespace(scalar=AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await get_carousel_draft("missing", session=session_missing)
    assert exc.value.status_code == 404

    deleted: list[object] = []
    session_del = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [row]))
        ),
        delete=AsyncMock(side_effect=deleted.append),
        commit=AsyncMock(),
    )
    out = await delete_carousel_draft("vid-g", session=session_del)
    assert out["ok"] is True
    assert out["deleted"] == 1
    assert deleted == [row]


@pytest.mark.asyncio
async def test_projects_aggregation_lightweight():
    t_draft = datetime(2026, 3, 10, tzinfo=timezone.utc)
    t_fin = datetime(2026, 3, 9, tzinfo=timezone.utc)
    draft = _save_row(
        id=1,
        drive_file_id="vid-a",
        kind=SAVE_KIND_DRAFT,
        created_at=t_draft,
        label="WIP",
        payload={
            "phase": 3,
            "updated_at": "2026-03-10T08:00:00+00:00",
            "huge": "x" * 500,
        },
    )
    finished = _save_row(
        id=2,
        drive_file_id="vid-a",
        kind=SAVE_KIND_CAROUSEL,
        created_at=t_fin,
        label="Ready A",
        source="generation",
        payload={
            "carousels": [
                {"slides": [{"preview_url": "/media/video/vid-a/frame?ts=1"}]}
            ]
        },
        copy_version=2,
    )
    other_fin = _save_row(
        id=3,
        drive_file_id="vid-b",
        kind=SAVE_KIND_CAROUSEL,
        created_at=datetime(2026, 3, 8, tzinfo=timezone.utc),
        label="Only finished",
        payload={"slides": [{"preview_url": "https://evil.example/x.jpg"}]},
    )
    # topics_hooks should be ignored by the query filter; we only feed matching rows.

    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=SimpleNamespace(
            all=lambda: [
                (draft, "Talk A.mp4", "/Drive/A"),
                (finished, "Talk A.mp4", "/Drive/A"),
                (other_fin, "Talk B.mp4", "/Drive/B"),
            ]
        )
    )
    res = await list_carousel_projects(limit=10, offset=0, q=None, session=session)
    assert res["has_more"] is False
    assert len(res["items"]) == 2
    first = res["items"][0]
    assert first["drive_file_id"] == "vid-a"
    assert first["has_draft"] is True
    assert first["draft_phase"] == 3
    assert first["draft_id"] == 1
    assert first["finished_count"] == 1
    assert first["latest_finished"]["id"] == 2
    assert first["latest_finished"]["preview_url"] == "/media/video/vid-a/frame?ts=1"
    assert first["latest_finished"]["copy_version"] == 2
    assert "huge" not in first
    assert "payload" not in first
    second = res["items"][1]
    assert second["drive_file_id"] == "vid-b"
    assert second["has_draft"] is False
    assert second["latest_finished"]["preview_url"] is None  # external rejected


@pytest.mark.asyncio
async def test_projects_q_filter_and_pagination():
    rows = []
    for i, name in enumerate(["Alpha talk", "Beta clip", "Gamma"]):
        rows.append(
            (
                _save_row(
                    id=100 + i,
                    drive_file_id=f"v{i}",
                    kind=SAVE_KIND_CAROUSEL,
                    created_at=datetime(2026, 1, 10 - i, tzinfo=timezone.utc),
                    label=f"L{i}",
                    payload={},
                ),
                name,
                f"/{name}",
            )
        )
    session = AsyncMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(all=lambda: rows))

    filtered = await list_carousel_projects(limit=10, offset=0, q="beta", session=session)
    assert len(filtered["items"]) == 1
    assert filtered["items"][0]["video_name"] == "Beta clip"

    page = await list_carousel_projects(limit=2, offset=0, q=None, session=session)
    assert len(page["items"]) == 2
    assert page["has_more"] is True


@pytest.mark.asyncio
async def test_upsert_rejects_absurd_payload_size():
    # Build a body that exceeds the dumps size after jsonb_safe.
    huge = "y" * (DRAFT_PAYLOAD_MAX_BYTES + 1000)
    body = CarouselDraftUpsertBody(drive_file_id="vid-huge", phase=1, intent=huge)
    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
        ),
        add=MagicMock(),
        commit=AsyncMock(),
        refresh=AsyncMock(),
        delete=AsyncMock(),
    )
    with pytest.raises(HTTPException) as exc:
        await upsert_carousel_draft(body, session=session)
    assert exc.value.status_code == 413
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_malformed_draft_payload_get_never_crashes():
    row = _save_row(
        id=9,
        drive_file_id="vid-bad",
        kind=SAVE_KIND_DRAFT,
        payload=None,  # type: ignore[arg-type]
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=row))
    got = await get_carousel_draft("vid-bad", session=session)
    assert got["payload"] == {}
    assert got["id"] == 9


@pytest.mark.asyncio
async def test_copy_clears_draft():
    current = _save_row(
        id=21,
        drive_file_id="video-a",
        kind=SAVE_KIND_CAROUSEL,
        copy_version=4,
        payload={"carousels": [{"id": "c1", "slides": [{"caption": "old"}]}], "slides": []},
        source="generation",
        theme_key="",
    )
    current.algorithm_version = "p0"
    current.input_hash = "old"
    draft = _save_row(id=99, drive_file_id="video-a", kind=SAVE_KIND_DRAFT)
    deleted: list[object] = []

    # scalar: current artifact, then no existing hash match
    scalar = AsyncMock(side_effect=[current, None])
    # execute used by _clear_draft_for_video
    execute = AsyncMock(
        return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [draft]))
    )
    added: list[object] = []
    session = SimpleNamespace(
        scalar=scalar,
        execute=execute,
        add=MagicMock(side_effect=added.append),
        commit=AsyncMock(),
        refresh=AsyncMock(),
        delete=AsyncMock(side_effect=deleted.append),
    )
    body = CarouselArtifactCopyBody(
        drive_file_id="video-a",
        save_id=21,
        carousel_id="c1",
        layout_mode="single_1",
        slides=[{"caption": "edited"}],
    )
    await _save_carousel_copy_impl(body, session=session)
    assert deleted == [draft]
    assert len(added) == 1


@pytest.mark.asyncio
async def test_clear_draft_helper_idempotent():
    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
        ),
        delete=AsyncMock(),
        commit=AsyncMock(),
    )
    n = await _clear_draft_for_video(session, "vid-none")
    assert n == 0
    session.commit.assert_not_called()
