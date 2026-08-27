"""Batched access to already-indexed Gemini image and sampled-frame vectors."""
from __future__ import annotations

import threading

import numpy as np
from qdrant_client.models import FieldCondition, Filter, MatchAny, QueryRequest

from app.objects.taxonomy import COLORS, TAXONOMY

_taxonomy_lock = threading.Lock()
_taxonomy_cache: tuple[list[tuple[str, str]], np.ndarray] | None = None


def _taxonomy_matrix() -> tuple[list[tuple[str, str]], np.ndarray]:
    global _taxonomy_cache
    if _taxonomy_cache is not None:
        return _taxonomy_cache
    with _taxonomy_lock:
        if _taxonomy_cache is not None:
            return _taxonomy_cache
        from app.gemini.video_embeddings import embed_texts_batch_sync

        keys = [(taxon.name, taxon.category) for taxon in TAXONOMY]
        keys.extend((("gray" if color == "grey" else color), "color") for color in COLORS)
        keys = list(dict.fromkeys(keys))
        vectors = embed_texts_batch_sync(
            [f"a photo containing {name}" for name, _category in keys]
        )
        matrix = np.asarray(vectors, dtype=np.float32)
        matrix /= np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-9)
        _taxonomy_cache = (keys, matrix)
        return _taxonomy_cache


def retrieve_media_vectors_sync(
    drive_file_ids: list[str],
) -> dict[str, list[tuple[float | None, list[float]]]]:
    """Retrieve image vectors and sampled video-frame vectors in bounded batches."""
    if not drive_file_ids:
        return {}
    from app.config import get_settings
    from app.qdrant.client import get_qdrant
    from app.qdrant.images import _client as image_client, _point_id

    settings = get_settings()
    out: dict[str, list[tuple[float | None, list[float]]]] = {
        fid: [] for fid in drive_file_ids
    }
    id_map = {_point_id(fid): fid for fid in drive_file_ids}
    for start in range(0, len(id_map), 256):
        ids = list(id_map)[start : start + 256]
        points = image_client().retrieve(
            collection_name=settings.qdrant_images_collection,
            ids=ids,
            with_payload=False,
            with_vectors=True,
        )
        for point in points:
            fid = id_map.get(point.id)
            vector = point.vector
            if fid and isinstance(vector, list):
                out[fid].append((None, vector))

    # One filtered scroll per claimed batch; only pre-existing sampled frames.
    offset = None
    while True:
        points, offset = get_qdrant().scroll(
            collection_name=settings.qdrant_collection,
            scroll_filter=Filter(
                must=[
                    FieldCondition(
                        key="drive_file_id",
                        match=MatchAny(any=drive_file_ids),
                    )
                ]
            ),
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        for point in points:
            payload = point.payload or {}
            fid = str(payload.get("drive_file_id") or "")
            vector = point.vector
            if fid in out and isinstance(vector, list):
                out[fid].append((float(payload.get("timestamp") or 0.0), vector))
        if offset is None:
            break
    return out


def classify_vectors(
    samples: list[tuple[float | None, list[float]]],
    *,
    confidence_floor: float,
) -> list[dict[str, object]]:
    """Score existing vectors against the cached taxonomy and aggregate frames."""
    if not samples:
        return []
    keys, taxonomy = _taxonomy_matrix()
    matrix = np.asarray([vector for _ts, vector in samples], dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[1] != taxonomy.shape[1]:
        return []
    matrix /= np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-9)
    scores = matrix @ taxonomy.T
    labels: list[dict[str, object]] = []
    for column, (name, category) in enumerate(keys):
        per_sample = scores[:, column]
        best_idx = int(np.argmax(per_sample))
        similarity = float(per_sample[best_idx])
        confidence = max(0.0, min(1.0, (similarity + 1.0) / 2.0))
        matching = np.flatnonzero((per_sample + 1.0) / 2.0 >= confidence_floor)
        if confidence < confidence_floor:
            continue
        labels.append({
            "canonical_label": name,
            "category": category,
            "confidence": confidence,
            "evidence_source": "gemini_embedding",
            "evidence_text": None,
            "best_timestamp": samples[best_idx][0],
            "hit_count": int(len(matching)),
        })
    return labels


def classify_via_taxonomy_queries_sync(
    drive_file_ids: list[str],
    *,
    confidence_floor: float,
) -> dict[str, list[dict[str, object]]]:
    """Fallback when vector retrieval is unavailable: two batched taxonomy searches."""
    if not drive_file_ids:
        return {}
    from app.config import get_settings
    from app.qdrant.client import get_qdrant
    from app.qdrant.images import _client as image_client

    settings = get_settings()
    keys, taxonomy = _taxonomy_matrix()
    point_filter = Filter(
        must=[
            FieldCondition(
                key="drive_file_id",
                match=MatchAny(any=drive_file_ids),
            )
        ]
    )
    image_requests = [
        QueryRequest(
            query=vector.tolist(),
            filter=point_filter,
            limit=len(drive_file_ids),
            score_threshold=max(-1.0, 2.0 * confidence_floor - 1.0),
            with_payload=True,
        )
        for vector in taxonomy
    ]
    frame_requests = [
        request.model_copy(update={"limit": max(1, len(drive_file_ids) * 16)})
        for request in image_requests
    ]
    batches = (
        image_client().query_batch_points(
            collection_name=settings.qdrant_images_collection,
            requests=image_requests,
        ),
        get_qdrant().query_batch_points(
            collection_name=settings.qdrant_collection,
            requests=frame_requests,
        ),
    )
    aggregated: dict[str, dict[str, dict[str, object]]] = {}
    for responses in batches:
        for (name, category), response in zip(keys, responses):
            for point in response.points:
                payload = point.payload or {}
                fid = str(payload.get("drive_file_id") or "")
                if fid not in drive_file_ids:
                    continue
                confidence = max(0.0, min(1.0, (float(point.score) + 1.0) / 2.0))
                by_label = aggregated.setdefault(fid, {})
                existing = by_label.get(name)
                timestamp = (
                    float(payload["timestamp"]) if payload.get("timestamp") is not None else None
                )
                if existing is None:
                    by_label[name] = {
                        "canonical_label": name,
                        "category": category,
                        "confidence": confidence,
                        "evidence_source": "gemini_embedding",
                        "evidence_text": None,
                        "best_timestamp": timestamp,
                        "hit_count": 1,
                    }
                else:
                    existing["hit_count"] = int(existing["hit_count"]) + 1
                    if confidence > float(existing["confidence"]):
                        existing["confidence"] = confidence
                        existing["best_timestamp"] = timestamp
    return {fid: list(labels.values()) for fid, labels in aggregated.items()}
