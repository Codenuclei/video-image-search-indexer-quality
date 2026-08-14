"""Local video-transcript RAG in Qdrant (replaces Gemini File Search uploads).

At index time each spoken/VLM segment is embedded as text and stored under
``dfi_video_transcripts``. At search time the query embedding is matched
text→text — same pattern as image captions, fully self-hosted beside frame
vectors in ``dfi_video_frames``.
"""
from __future__ import annotations

import hashlib
import logging
import time
from functools import lru_cache

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

logger = logging.getLogger(__name__)

_DIM = 3072


@lru_cache(maxsize=1)
def _client() -> QdrantClient:
    from app.config import get_settings
    from app.qdrant.client import make_qdrant_client

    settings = get_settings()
    client = make_qdrant_client(settings.qdrant_url, timeout=30)
    _ensure_collection(client, settings.qdrant_video_transcripts_collection)
    return client


def _ensure_collection(client: QdrantClient, name: str) -> None:
    existing = {c.name for c in client.get_collections().collections}
    if name not in existing:
        client.create_collection(
            name,
            vectors_config=VectorParams(size=_DIM, distance=Distance.COSINE),
        )
        logger.info("Created Qdrant video-transcript collection '%s'", name)


def _point_id(drive_file_id: str, start_sec: float) -> int:
    key = f"vt::{drive_file_id}::{start_sec:.3f}"
    return int(hashlib.sha256(key.encode()).hexdigest()[:15], 16)


def upsert_transcript_segment_sync(
    *,
    drive_file_id: str,
    start_sec: float,
    end_sec: float | None,
    text: str,
    vector: list[float],
) -> None:
    """Upsert one transcript/VLM segment. Call via asyncio.to_thread()."""
    from app.config import get_settings

    cleaned = (text or "").strip()
    if not cleaned or len(vector) != _DIM:
        return

    client = _client()
    collection = get_settings().qdrant_video_transcripts_collection
    point = PointStruct(
        id=_point_id(drive_file_id, start_sec),
        vector=vector,
        payload={
            "drive_file_id": drive_file_id,
            "start_sec": float(start_sec),
            "end_sec": float(end_sec) if end_sec is not None else None,
            "text": cleaned[:2000],
        },
    )
    last_exc: Exception | None = None
    for attempt in range(5):
        try:
            client.upsert(collection_name=collection, points=[point])
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            wait = min(30, 2**attempt)
            logger.warning(
                "Qdrant transcript upsert failed for %s@%.1fs (attempt %d/5): %s",
                drive_file_id,
                start_sec,
                attempt + 1,
                str(exc)[:120],
            )
            time.sleep(wait)
    if last_exc is not None:
        raise last_exc


def search_transcripts_sync(
    query_vector: list[float],
    *,
    limit: int = 20,
    min_score: float = 0.0,
) -> list[dict]:
    """Nearest transcript segments. Returns drive_file_id, start_sec, end_sec, text, score."""
    from app.config import get_settings

    client = _client()
    hits = client.query_points(
        get_settings().qdrant_video_transcripts_collection,
        query=query_vector,
        limit=limit,
        score_threshold=min_score if min_score > 0 else None,
    ).points
    out: list[dict] = []
    for h in hits:
        payload = h.payload or {}
        out.append(
            {
                "drive_file_id": payload.get("drive_file_id"),
                "start_sec": float(payload.get("start_sec") or 0.0),
                "end_sec": (
                    float(payload["end_sec"])
                    if payload.get("end_sec") is not None
                    else None
                ),
                "text": payload.get("text") or "",
                "score": float(h.score or 0.0),
            }
        )
    return out


def delete_transcripts_for_file_sync(drive_file_id: str) -> None:
    """Delete all transcript points for one drive file (filter by payload)."""
    from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue

    from app.config import get_settings

    file_id = (drive_file_id or "").strip()
    if not file_id:
        return

    client = _client()
    collection = get_settings().qdrant_video_transcripts_collection
    client.delete(
        collection_name=collection,
        points_selector=FilterSelector(
            filter=Filter(
                must=[
                    FieldCondition(
                        key="drive_file_id",
                        match=MatchValue(value=file_id),
                    )
                ]
            )
        ),
    )
    logger.info("Deleted Qdrant transcript points for %s", file_id)
