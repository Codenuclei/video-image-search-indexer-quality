"""Unit tests for face job enqueue helpers (no InsightFace runtime)."""
from __future__ import annotations

from app.config import Settings


def test_face_worker_defaults_are_sequential_and_off() -> None:
    settings = Settings()
    assert settings.face_jobs_enabled is False
    assert settings.run_face_worker is False
    assert settings.face_worker_concurrency == 1
    assert settings.face_job_max_attempts == 3
    assert settings.index_disk_high_water_bytes == 2 * 1024 * 1024 * 1024


def test_face_job_status_enum_members() -> None:
    from app.db.models import FaceJobStatus

    assert FaceJobStatus.PENDING.value == "pending"
    assert FaceJobStatus.PROCESSING.value == "processing"
    assert FaceJobStatus.DONE.value == "done"
    assert FaceJobStatus.ERROR.value == "error"
