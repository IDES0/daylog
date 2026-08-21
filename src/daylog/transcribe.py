"""Audio -> text via a local faster-whisper model."""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)

DEFAULT_MODEL_SIZE = "small"


@lru_cache(maxsize=1)
def _load_model(model_size: str) -> WhisperModel:
    logger.info("loading faster-whisper model=%s", model_size)
    return WhisperModel(model_size, device="cpu", compute_type="int8")


def transcribe(audio_path: Path, model_size: str = DEFAULT_MODEL_SIZE) -> str:
    """Transcribe an audio file to text. Blocking; run off the event loop."""
    model = _load_model(model_size)
    segments, info = model.transcribe(str(audio_path))
    text = " ".join(segment.text.strip() for segment in segments)
    logger.info(
        "transcribed audio_path=%s language=%s duration=%.1fs",
        audio_path,
        info.language,
        info.duration,
    )
    return text.strip()
