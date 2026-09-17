"""Durable Carousel Studio jobs survive process restart (Postgres-backed)."""

from __future__ import annotations

import pytest

from app.db.models import CarouselStudioJob
from app.search.carousel_studio_jobs import (
    KIND_EXTRACT,
    KIND_VISUAL_PREP,
    STATUS_PREPARING,
    STATUS_READY,
    STATUS_RUNNING,
    is_running,
    latest_job,
    mark_running,
    clear_running,
    read_job,
    write_job,
)
from app.search.carousel_visual_prep import (
    latest_job_for_fingerprint,
    slides_fingerprint,
    write_job as write_visual_job,
    read_job as read_visual_job,
)
from tests.conftest import requires_postgres


@requires_postgres
@pytest.mark.asyncio
async def test_visual_prep_job_roundtrip_postgres(db_session) -> None:
    slides = [
        {"timestamp_sec": 1, "end_timestamp_sec": 2, "transcript_text": "hello"},
        {"timestamp_sec": 3, "end_timestamp_sec": 4, "hook_line": "world"},
    ]
    fp = slides_fingerprint(slides)
    created = await write_visual_job(
        db_session,
        "vid-1",
        status=STATUS_PREPARING,
        request_body={"slides_fingerprint": fp, "body": {"drive_file_id": "vid-1"}},
    )
    job_id = created["job_id"]
    loaded = await read_visual_job(db_session, "vid-1", job_id)
    assert loaded is not None
    assert loaded["status"] == STATUS_PREPARING
    assert loaded["request"]["slides_fingerprint"] == fp

    await write_visual_job(
        db_session,
        "vid-1",
        job_id=job_id,
        status=STATUS_READY,
        payload={"images_ready": True, "carousels": [{"id": "c1"}]},
        error=None,
    )
    ready = await read_visual_job(db_session, "vid-1", job_id)
    assert ready["status"] == STATUS_READY
    assert ready["result"]["images_ready"] is True

    # Simulate restart: in-process running set is empty, row still preparing/ready in DB.
    clear_running(job_id)
    assert is_running(job_id) is False
    latest = await latest_job_for_fingerprint(db_session, "vid-1", fp)
    assert latest is not None
    assert latest["job_id"] == job_id
    assert latest["status"] == STATUS_READY


@requires_postgres
@pytest.mark.asyncio
async def test_extract_job_status_survives_without_filesystem(db_session) -> None:
    job = await write_job(
        db_session,
        kind=KIND_EXTRACT,
        drive_file_id="vid-2",
        status=STATUS_RUNNING,
        request_body={"body": {"drive_file_id": "vid-2", "generate": True}},
    )
    jid = job["job_id"]
    row = await db_session.get(CarouselStudioJob, jid)
    assert row is not None
    assert row.kind == KIND_EXTRACT

    # "Restart": clear process-local claim; Postgres row remains.
    mark_running(jid)
    clear_running(jid)
    again = await read_job(db_session, jid, kind=KIND_EXTRACT)
    assert again is not None
    assert again["status"] == STATUS_RUNNING
    assert again["request"]["body"]["drive_file_id"] == "vid-2"

    await write_job(
        db_session,
        kind=KIND_EXTRACT,
        drive_file_id="vid-2",
        status=STATUS_READY,
        job_id=jid,
        payload={"hooks": [], "topics": [{"text": "t1"}], "status": "ready"},
    )
    ready = await read_job(db_session, jid, kind=KIND_EXTRACT)
    assert ready["status"] == STATUS_READY
    assert ready["result"]["topics"][0]["text"] == "t1"

    latest = await latest_job(db_session, drive_file_id="vid-2", kind=KIND_EXTRACT)
    assert latest["job_id"] == jid


def test_slides_fingerprint_stable() -> None:
    slides = [
        {"timestamp_sec": 1, "end_timestamp_sec": 2, "transcript_text": "hello"},
        {"timestamp_sec": 3, "end_timestamp_sec": 4, "hook_line": "world"},
    ]
    assert slides_fingerprint(slides) == slides_fingerprint(list(slides))


def test_studio_http_budget_under_app_platform_cap() -> None:
    from app.routers import carousel_script as mod

    assert mod._STUDIO_HTTP_BUDGET_SEC <= 90.0
    assert mod._SELECT_IMAGES_REQUEST_TIMEOUT_SEC <= 90.0
