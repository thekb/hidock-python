"""Optional local speech recognition, with no dependency for USB-only use."""

import os
from pathlib import Path
import tempfile
from typing import Protocol

from .errors import HiDockError


class TranscriptionError(HiDockError):
    """Audio was retained, but speech recognition could not complete."""


class Transcriber(Protocol):
    def transcribe(self, audio: str | Path, destination: str | Path | None = None,
                   *, overwrite: bool = False) -> Path: ...


class WhisperTranscriber:
    """Reuse one local CPU/int8 Whisper model across multiple recordings.

    Named models download on first use. Pass a local model directory and
    ``local_files_only=True`` for offline operation. Instances are intended
    for sequential use. Language is detected unless explicitly specified.
    """

    def __init__(self, model: str = "base", *, language: str | None = None,
                 cache_dir: str | Path | None = None, local_files_only: bool = False):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise TranscriptionError(
                'Install transcription support with: python -m pip install "hidock-python[transcription]"'
            ) from exc
        self._factory = WhisperModel
        self._model = None
        self.model_name = model
        self.language = language
        self.cache_dir = str(cache_dir) if cache_dir is not None else None
        self.local_files_only = local_files_only

    def transcribe(self, audio: str | Path, destination: str | Path | None = None,
                   *, overwrite: bool = False) -> Path:
        """Write UTF-8 text atomically; failures never remove the source audio."""
        audio = Path(audio)
        target = Path(destination) if destination is not None else audio.with_suffix(".txt")
        if not audio.is_file():
            raise FileNotFoundError(audio)
        if target.resolve() == audio.resolve():
            raise ValueError("Transcript destination must differ from the audio file")
        if target.exists():
            if os.path.samefile(audio, target):
                raise ValueError("Transcript destination must differ from the audio file")
            if not overwrite:
                raise FileExistsError(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".part", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                try:
                    if self._model is None:
                        self._model = self._factory(
                            self.model_name, device="cpu", compute_type="int8",
                            download_root=self.cache_dir, local_files_only=self.local_files_only,
                        )
                    segments, info = self._model.transcribe(
                        str(audio), language=self.language, vad_filter=True,
                    )
                    if info.duration <= 0:
                        raise ValueError("Audio contains no decoded samples")
                    # Consume the generator: inference errors can occur during iteration.
                    for segment in segments:
                        text = segment.text.strip()
                        if text:
                            output.write(text + "\n")
                except Exception as exc:
                    raise TranscriptionError(
                        f"Transcription failed for {audio}; audio retained: {exc}"
                    ) from exc
                output.flush()
                os.fsync(output.fileno())
            if overwrite:
                os.replace(temporary, target)
            else:
                os.link(temporary, target)
            return target
        finally:
            Path(temporary).unlink(missing_ok=True)
