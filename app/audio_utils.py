import json
import subprocess
from pathlib import Path

ALLOWED_EXTENSIONS = {".ogg", ".mp3", ".m4a", ".wav", ".webm", ".opus", ".aac", ".flac"}


class AudioValidationError(Exception):
    pass


def get_extension(filename: str) -> str:
    return Path(filename).suffix.lower()


def validate_extension(filename: str) -> None:
    ext = get_extension(filename)
    if ext not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise AudioValidationError(f"Unsupported audio format '{ext or 'unknown'}'. Allowed: {allowed}")


def probe_duration_seconds(audio_path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(audio_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
    except FileNotFoundError as exc:
        raise AudioValidationError("ffmpeg/ffprobe is not installed on the server") from exc
    except subprocess.CalledProcessError as exc:
        raise AudioValidationError("Could not read audio file metadata") from exc

    data = json.loads(result.stdout or "{}")
    duration = float(data.get("format", {}).get("duration", 0) or 0)
    if duration <= 0:
        raise AudioValidationError("Audio duration could not be determined")
    return duration


def convert_to_wav(input_path: Path, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(output_path),
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=120)
    except FileNotFoundError as exc:
        raise AudioValidationError("ffmpeg is not installed on the server") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise AudioValidationError(f"Audio conversion failed: {stderr or 'unknown error'}") from exc

    if not output_path.exists():
        raise AudioValidationError("Audio conversion produced no output file")

    return output_path
