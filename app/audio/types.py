from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

import numpy as np

from ..models import SourceKind


class AudioDependencyError(RuntimeError):
    pass


@dataclass(slots=True)
class AudioChunk:
    source: SourceKind
    pcm: np.ndarray
    sample_rate: int
    started_at: float
    ended_at: float

    @classmethod
    def now(cls, source: SourceKind, pcm: np.ndarray, sample_rate: int, duration: float) -> "AudioChunk":
        end = monotonic()
        return cls(source=source, pcm=pcm, sample_rate=sample_rate, started_at=end - duration, ended_at=end)

