"""
app/qdrant/client.py
====================
Qdrant client for DFI video frame embeddings (Gemini Embedding 2 vectors).

Uses the synchronous QdrantClient wrapped in asyncio.to_thread() for
FastAPI compatibility.  Maintains a separate collection from SVS so
both can coexist in the same Qdrant instance during transition.
"""
from __future__ import annotations

import hashlib
import logging
from functools import lru_cache

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

logger = logging.getLogger(__name__)

_DIM = 3072


@lru_cache(maxsize=1)
def get_qdrant() -> QdrantClient:
    from app.config import get_settings
    settings = get_settings()
    client = QdrantClient(url=settings.qdrant_url, timeout=30)
    _ensure_collection(client, settings.qdrant_collection)
    return client


def _ensure_collection(client: QdrantClient, name: str) -> None:
    existing = {c.name for c in client.get_collections().collections}
    if name not in existing:
        client.create_collection(
            name,
            vectors_config=VectorParams(size=_DIM, distance=Distance.COSINE),
        )
        logger.info("Created Qdrant collection '%s'  dim=%d", name, _DIM)


def _point_id(drive_file_id: str, timestamp: float) -> int:
    """Stable integer ID from (drive_file_id, timestamp)."""
    key = f"{drive_file_id}::{timestamp:.3f}"
    return int(hashlib.sha256(key.encode()).hexdigest()[:15], 16)


def upsert_frame_sync(
    *,
    drive_file_id: str,
    timestamp: float,
    vector: list[float],
) -> None:
    """Upsert one frame vector.  Call via asyncio.to_thread()."""
    from app.config import get_settings
    client = get_qdrant()
    pid    = _point_id(drive_file_id, timestamp)
    client.upsert(
        collection_name=get_settings().qdrant_collection,
        points=[PointStruct(
            id=pid,
            vector=vector,
            payload={
                "drive_file_id": drive_file_id,
                "timestamp":     timestamp,
            },
        )],
    )


def search_frames_sync(
    query_vector: list[float],
    *,
    limit: int = 10,
    min_score: float = 0.0,
) -> list[dict]:
    """
    Search Qdrant for nearest frames.  Call via asyncio.to_thread().
    Returns list of {drive_file_id, timestamp, score}.
    """
    from app.config import get_settings
    client = get_qdrant()
    hits   = client.query_points(
        get_settings().qdrant_collection,
        query=query_vector,
        limit=limit,
        score_threshold=min_score if min_score > 0 else None,
    ).points
    return [
        {
            "drive_file_id": h.payload["drive_file_id"],
            "timestamp":     h.payload["timestamp"],
            "score":         h.score,
        }
        for h in hits
    ]


def collection_info_sync() -> dict:
    """Return collection stats for health endpoint."""
    try:
        from app.config import get_settings
        client = get_qdrant()
        info   = client.get_collection(get_settings().qdrant_collection)
        return {
            "status":  "ok",
            "points":  info.points_count,
            "collection": get_settings().qdrant_collection,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
