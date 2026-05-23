from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

from .recorder import mix_tracks
from ..asr.base import write_wav


def decode_audio_to_wav(source_path: Path, wav_path: Path, sample_rate: int) -> tuple[np.ndarray, int]:
    """Decode an imported audio file to mono 16-bit WAV and return float32 PCM."""
    ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    if not Path(ffmpeg).exists() and shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required to import audio files")
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source_path),
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "wav",
        str(wav_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "unknown ffmpeg error").strip()
        raise RuntimeError(f"cannot decode audio file: {detail}")
    return read_wav_mono_float32(wav_path)


def read_wav_mono_float32(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        sample_rate = wf.getframerate()
        channels = wf.getnchannels()
        frames = wf.readframes(wf.getnframes())
    pcm = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1)
    return pcm, sample_rate


def write_mixed_wav(path: Path, sample_rate: int, system_pcm: np.ndarray | None, mic_pcm: np.ndarray | None) -> Path:
    return write_wav(path, mix_tracks(system_pcm, mic_pcm), sample_rate)
