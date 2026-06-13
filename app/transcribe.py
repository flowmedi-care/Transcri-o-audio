import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from faster_whisper import WhisperModel

from app.config import Settings, get_settings


@dataclass
class TranscriptionResult:
    text: str
    duration_seconds: float
    processing_time_seconds: float
    model: str


@lru_cache
def _load_model(model_name: str, compute_type: str) -> WhisperModel:
    return WhisperModel(model_name, device="cpu", compute_type=compute_type)


def transcribe_audio(audio_path: Path, settings: Settings | None = None) -> TranscriptionResult:
    settings = settings or get_settings()
    started = time.perf_counter()

    model = _load_model(settings.whisper_model, settings.whisper_compute_type)
    segments, info = model.transcribe(
        str(audio_path),
        language=settings.whisper_language,
        vad_filter=True,
    )

    text = " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()
    processing_time = time.perf_counter() - started

    return TranscriptionResult(
        text=text,
        duration_seconds=float(info.duration or 0),
        processing_time_seconds=processing_time,
        model=settings.whisper_model,
    )
