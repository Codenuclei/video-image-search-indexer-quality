from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from app.objects.yoloe_engine import detect_objects_bgr


class _FakeBoxes:
    def __init__(self) -> None:
        self.xyxy = _T(np.array([[10.0, 20.0, 110.0, 80.0]], dtype=np.float32))
        self.conf = _T(np.array([0.91], dtype=np.float32))
        self.cls = _T(np.array([0.0], dtype=np.float32))


class _T:
    def __init__(self, arr: np.ndarray) -> None:
        self._arr = arr

    def cpu(self) -> "_T":
        return self

    def numpy(self) -> np.ndarray:
        return self._arr


def test_yoloe_maps_taxonomy_and_open_vocab(monkeypatch) -> None:
    fake = SimpleNamespace(boxes=_FakeBoxes(), names={0: "tshirt"})

    class _Model:
        def predict(self, **kwargs):  # noqa: ANN003
            assert kwargs["source"].shape[2] == 3
            return [fake]

    monkeypatch.setattr("app.objects.yoloe_engine._model", lambda: _Model())
    dets = detect_objects_bgr(np.zeros((64, 64, 3), dtype=np.uint8))
    assert len(dets) == 1
    assert dets[0].canonical_label == "t-shirt"
    assert dets[0].category == "apparel"
    assert dets[0].bbox_width == 100.0
    assert dets[0].bbox_height == 60.0


def test_yoloe_unknown_class_is_open_vocab(monkeypatch) -> None:
    fake = SimpleNamespace(boxes=_FakeBoxes(), names={0: "fire hydrant"})

    class _Model:
        def predict(self, **kwargs):  # noqa: ANN003
            return [fake]

    monkeypatch.setattr("app.objects.yoloe_engine._model", lambda: _Model())
    dets = detect_objects_bgr(np.zeros((32, 32, 3), dtype=np.uint8))
    assert dets[0].canonical_label == "fire hydrant"
    assert dets[0].category == "open_vocab"
