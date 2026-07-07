import numpy as np

from app.faces.engine import _resolve_providers, _safe_crop


def test_safe_crop_normal_box_returns_expected_region():
    image = np.zeros((100, 200, 3), dtype=np.uint8)
    image[10:20, 30:50] = 255

    crop = _safe_crop(image, 30, 10, 50, 20)

    assert crop.shape == (10, 20, 3)
    assert (crop == 255).all()


def test_safe_crop_clamps_out_of_bounds_coordinates():
    image = np.zeros((50, 50, 3), dtype=np.uint8)

    crop = _safe_crop(image, -10, -10, 1000, 1000)

    assert crop.shape == (50, 50, 3)


def test_safe_crop_degenerate_box_returns_single_pixel_fallback():
    image = np.zeros((50, 50, 3), dtype=np.uint8)

    crop = _safe_crop(image, 30, 30, 30, 30)

    assert crop.shape == (1, 1, 3)


def test_resolve_providers_falls_back_to_cpu_when_none_requested_available(monkeypatch):
    import types

    fake_ort = types.SimpleNamespace(get_available_providers=lambda: ["CPUExecutionProvider"])
    monkeypatch.setitem(__import__("sys").modules, "onnxruntime", fake_ort)

    result = _resolve_providers(["TensorrtExecutionProvider"])

    assert result == ["CPUExecutionProvider"]


def test_resolve_providers_prefers_cuda_when_available(monkeypatch):
    import types

    fake_ort = types.SimpleNamespace(
        get_available_providers=lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"]
    )
    monkeypatch.setitem(__import__("sys").modules, "onnxruntime", fake_ort)

    result = _resolve_providers(["CPUExecutionProvider"])

    assert result == ["CUDAExecutionProvider", "CPUExecutionProvider"]
