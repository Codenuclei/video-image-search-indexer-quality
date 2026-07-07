"""Qdrant collection for full-image embeddings (Gemini Embedding 2)."""
from __future__ import annotations

import hashlib
import logging
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
    _ensure_collection(client, settings.qdrant_images_collection)
    return client


def _ensure_collection(client: QdrantClient, name: str) -> None:
    existing = {c.name for c in client.get_collections().collections}
    if name not in existing:
        client.create_collection(
            name,
            vectors_config=VectorParams(size=_DIM, distance=Distance.COSINE),
        )
        logger.info("Created Qdrant image collection '%s'", name)


def _point_id(drive_file_id: str) -> int:
    return int(hashlib.sha256(f"img::{drive_file_id}".encode()).hexdigest()[:15], 16)


def upsert_image_sync(*, drive_file_id: str, vector: list[float]) -> None:
    from app.config import get_settings

    client = _client()
    client.upsert(
        collection_name=get_settings().qdrant_images_collection,
        points=[
            PointStruct(
                id=_point_id(drive_file_id),
                vector=vector,
                payload={"drive_file_id": drive_file_id},
            )
        ],
    )


def delete_image_sync(drive_file_id: str) -> None:
    from app.config import get_settings

    client = _client()
    client.delete(
        collection_name=get_settings().qdrant_images_collection,
        points_selector=[_point_id(drive_file_id)],
    )


def search_images_sync(
    query_vector: list[float],
    *,
    limit: int = 20,
    min_score: float = 0.0,
) -> list[dict]:
    from app.config import get_settings

    client = _client()
    hits = client.query_points(
        get_settings().qdrant_images_collection,
        query=query_vector,
        limit=limit,
        score_threshold=min_score if min_score > 0 else None,
    ).points
    return [
        {"drive_file_id": h.payload["drive_file_id"], "score": h.score}
        for h in hits
    ]


def existing_image_ids_sync(drive_file_ids: list[str]) -> set[str]:
    """Return the subset of drive_file_ids that already have an embedding."""
    from app.config import get_settings

    if not drive_file_ids:
        return set()
    client = _client()
    id_map = {_point_id(fid): fid for fid in drive_file_ids}
    found = client.retrieve(
        collection_name=get_settings().qdrant_images_collection,
        ids=list(id_map.keys()),
        with_payload=False,
        with_vectors=False,
    )
    return {id_map[p.id] for p in found if p.id in id_map}


def collection_info_sync() -> dict:
    try:
        from app.config import get_settings

        client = _client()
        info = client.get_collection(get_settings().qdrant_images_collection)
        return {
            "status": "ok",
            "points": info.points_count,
            "collection": get_settings().qdrant_images_collection,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
