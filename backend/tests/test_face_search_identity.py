"""Reverse face search keeps person-linked clusters as distinct matches."""

from __future__ import annotations

from app.reid.face_search import _identity_key


def test_cluster_key_wins_over_person() -> None:
    # Two clusters linked to the same person must not collapse into one match.
    assert _identity_key(10, person_id=5, cluster_id=9) == ("cluster", 9)
    assert _identity_key(11, person_id=5, cluster_id=12) == ("cluster", 12)
    assert _identity_key(10, person_id=5, cluster_id=9) != _identity_key(
        11, person_id=5, cluster_id=12
    )


def test_person_key_when_cluster_missing() -> None:
    # Named faces whose cluster_id was cleared still group per person.
    assert _identity_key(10, person_id=5, cluster_id=None) == ("person", 5)
    assert _identity_key(11, person_id=5, cluster_id=None) == ("person", 5)


def test_face_key_for_unlinked_faces() -> None:
    assert _identity_key(10, person_id=None, cluster_id=None) == ("face", 10)
    assert _identity_key(11, person_id=None, cluster_id=None) != _identity_key(
        10, person_id=None, cluster_id=None
    )
