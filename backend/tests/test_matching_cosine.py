import math

from app.matching.service import cosine_similarity


def test_cosine_similarity_identical_vectors_is_one():
    vec = [0.1, 0.2, 0.3, 0.4]
    assert math.isclose(cosine_similarity(vec, vec), 1.0, rel_tol=1e-6)


def test_cosine_similarity_orthogonal_vectors_is_zero():
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    assert math.isclose(cosine_similarity(a, b), 0.0, abs_tol=1e-6)


def test_cosine_similarity_opposite_vectors_is_negative_one():
    a = [1.0, 0.0]
    b = [-1.0, 0.0]
    assert math.isclose(cosine_similarity(a, b), -1.0, rel_tol=1e-6)


def test_cosine_similarity_handles_zero_vector_without_crashing():
    a = [0.0, 0.0]
    b = [1.0, 1.0]
    # Denominator guarded against zero — should not raise, result is well-defined.
    result = cosine_similarity(a, b)
    assert isinstance(result, float)
