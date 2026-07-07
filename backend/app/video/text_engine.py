from __future__ import annotations

import logging
from functools import lru_cache

from app.config import Settings, get_settings
from app.pipelines.async_cpu import run_cpu_bound

logger = logging.getLogger(__name__)

_TEXT_DIM = 384


class TranscriptEmbedder:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        from sentence_transformers import SentenceTransformer

        name = self._settings.transcript_embedding_model
        logger.info("Loading transcript embedder %s", name)
        self._model = SentenceTransformer(name)
        return self._model

    def encode(self, text: str) -> list[float]:
        model = self._load()
        vec = model.encode(text, normalize_embeddings=True)
        return vec.tolist()

    def encode_batch(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        vecs = model.encode(texts, normalize_embeddings=True)
        return [v.tolist() for v in vecs]


@lru_cache
def get_transcript_embedder() -> TranscriptEmbedder:
    return TranscriptEmbedder()


async def embed_texts_async(texts: list[str], settings: Settings | None = None) -> list[list[float]]:
    embedder = TranscriptEmbedder(settings)
    return await run_cpu_bound(embedder.encode_batch, texts)
