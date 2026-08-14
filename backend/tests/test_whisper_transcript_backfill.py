"""On-demand Whisper transcript backfill endpoints."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import BackgroundTasks

from app.routers.carousel_script import (
    EnsureTranscriptRequest,
    carousel_ensure_transcript,
    carousel_transcript_status,
)
from app.video.whisper_backfill import (
    TRANSCRIPT_RUNNING_PREFIX,
    claim_transcript_job,
    transcript_status_payload,
)


@pytest.mark.asyncio
async def test_transcript_status_ready_when_cues_exist() -> None:
    drive_file = SimpleNamespace(
        id="v1",
        name="clip.mp4",
        error_message=None,
    )
    media = SimpleNamespace(id=9)
    session = AsyncMock()
    session.get.return_value = drive_file

    async def _execute(stmt):
        sql = str(stmt)
        if "media" in sql.lower() or "Media" in sql:
            return SimpleNamespace(scalar_one_or_none=lambda: media)
        return SimpleNamespace(scalar_one=lambda: 42)

    session.execute.side_effect = _execute

    with patch(
        "app.video.whisper_backfill.count_text_cues",
        new=AsyncMock(return_value=42),
    ):
        payload = await transcript_status_payload(session, "v1")

    assert payload["status"] == "ready"
    assert payload["cue_count"] == 42
    assert payload["has_captions"] is True


@pytest.mark.asyncio
async def test_claim_transcript_job_marks_running() -> None:
    drive_file = SimpleNamespace(
        id="v2",
        name="clip.mp4",
        error_message=None,
    )
    session = AsyncMock()
    session.get.return_value = drive_file
    session.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: None)
    )
    session.commit = AsyncMock()

    claim = await claim_transcript_job(session, "v2")
    assert claim == "claimed"
    assert str(drive_file.error_message).startswith(TRANSCRIPT_RUNNING_PREFIX)
    session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_ensure_transcript_endpoint_schedules_background() -> None:
    session = AsyncMock()
    bg = BackgroundTasks()

    with (
        patch(
            "app.video.whisper_backfill.transcript_status_payload",
            new=AsyncMock(
                side_effect=[
                    {
                        "ok": True,
                        "status": "missing",
                        "cue_count": 0,
                        "name": "clip.mp4",
                    },
                    {
                        "ok": True,
                        "status": "running",
                        "cue_count": 0,
                        "phase": "starting",
                        "message": "Getting transcripts from the video…",
                        "name": "clip.mp4",
                    },
                ]
            ),
        ),
        patch(
            "app.video.whisper_backfill.claim_transcript_job",
            new=AsyncMock(return_value="claimed"),
        ),
        patch("app.video.whisper_backfill.ensure_whisper_transcript") as ensure_fn,
    ):
        # Re-import path used inside the route
        result = await carousel_ensure_transcript(
            EnsureTranscriptRequest(drive_file_id="v3"),
            bg,
            session,
        )

    assert result["status"] == "running"
    assert "Getting transcripts" in (result.get("message") or "")
    assert len(bg.tasks) == 1
