from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from app.faces.engine import DetectedFace


@dataclass
class _TrackedFace:
    embedding: np.ndarray
    hits: int = 1

    def update(self, embedding: list[float]) -> None:
        self.embedding = (self.embedding + np.asarray(embedding, dtype=np.float32)) / 2.0
        self.hits += 1


@dataclass
class LocalIdentityTracker:
    """Deduplicate repeated detections of the same person within one image."""

    similarity_threshold: float
    _tracks: list[_TrackedFace] = field(default_factory=list)

    def match(self, embedding: list[float]) -> _TrackedFace | None:
        vec = np.asarray(embedding, dtype=np.float32)
        best: _TrackedFace | None = None
        best_sim = -1.0
        for track in self._tracks:
            denom = (np.linalg.norm(vec) * np.linalg.norm(track.embedding)) or 1e-8
            sim = float(np.dot(vec, track.embedding) / denom)
            if sim > best_sim:
                best_sim = sim
                best = track
        if best is not None and best_sim >= self.similarity_threshold:
            return best
        return None

    def register(self, embedding: list[float]) -> _TrackedFace:
        track = _TrackedFace(embedding=np.asarray(embedding, dtype=np.float32))
        self._tracks.append(track)
        return track


def passes_quality_filter(
    detection: DetectedFace,
    image_width: int,
    image_height: int,
    min_area_fraction: float,
) -> bool:
    area = detection.bbox_width * detection.bbox_height
    image_area = max(image_width * image_height, 1)
    return area / image_area >= min_area_fraction
