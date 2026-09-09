from __future__ import annotations

import numpy as np

from app.faces.l2_recall import (
    gallery_matrix,
    hit_at_k,
    l2_normalize,
    person_proto,
    rank_person_ids,
)


def test_l2_rank_equals_cosine_on_unit_vectors() -> None:
    rng = np.random.default_rng(0)
    true = l2_normalize(rng.normal(size=512))
    other = l2_normalize(rng.normal(size=512))
    query = l2_normalize(0.9 * true + 0.1 * rng.normal(size=512))
    ids, mat = gallery_matrix([7, 3], {7: true, 3: other})
    ranked = rank_person_ids(query, ids, mat)
    assert ranked[0] == 7
    assert hit_at_k(ranked, 7, 1)
    assert hit_at_k(ranked, 7, 5)


def test_leave_one_out_proto_drops_query_face() -> None:
    a = np.zeros(512)
    a[0] = 1.0
    b = np.zeros(512)
    b[1] = 1.0
    full = person_proto([a, a, b])
    leftover = person_proto([b])
    assert full is not None and leftover is not None
    assert float(np.dot(full, a)) > float(np.dot(leftover, a))
