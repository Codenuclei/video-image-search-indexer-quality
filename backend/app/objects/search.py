"""Structured object-tag lookup and deterministic search fusion."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Media, MediaObjectLabel
from app.objects.taxonomy import OBJECT_MODEL_VERSION, object_query_labels
from app.schemas import ObjectEvidence

OBJECT_EXACT_BOOST = 0.08
OBJECT_EXACT_BASE_SCORE = 0.72


async def object_matches_for_query(
    session: AsyncSession,
    query: str,
) -> dict[str, list[ObjectEvidence]]:
    labels = object_query_labels(query)
    if not labels:
        return {}
    rows = (
        await session.execute(
            select(Media.drive_file_id, MediaObjectLabel)
            .join(MediaObjectLabel, MediaObjectLabel.media_id == Media.id)
            .where(
                MediaObjectLabel.model_version == OBJECT_MODEL_VERSION,
                MediaObjectLabel.canonical_label.in_(labels),
            )
            .order_by(
                Media.drive_file_id,
                MediaObjectLabel.confidence.desc(),
                MediaObjectLabel.canonical_label,
            )
        )
    ).all()
    matches: dict[str, list[ObjectEvidence]] = {}
    for drive_file_id, label in rows:
        matches.setdefault(drive_file_id, []).append(
            ObjectEvidence(
                label=label.canonical_label,
                category=label.category,
                confidence=label.confidence,
                source=label.evidence_source,
                evidence_text=label.evidence_text,
                best_timestamp=label.best_timestamp,
                hit_count=label.hit_count,
            )
        )
    return matches


def fuse_object_score(
    score: float | None,
    matched_count: int,
    object_confidence: float = 1.0,
) -> float:
    if matched_count <= 0:
        return float(score or 0.0)
    confidence = max(0.0, min(1.0, float(object_confidence)))
    object_score = OBJECT_EXACT_BASE_SCORE + 0.18 * confidence
    base = max(float(score or 0.0), object_score)
    return min(0.99, base + OBJECT_EXACT_BOOST + 0.01 * (matched_count - 1))
