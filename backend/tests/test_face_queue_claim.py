"""Face job claim: poison high-attempt jobs before reclaim."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile, DriveFileStatus, FaceJob, FaceJobStatus
from app.workers.face_queue import claim_face_jobs
from tests.conftest import requires_postgres


@requires_postgres
@pytest.mark.asyncio
async def test_claim_marks_high_attempt_jobs_error(db_session: AsyncSession) -> None:
    db_session.add(
        DriveFile(
            id="poison-pending",
            name="a.jpg",
            mime_type="image/jpeg",
            path="/a.jpg",
            status=DriveFileStatus.PENDING,
        )
    )
    db_session.add(
        DriveFile(
            id="poison-stale",
            name="b.jpg",
            mime_type="image/jpeg",
            path="/b.jpg",
            status=DriveFileStatus.PENDING,
        )
    )
    db_session.add(
        DriveFile(
            id="claimable",
            name="c.jpg",
            mime_type="image/jpeg",
            path="/c.jpg",
            status=DriveFileStatus.PENDING,
        )
    )
    await db_session.flush()

    stale_lock = datetime.now(timezone.utc) - timedelta(hours=2)
    db_session.add(
        FaceJob(
            drive_file_id="poison-pending",
            status=FaceJobStatus.PENDING,
            attempts=3,
        )
    )
    db_session.add(
        FaceJob(
            drive_file_id="poison-stale",
            status=FaceJobStatus.PROCESSING,
            attempts=5,
            lock_token="old",
            locked_at=stale_lock,
        )
    )
    db_session.add(
        FaceJob(
            drive_file_id="claimable",
            status=FaceJobStatus.PENDING,
            attempts=0,
        )
    )
    await db_session.commit()

    claimed = await claim_face_jobs(
        db_session,
        limit=4,
        lease_seconds=900,
        max_attempts=3,
    )
    await db_session.commit()

    assert len(claimed) == 1
    assert claimed[0].drive_file_id == "claimable"
    assert claimed[0].status == FaceJobStatus.PROCESSING

    rows = {
        j.drive_file_id: j
        for j in (await db_session.execute(select(FaceJob))).scalars().all()
    }
    assert rows["poison-pending"].status == FaceJobStatus.ERROR
    assert rows["poison-pending"].error_message == "lease_expired_max_attempts"
    assert rows["poison-pending"].lock_token is None
    assert rows["poison-stale"].status == FaceJobStatus.ERROR
    assert rows["poison-stale"].error_message == "lease_expired_max_attempts"
    assert rows["poison-stale"].lock_token is None
    assert rows["claimable"].status == FaceJobStatus.PROCESSING


def test_face_worker_gpu_concurrency_floor_is_not_forced() -> None:
    from app.workers import face_queue as face_mod
    import inspect

    src = inspect.getsource(face_mod.FaceWorkerLoop.ensure_started)
    assert "min(4, max(1, n))" in src
    assert "min(16, max(4, n))" not in src
