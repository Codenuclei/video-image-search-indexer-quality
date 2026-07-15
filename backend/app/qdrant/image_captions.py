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


@lru_cache(maxsize=1)
def _client() -> QdrantClient:
    from app.config import get_settings

    settings = get_settings()
    client = QdrantClient(url=settings.qdrant_url, timeout=30)
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
    limit: int = 30,
    min_score: float = 0.0,
) -> list[dict]:
    from app.config import get_settings

    client = _client()
    hits = client.query_points(
        get_settings().qdrant_image_captions_collection,
        query=query_vector,
        limit=limit,
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
    ]


def get_captions_by_ids_sync(drive_file_ids: list[str]) -> dict[str, str]:
    """Return stored caption text for drive files (empty string if missing)."""
    from app.config import get_settings

    if not drive_file_ids:
        return {}
    client = _client()
    id_map = {_point_id(fid): fid for fid in drive_file_ids}
    found = client.retrieve(
        collection_name=get_settings().qdrant_image_captions_collection,
        ids=list(id_map.keys()),
        with_payload=True,
        with_vectors=False,
    )
    out: dict[str, str] = {}
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
    found = client.retrieve(
        collection_name=get_settings().qdrant_image_captions_collection,
        ids=list(id_map.keys()),
        with_payload=False,
        with_vectors=False,
    )
    return {id_map[p.id] for p in found if p.id in id_map}


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
