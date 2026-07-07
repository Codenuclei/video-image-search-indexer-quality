"""
app/qdrant/folder_context.py
============================
Store and search folder-context embeddings in Qdrant.

Each folder_path gets a single 3072-dim Gemini Embedding 2 vector
stored in the `dfi_folder_contexts` collection.  At search time the
query vector is compared against folder context vectors to find the
most relevant folder scope.
"""
from __future__ import annotations

import hashlib
import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

_FOLDER_COLLECTION = "dfi_folder_contexts"
_DIM = 3072


def _folder_point_id(folder_path: str) -> int:
    """Stable integer ID derived from the folder path string."""
    return int(hashlib.md5(folder_path.encode()).hexdigest()[:12], 16)


@lru_cache(maxsize=1)
def _get_client():
    from qdrant_client import QdrantClient
    from app.config import get_settings
    return QdrantClient(url=get_settings().qdrant_url)


def _ensure_collection() -> None:
    from qdrant_client.models import Distance, VectorParams
    client = _get_client()
    existing = {c.name for c in client.get_collections().collections}
    if _FOLDER_COLLECTION not in existing:
        client.create_collection(
            _FOLDER_COLLECTION,
            vectors_config=VectorParams(size=_DIM, distance=Distance.COSINE),
        )
        logger.info("Created Qdrant collection %s", _FOLDER_COLLECTION)


def upsert_folder_context_sync(folder_path: str, description: str, vector: list[float]) -> None:
    from qdrant_client.models import PointStruct
    _ensure_collection()
    client = _get_client()
    point_id = _folder_point_id(folder_path)
    client.upsert(
        collection_name=_FOLDER_COLLECTION,
        points=[PointStruct(
            id=point_id,
            vector=vector,
            payload={"folder_path": folder_path, "description": description},
        )],
        wait=True,
    )
    logger.info("Upserted folder context for %s (id=%d)", folder_path, point_id)


def delete_folder_context_sync(folder_path: str) -> None:
    from qdrant_client.models import PointIdsList
    _ensure_collection()
    client = _get_client()
    client.delete(
        collection_name=_FOLDER_COLLECTION,
        points_selector=PointIdsList(points=[_folder_point_id(folder_path)]),
        wait=True,
    )
