"""Closed-set face identification helpers. L2 on unit vectors equals cosine rank."""

from __future__ import annotations

import numpy as np

DIM = 512


def l2_normalize(vec: np.ndarray) -> np.ndarray:
    v = np.asarray(vec, dtype=np.float64).reshape(-1)
    n = float(np.linalg.norm(v))
    if n <= 1e-12:
        return v
    return v / n


def person_proto(embeddings: list[np.ndarray]) -> np.ndarray | None:
    """Mean of face vectors, then L2-normalize. None if empty."""
    kept = [np.asarray(item, dtype=np.float64).reshape(-1) for item in embeddings if item is not None]
    kept = [item for item in kept if item.size == DIM]
    if not kept:
        return None
    return l2_normalize(np.mean(np.stack(kept, axis=0), axis=0))


def gallery_matrix(
    person_ids: list[int],
    protos: dict[int, np.ndarray],
) -> tuple[np.ndarray, np.ndarray]:
    ids = np.asarray([pid for pid in person_ids if pid in protos], dtype=np.int64)
    if ids.size == 0:
        return ids, np.zeros((0, DIM), dtype=np.float64)
    mat = np.stack([protos[int(pid)] for pid in ids], axis=0)
    return ids, mat


def rank_person_ids(query: np.ndarray, person_ids: np.ndarray, matrix: np.ndarray) -> list[int]:
    """Ascending L2 rank. Unit-norm gallery/query => equivalent to descending cosine."""
    q = l2_normalize(query)
    if matrix.size == 0:
        return []
    # ||q-g||^2 = 2 - 2 q·g
    order = np.argsort(-(matrix @ q))
    return [int(person_ids[i]) for i in order]


def hit_at_k(ranked_ids: list[int], true_id: int, k: int) -> bool:
    return true_id in ranked_ids[: max(0, k)]
