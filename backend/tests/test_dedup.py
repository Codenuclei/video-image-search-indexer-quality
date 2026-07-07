from app.faces.engine import DetectedFace
from app.pipelines.dedup import LocalIdentityTracker, passes_quality_filter


def _det(embedding: list[float], confidence: float = 0.9) -> DetectedFace:
    return DetectedFace(
        bbox_x=10,
        bbox_y=10,
        bbox_width=100,
        bbox_height=100,
        confidence=confidence,
        embedding=embedding,
        thumbnail_jpeg=b"",
    )


def test_local_identity_tracker_merges_similar_embeddings():
    tracker = LocalIdentityTracker(similarity_threshold=0.9)
    e1 = [1.0] + [0.0] * 511
    e2 = [0.99, 0.1] + [0.0] * 510
    assert tracker.match(e1) is None
    tracker.register(e1)
    assert tracker.match(e2) is not None


def test_passes_quality_filter_rejects_tiny_faces():
    det = _det([1.0] + [0.0] * 511)
    det = DetectedFace(
        bbox_x=0,
        bbox_y=0,
        bbox_width=5,
        bbox_height=5,
        confidence=0.9,
        embedding=det.embedding,
        thumbnail_jpeg=b"",
    )
    assert passes_quality_filter(det, 1000, 1000, min_area_fraction=0.01) is False
    assert passes_quality_filter(det, 1000, 1000, min_area_fraction=0.00001) is True
