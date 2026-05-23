from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from ..models import SourceKind


class WavTrackWriter:
    def __init__(self, path: Path, sample_rate: int):
        self.path = path
        self.sample_rate = sample_rate
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._wave = wave.open(str(path), "wb")
        self._wave.setnchannels(1)
        self._wave.setsampwidth(2)
        self._wave.setframerate(sample_rate)
        self._closed = False

    def write(self, pcm: np.ndarray) -> None:
        if self._closed:
            return
        mono = to_mono_float32(pcm)
        int16 = (mono * 32767).clip(-32768, 32767).astype(np.int16)
        self._wave.writeframes(int16.tobytes())

    def close(self) -> Path:
        if not self._closed:
            self._wave.close()
            self._closed = True
        return self.path


class MeetingRecorder:
    def __init__(self, session_dir: Path, sample_rate: int):
        rec_dir = session_dir / "recordings"
        self.sample_rate = sample_rate
        self.system = WavTrackWriter(rec_dir / "system.wav", sample_rate)
        self.mic = WavTrackWriter(rec_dir / "mic.wav", sample_rate)
        self.mixed_path = rec_dir / "mixed.wav"
        self._system_chunks: list[np.ndarray] = []
        self._mic_chunks: list[np.ndarray] = []

    def write(self, source: SourceKind, pcm: np.ndarray) -> None:
        mono = to_mono_float32(pcm)
        if source == SourceKind.SYSTEM:
            self.system.write(mono)
            self._system_chunks.append(mono.copy())
        elif source == SourceKind.MIC:
            self.mic.write(mono)
            self._mic_chunks.append(mono.copy())

    def close(self) -> dict[str, str]:
        system_path = self.system.close()
        mic_path = self.mic.close()
        system_track = _concat_chunks(self._system_chunks)
        mic_track = _concat_chunks(self._mic_chunks)
        mixed = mix_tracks(system_track, mic_track)
        mixed_writer = WavTrackWriter(self.mixed_path, self.sample_rate)
        mixed_writer.write(mixed)
        mixed_path = mixed_writer.close()
        return {
            "recordings/system.wav": str(system_path),
            "recordings/mic.wav": str(mic_path),
            "recordings/mixed.wav": str(mixed_path),
        }


def to_mono_float32(pcm: np.ndarray) -> np.ndarray:
    arr = np.asarray(pcm, dtype=np.float32)
    if arr.ndim == 2:
        arr = arr.mean(axis=1)
    return arr.reshape(-1)


def mix_tracks(a: np.ndarray | None, b: np.ndarray | None) -> np.ndarray:
    if a is None and b is None:
        return np.zeros(0, dtype=np.float32)
    if a is None:
        return to_mono_float32(b) * 0.85
    if b is None:
        return to_mono_float32(a) * 0.85
    aa = to_mono_float32(a)
    bb = to_mono_float32(b)
    n = max(len(aa), len(bb))
    out = np.zeros(n, dtype=np.float32)
    out[: len(aa)] += aa * 0.7
    out[: len(bb)] += bb * 0.7
    return out.clip(-1.0, 1.0)


def _concat_chunks(chunks: list[np.ndarray]) -> np.ndarray | None:
    if not chunks:
        return None
    return np.concatenate(chunks).astype(np.float32, copy=False)
