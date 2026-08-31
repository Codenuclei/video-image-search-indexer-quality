"""Qdrant collection for image *caption* text embeddings (Gemini Embedding 2).

Captions are generated at index time (VLM describe) and embedded as text. At
search time the query embedding is matched against captions — a text→text
comparison that is far better calibrated than query→image, so it acts as a
precision gate fused with the raw visual score.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from functools import lru_cache

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

logger = logging.getLogger(__name__)

_DIM = 3072
_GENERIC_CAPTION_RE = re.compile(
    r"^(?:a|an|the)?\s*(?:completely\s+)?(?:black|blank|empty)\s+image\.?$|"
    r"^(?:a|an|the)?\s*image\s+(?:of|showing)\s+(?:some\s+)?people\.?$|"
    r"^(?:no|without)\s+visible\s+content\.?$",
    re.IGNORECASE,
)
_LEXICAL_STOP_WORDS = frozenset({
    "a", "an", "and", "are", "at", "for", "from", "in", "of", "on", "or",
    "photo", "photos", "image", "images", "picture", "pictures", "show", "showing",
    "the", "to", "with",
})
_GRADUATION_CONCEPT_RE = re.compile(
    r"\b(?:graduat\w*|convocation|mortarboards?|academic\s+gowns?|"
    r"graduation\s+caps?|yellow\s+stoles?|black\s+and\s+yellow\s+(?:robes?|gowns?))\b",
    re.IGNORECASE,
)


def caption_matches_query_text(caption: str, query: str) -> bool:
    """Lexical recall path used alongside vector similarity."""
    cap = caption.lower()
    normalized_query = re.sub(r"\bgradutes\b", "graduates", query.lower())
    if re.search(r"\b(?:graduate|graduates|graduation|convocation)\b", normalized_query):
        return bool(_GRADUATION_CONCEPT_RE.search(caption))

    # Object/brand queries: same concept rules as fusion (aliases, compact forms,
    # apparel↔brand association) so lexical recall is not stuck on raw tokens
    # like "tshirt" vs "t-shirt" or "text" scaffolding.
    try:
        from app.objects.query_concepts import (
            all_query_concepts_supported,
            parse_query_concepts,
        )

        concepts = parse_query_concepts(normalized_query)
        if concepts.taxonomy_labels or concepts.residual_terms:
            return all_query_concepts_supported(concepts, caption=caption)
    except Exception:  # noqa: BLE001
        logger.debug("concept lexical match failed for %r", query, exc_info=True)

    tokens = [
        token
        for token in re.findall(r"\w+", normalized_query)
        if len(token) >= 3 and token not in _LEXICAL_STOP_WORDS
    ]
    if not tokens:
        return False
    return all(
        token in cap
        or (token.endswith("s") and token[:-1] in cap)
        or (not token.endswith("s") and f"{token}s" in cap)
        for token in tokens
    )


@lru_cache(maxsize=1)
def _client() -> QdrantClient:
    from app.config import get_settings
    from app.qdrant.client import make_qdrant_client

    settings = get_settings()
    client = make_qdrant_client(settings.qdrant_url, timeout=30)
    _ensure_collection(client, settings.qdrant_image_captions_collection)
    return client


def _ensure_collection(client: QdrantClient, name: str) -> None:
    existing = {c.name for c in client.get_collections().collections}
    if name not in existing:
        client.create_collection(
            name,
            vectors_config=VectorParams(size=_DIM, distance=Distance.COSINE),
        )
        logger.info("Created Qdrant image-caption collection '%s'", name)


def caption_word_count(text: str | None) -> int:
    return len(re.findall(r"\w+", (text or "").strip()))


def is_valid_caption(text: str | None, *, min_words: int | None = None) -> bool:
    """A caption must be more than a stub (e.g. 'photo', 'image of people')."""
    from app.config import get_settings

    threshold = min_words if min_words is not None else get_settings().image_caption_min_words
    cleaned = (text or "").strip()
    if not cleaned:
        return False
    if _GENERIC_CAPTION_RE.fullmatch(cleaned):
        return False
    lowered = cleaned.lower()
    if "blank image" in lowered and "no visible content" in lowered:
        return False
    return caption_word_count(cleaned) >= max(1, threshold)


def valid_caption_ids_sync(
    drive_file_ids: list[str],
    *,
    min_words: int | None = None,
) -> set[str]:
    """IDs with a Qdrant caption point whose text passes quality checks."""
    if not drive_file_ids:
        return set()
    captions = get_captions_by_ids_sync(drive_file_ids)
    return {fid for fid, text in captions.items() if is_valid_caption(text, min_words=min_words)}


def invalid_caption_ids_sync(
    drive_file_ids: list[str],
    *,
    min_words: int | None = None,
) -> set[str]:
    """IDs with a caption point but stub/empty text — should be re-captioned."""
    if not drive_file_ids:
        return set()
    existing = existing_caption_ids_sync(drive_file_ids)
    valid = valid_caption_ids_sync(list(existing), min_words=min_words)
    return existing - valid


def caption_quality_stats_sync(drive_file_ids: list[str]) -> dict[str, int]:
    """Audit caption coverage: valid, invalid stubs, and missing."""
    if not drive_file_ids:
        return {"total": 0, "valid": 0, "invalid": 0, "missing": 0}
    existing = existing_caption_ids_sync(drive_file_ids)
    valid = valid_caption_ids_sync(list(existing))
    invalid = existing - valid
    return {
        "total": len(drive_file_ids),
        "valid": len(valid),
        "invalid": len(invalid),
        "missing": len(drive_file_ids) - len(existing),
    }


def _point_id(drive_file_id: str) -> int:
    return int(hashlib.sha256(f"cap::{drive_file_id}".encode()).hexdigest()[:15], 16)


def upsert_caption_sync(*, drive_file_id: str, vector: list[float], caption: str) -> None:
    from app.config import get_settings

    client = _client()
    collection = get_settings().qdrant_image_captions_collection
    point = PointStruct(
        id=_point_id(drive_file_id),
        vector=vector,
        payload={"drive_file_id": drive_file_id, "caption": caption},
    )
    last_exc: Exception | None = None
    for attempt in range(5):
        try:
            client.upsert(collection_name=collection, points=[point])
            try:
                from app.drive.library_folder_media_cache import note_media_presence

                note_media_presence(drive_file_id, captioned=True)
            except Exception:  # noqa: BLE001
                pass
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            wait = min(30, 2 ** attempt)
            logger.warning(
                "Qdrant caption upsert failed for %s (attempt %d/5): %s",
                drive_file_id,
                attempt + 1,
                str(exc)[:120],
            )
            time.sleep(wait)
    raise last_exc  # type: ignore[misc]


def delete_caption_sync(drive_file_id: str) -> None:
    from app.config import get_settings

    client = _client()
    client.delete(
        collection_name=get_settings().qdrant_image_captions_collection,
        points_selector=[_point_id(drive_file_id)],
    )


def search_captions_sync(
    query_vector: list[float],
    *,
    limit: int | None = 30,
    min_score: float = 0.0,
    page_size: int = 200,
) -> list[dict]:
    from app.config import get_settings
    from app.qdrant.images import _QDRANT_QUERY_MAX

    client = _client()
    collection = get_settings().qdrant_image_captions_collection
    fetch = (
        max(1, limit)
        if limit is not None
        else min(_QDRANT_QUERY_MAX, max(page_size, _QDRANT_QUERY_MAX))
    )
    hits = client.query_points(
        collection,
        query=query_vector,
        limit=fetch,
        score_threshold=min_score if min_score > 0 else None,
        with_payload=True,
    ).points
    return [
        {
            "drive_file_id": h.payload["drive_file_id"],
            "score": h.score,
            "caption": h.payload.get("caption", ""),
        }
        for h in hits
        if is_valid_caption(str(h.payload.get("caption") or ""))
    ]


def search_caption_keywords_sync(
    query: str,
    *,
    page_size: int = 500,
) -> list[dict]:
    """Return every valid caption with a direct query/concept text match."""
    from app.config import get_settings

    client = _client()
    collection = get_settings().qdrant_image_captions_collection
    offset = None
    matches: list[dict] = []
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            limit=max(1, page_size),
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for point in points:
            payload = point.payload or {}
            caption = str(payload.get("caption") or "")
            if is_valid_caption(caption) and caption_matches_query_text(caption, query):
                matches.append(
                    {
                        "drive_file_id": payload["drive_file_id"],
                        "caption": caption,
                    }
                )
        if offset is None:
            break
    return matches


def get_captions_by_ids_sync(drive_file_ids: list[str]) -> dict[str, str]:
    """Return stored caption text for drive files (empty string if missing)."""
    from app.config import get_settings

    if not drive_file_ids:
        return {}
    client = _client()
    id_map = {_point_id(fid): fid for fid in drive_file_ids}
    out: dict[str, str] = {}
    point_ids = list(id_map)
    for start in range(0, len(point_ids), 500):
        found = client.retrieve(
            collection_name=get_settings().qdrant_image_captions_collection,
            ids=point_ids[start : start + 500],
            with_payload=True,
            with_vectors=False,
        )
        for point in found:
            fid = id_map.get(point.id)
            if fid and point.payload:
                out[fid] = str(point.payload.get("caption") or "")
    return out


def existing_caption_ids_sync(drive_file_ids: list[str]) -> set[str]:
    """Return the subset of drive_file_ids that already have a caption."""
    from app.config import get_settings

    if not drive_file_ids:
        return set()
    client = _client()
    id_map = {_point_id(fid): fid for fid in drive_file_ids}
    found_ids: set[str] = set()
    point_ids = list(id_map)
    for start in range(0, len(point_ids), 500):
        found = client.retrieve(
            collection_name=get_settings().qdrant_image_captions_collection,
            ids=point_ids[start : start + 500],
            with_payload=False,
            with_vectors=False,
        )
        found_ids.update(id_map[p.id] for p in found if p.id in id_map)
    return found_ids


def collection_info_sync() -> dict:
    try:
        from app.config import get_settings

        client = _client()
        info = client.get_collection(get_settings().qdrant_image_captions_collection)
        return {
            "status": "ok",
            "points": info.points_count,
            "collection": get_settings().qdrant_image_captions_collection,
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": str(exc)}
