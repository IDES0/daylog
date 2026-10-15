"""Audio -> text via a local faster-whisper model.

The model is loaded on the first voice note and dropped after
IDLE_UNLOAD_SECONDS without one, so the container doesn't hold ~0.5 GB
of weights around the clock (Railway bills memory by the minute).
faster_whisper itself is imported lazily for the same reason.
"""

from __future__ import annotations

import contextlib
import ctypes
import gc
import logging
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)

DEFAULT_MODEL_SIZE = "small"
IDLE_UNLOAD_SECONDS = 300.0

_lock = threading.Lock()
_model: WhisperModel | None = None
_model_size: str | None = None
_in_use = 0
_unload_timer: threading.Timer | None = None


def _acquire(model_size: str) -> WhisperModel:
    global _model, _model_size, _in_use, _unload_timer
    with _lock:
        if _unload_timer is not None:
            _unload_timer.cancel()
            _unload_timer = None
        if _model is None or _model_size != model_size:
            from faster_whisper import WhisperModel

            logger.info("loading faster-whisper model=%s", model_size)
            _model = WhisperModel(model_size, device="cpu", compute_type="int8")
            _model_size = model_size
        _in_use += 1
        return _model


def _release() -> None:
    global _in_use, _unload_timer
    with _lock:
        _in_use -= 1
        if _in_use == 0:
            _unload_timer = threading.Timer(IDLE_UNLOAD_SECONDS, unload)
            _unload_timer.daemon = True
            _unload_timer.start()


def unload() -> None:
    """Drop the model and hand its memory back to the OS, unless in use."""
    global _model, _model_size, _unload_timer
    with _lock:
        _unload_timer = None
        if _model is None or _in_use > 0:
            return
        _model = None
        _model_size = None
    gc.collect()
    _trim_heap()
    logger.info("unloaded faster-whisper model after idle")


def is_loaded() -> bool:
    return _model is not None


def _trim_heap() -> None:
    # glibc keeps freed memory in the process unless asked to return it.
    if not sys.platform.startswith("linux"):
        return
    with contextlib.suppress(OSError, AttributeError):
        ctypes.CDLL("libc.so.6").malloc_trim(0)


def transcribe(audio_path: Path, model_size: str = DEFAULT_MODEL_SIZE) -> str:
    """Transcribe an audio file to text. Blocking; run off the event loop."""
    model = _acquire(model_size)
    try:
        segments, info = model.transcribe(str(audio_path))
        text = " ".join(segment.text.strip() for segment in segments)
    finally:
        _release()
    logger.info(
        "transcribed audio_path=%s language=%s duration=%.1fs",
        audio_path,
        info.language,
        info.duration,
    )
    return text.strip()
