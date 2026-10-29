"""Real-face / plural student evidence against Postgres ("Pratham Mittal with Students")."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.db.models import DriveFile, DriveFileStatus, Face, Media, MediaType, Person
from app.search.local import (
    matching_drive_file_ids_for_roles,
    parse_role_context,
    student_face_counts,
)
from tests.conftest import requires_postgres

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


async def _photo(session, file_id: str, faces: list[dict]) -> None:
    session.add(
        DriveFile(
            id=file_id,
            name=f"{file_id}.jpg",
            mime_type="image/jpeg",
            path=f"/Events/{file_id}.jpg",
            status=DriveFileStatus.PROCESSED,
            last_synced_at=T0,
        )
    )
    await session.flush()
    media = Media(drive_file_id=file_id, type=MediaType.IMAGE)
    session.add(media)
    await session.flush()
    for face in faces:
        session.add(
            Face(
                media_id=media.id,
                bbox_x=face.get("x", 100.0),
                bbox_y=face.get("y", 100.0),
                bbox_width=face.get("w", 150.0),
                bbox_height=face.get("h", 200.0),
                detection_confidence=face.get("conf", 0.85),
                person_id=face.get("person_id"),
            )
        )
    await session.flush()


@requires_postgres
@pytest.mark.asyncio
async def test_panel_with_mannequin_and_screen_face_is_not_students(db_session) -> None:
    pratham = Person(name="Pratham Mittal", role="non_student")
    nas = Person(name="Nas Daily", role="non_student")
    db_session.add_all([pratham, nas])
    await db_session.flush()

    # Copy of 9S0A8142.JPG: two panelists, VR mannequin (0.64), backdrop screen face off-image.
    await _photo(
        db_session,
        "panel",
        [
            {"person_id": pratham.id},
            {"person_id": nas.id},
            {"x": 693, "y": 1793, "w": 180, "h": 188, "conf": 0.64},
            {"x": 3226, "y": -480, "w": 781, "h": 1022, "conf": 0.57},
        ],
    )
    await _photo(db_session, "one_student", [{"person_id": pratham.id}, {"conf": 0.86}])
    await _photo(
        db_session,
        "classroom",
        [{"person_id": pratham.id}, {"conf": 0.88}, {"conf": 0.81}, {"conf": 0.9}],
    )
    await db_session.commit()

    ids = ["panel", "one_student", "classroom"]
    _, plural = parse_role_context("Pratham Mittal with Students")
    _, singular = parse_role_context("Pratham Mittal with a student")

    assert await matching_drive_file_ids_for_roles(
        db_session, ids, person_names=["Pratham Mittal"], role_ctx=plural
    ) == ["classroom"]
    assert sorted(
        await matching_drive_file_ids_for_roles(
            db_session, ids, person_names=["Pratham Mittal"], role_ctx=singular
        )
    ) == ["classroom", "one_student"]
    assert await student_face_counts(db_session, ids, exclude_person_ids=[pratham.id]) == {
        "one_student": 1,
        "classroom": 3,
    }
