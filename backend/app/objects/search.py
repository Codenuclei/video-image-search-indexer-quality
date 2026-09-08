"""Structured object-tag lookup and deterministic search fusion."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Media, MediaObjectLabel
from app.objects.identify_tags import (
    QWEN_IDENTIFY_MODEL_VERSION,
    cached_identify_overlay,
    parse_identify_query,
    persist_rows,
)
from app.objects.taxonomy import OBJECT_MODEL_VERSION, object_query_labels
from app.schemas import ObjectEvidence

OBJECT_EXACT_BOOST = 0.08
OBJECT_EXACT_BASE_SCORE = 0.72


def _evidence(label: MediaObjectLabel) -> ObjectEvidence:
    return ObjectEvidence(
        label=label.canonical_label,
        category=label.category,
        confidence=label.confidence,
        source=label.evidence_source,
        evidence_text=label.evidence_text,
        best_timestamp=label.best_timestamp,
        hit_count=label.hit_count,
    )


async def object_matches_for_query(
    session: AsyncSession,
    query: str,
    *,
    include_identify: bool = False,
) -> dict[str, list[ObjectEvidence]]:
    taxonomy_labels = object_query_labels(query)
    identify_query = parse_identify_query(query) if include_identify else None
    lookup = list(taxonomy_labels)
    if identify_query is not None:
        lookup.extend(sorted(identify_query.lookup_labels))
    lookup = list(dict.fromkeys(lookup))
    if not lookup:
        return {}

    versions = [OBJECT_MODEL_VERSION]
    if include_identify:
        versions.append(QWEN_IDENTIFY_MODEL_VERSION)

    rows = (
        await session.execute(
            select(Media.drive_file_id, MediaObjectLabel)
            .join(MediaObjectLabel, MediaObjectLabel.media_id == Media.id)
            .where(
                MediaObjectLabel.model_version.in_(versions),
                MediaObjectLabel.canonical_label.in_(lookup),
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
        matches.setdefault(drive_file_id, []).append(_evidence(label))

    if include_identify and identify_query is not None:
        overlay = cached_identify_overlay()
        lookup_set = set(lookup)
        for fid, parsed in overlay.items():
            rows = persist_rows(parsed)
            phrases = [str(row["canonical_label"]) for row in rows]
            if not (lookup_set & set(phrases)):
                continue
            extra = [
                ObjectEvidence(
                    label=str(row["canonical_label"]),
                    category=str(row["category"]),
                    confidence=float(row["confidence"]),
                    source=str(row["evidence_source"]),
                    evidence_text=str(row.get("evidence_text") or ""),
                    hit_count=int(row.get("hit_count") or 1),
                )
                for row in rows
                if str(row["canonical_label"]) in lookup_set
                or str(row["evidence_source"]) in {"qwen_identify", "qwen_action"}
            ]
            if extra:
                matches.setdefault(fid, []).extend(extra)

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
