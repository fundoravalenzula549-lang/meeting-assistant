from __future__ import annotations

import tempfile
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np

from ..models import Language


@dataclass(slots=True)
class ASRSegment:
    start: float
    end: float
    text: str
    speaker: str | None = None


@dataclass(slots=True)
class ASRResult:
    text: str
    segments: list[ASRSegment] = field(default_factory=list)
    language: str = "auto"
    duration: float = 0.0
    processing_time: float = 0.0
    backend: str = ""


class ASRBackend(Protocol):
    async def transcribe_pcm(
        self,
        pcm: np.ndarray,
        sample_rate: int,
        language: Language,
        work_dir: Path,
    ) -> ASRResult:
        ...


def write_wav(path: Path, pcm: np.ndarray, sample_rate: int) -> Path:
    arr = np.asarray(pcm, dtype=np.float32).reshape(-1)
    int16 = (arr * 32767).clip(-32768, 32767).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(int16.tobytes())
    return path


def temp_wav_path(work_dir: Path, prefix: str = "chunk") -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    fh = tempfile.NamedTemporaryFile(prefix=f"{prefix}_", suffix=".wav", dir=work_dir, delete=False)
    fh.close()
    return Path(fh.name)

