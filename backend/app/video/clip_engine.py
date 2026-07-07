from __future__ import annotations

import logging
from functools import lru_cache

import numpy as np
from PIL import Image

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

_CLIP_DIM = 512


class CLIPEngine:
    """OpenAI CLIP ViT-B/32 for frame + text moment search."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._model = None
        self._processor = None
        self._device = "cpu"

    def _load(self) -> None:
        if self._model is not None:
            return
        import torch
        from transformers import CLIPModel, CLIPProcessor

        model_name = self._settings.clip_model_name
        logger.info("Loading CLIP model %s on CPU", model_name)
        self._processor = CLIPProcessor.from_pretrained(model_name)
        self._model = CLIPModel.from_pretrained(model_name)
        self._model.eval()
        self._model.to(self._device)
        torch.set_num_threads(max(1, self._settings.cpu_inference_threads))

    def encode_image_bgr(self, image_bgr: np.ndarray) -> list[float]:
        import torch

        self._load()
        rgb = Image.fromarray(image_bgr[:, :, ::-1])
        inputs = self._processor(images=rgb, return_tensors="pt")
        inputs = {k: v.to(self._device) for k, v in inputs.items()}
        with torch.no_grad():
            features = self._model.get_image_features(**inputs)
            features = features / features.norm(dim=-1, keepdim=True)
        return features[0].cpu().tolist()

    def encode_text(self, text: str) -> list[float]:
        import torch

        self._load()
        inputs = self._processor(text=[text], return_tensors="pt", padding=True, truncation=True)
        inputs = {k: v.to(self._device) for k, v in inputs.items()}
        with torch.no_grad():
            features = self._model.get_text_features(**inputs)
            features = features / features.norm(dim=-1, keepdim=True)
        return features[0].cpu().tolist()


@lru_cache
def get_clip_engine() -> CLIPEngine:
    return CLIPEngine()
