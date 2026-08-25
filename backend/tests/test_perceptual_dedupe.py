"""Unit tests for OpenCV dHash visual dedupe."""
from __future__ import annotations

import cv2
import numpy as np
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile, DriveFileStatus
from app.drive.conflicts import apply_visual_dedupe_on_image
from app.drive.content_hash import DUPLICATE_CONTENT_PREFIX
from app.drive.perceptual_hash import dhash_hex_from_bgr, hamming_hex
from tests.conftest import requires_postgres


def test_dhash_cross_resolution_similar() -> None:
    base = np.zeros((64, 64, 3), dtype=np.uint8)
    cv2.rectangle(base, (8, 8), (56, 56), (220, 180, 40), -1)
    cv2.circle(base, (32, 32), 12, (30, 30, 30), -1)
    large = cv2.resize(base, (800, 800), interpolation=cv2.INTER_AREA)
    small = cv2.resize(base, (120, 120), interpolation=cv2.INTER_AREA)
    h_large = dhash_hex_from_bgr(large)
    h_small = dhash_hex_from_bgr(small)
    assert len(h_large) == 16
    assert hamming_hex(h_large, h_small) <= 5


@requires_postgres
@pytest.mark.asyncio
async def test_visual_dedupe_marks_processed(db_session: AsyncSession) -> None:
    img = np.zeros((80, 80, 3), dtype=np.uint8)
    cv2.rectangle(img, (10, 10), (70, 70), (255, 255, 255), -1)
    vh = dhash_hex_from_bgr(img)

    existing = DriveFile(
        id="vis_exist",
        name="a.jpg",
        mime_type="image/jpeg",
        path="/a.jpg",
        status=DriveFileStatus.PROCESSED,
        visual_hash=vh,
        content_hash="hash_a",
        content_hash_algo="md5",
    )
    incoming = DriveFile(
        id="vis_new",
        name="b.jpg",
        mime_type="image/jpeg",
        path="/b.jpg",
        status=DriveFileStatus.PENDING,
        visual_hash=vh,
        content_hash="hash_b",
        content_hash_algo="md5",
    )
    db_session.add_all([existing, incoming])
    await db_session.flush()

    reason = await apply_visual_dedupe_on_image(db_session, incoming, max_hamming=5)
    await db_session.flush()

    assert reason == "duplicate_content"
    assert incoming.status == DriveFileStatus.PROCESSED
    assert (incoming.error_message or "").startswith(DUPLICATE_CONTENT_PREFIX)
