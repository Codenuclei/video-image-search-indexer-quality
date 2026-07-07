from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache

from app.config import Settings, get_settings
from app.pipelines.async_cpu import run_cpu_bound

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WhisperSegment:
    start_sec: float
    end_sec: float
    text: str


class WhisperEngine:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        from faster_whisper import WhisperModel

        logger.info("Loading Whisper model %s (CPU int8)", self._settings.whisper_model_size)
        self._model = WhisperModel(
            self._settings.whisper_model_size,
            device="cpu",
            compute_type="int8",
        )
        return self._model

    def transcribe(self, wav_path: str) -> list[WhisperSegment]:
        model = self._load()
        segments, _ = model.transcribe(
            wav_path,
            beam_size=1,
            vad_filter=True,
        )
        out: list[WhisperSegment] = []
        for seg in segments:
            text = (seg.text or "").strip()
            if not text:
                continue
            out.append(WhisperSegment(start_sec=float(seg.start), end_sec=float(seg.end), text=text))
        return out


@lru_cache
def get_whisper_engine() -> WhisperEngine:
    return WhisperEngine()


async def transcribe_audio_async(wav_path: str, settings: Settings | None = None) -> list[WhisperSegment]:
    engine = WhisperEngine(settings)
    return await run_cpu_bound(engine.transcribe, wav_path)
