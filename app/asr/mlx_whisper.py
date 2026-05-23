from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from time import monotonic

import numpy as np

from .base import ASRResult, ASRSegment, temp_wav_path, write_wav
from ..models import Language
from ..text_normalizer import normalize_chinese_text


class MLXWhisperASR:
    def __init__(self, model_name: str = "large-v3-turbo"):
        self.model_name = model_name
        self.repo = _mlx_repo_name(model_name)

    async def warmup(self, language: Language, work_dir: Path) -> None:
        wav_path = temp_wav_path(work_dir, "mlx_warmup")
        write_wav(wav_path, np.zeros(1600, dtype=np.float32), 16000)
        try:
            await asyncio.to_thread(self._transcribe_file, wav_path, language)
        finally:
            try:
                wav_path.unlink()
            except OSError:
                pass

    async def transcribe_pcm(
        self,
        pcm: np.ndarray,
        sample_rate: int,
        language: Language,
        work_dir: Path,
    ) -> ASRResult:
        wav_path = temp_wav_path(work_dir, "mlx_asr")
        write_wav(wav_path, pcm, sample_rate)
        try:
            return await asyncio.to_thread(self._transcribe_file, wav_path, language)
        finally:
            try:
                wav_path.unlink()
            except OSError:
                pass

    def _transcribe_file(self, wav_path: Path, language: Language) -> ASRResult:
        import mlx_whisper

        t0 = monotonic()
        lang = None if language == Language.AUTO else str(language)
        kwargs = {
            "path_or_hf_repo": self.repo,
            "language": lang,
            "word_timestamps": False,
            "condition_on_previous_text": False,
            "temperature": 0,
        }
        if "sample_len" in inspect.signature(mlx_whisper.transcribe).parameters:
            kwargs["sample_len"] = 80
        result = mlx_whisper.transcribe(str(wav_path), **kwargs)
        segments: list[ASRSegment] = []
        texts: list[str] = []
        for seg in result.get("segments", []):
            text = normalize_chinese_text(str(seg.get("text", "")).strip())
            if not text:
                continue
            start = float(seg.get("start", 0.0) or 0.0)
            end = float(seg.get("end", start) or start)
            segments.append(ASRSegment(start=start, end=end, text=text))
            texts.append(text)
        text = normalize_chinese_text(str(result.get("text", " ".join(texts))).strip())
        return ASRResult(
            text=text,
            segments=segments,
            language=str(result.get("language", lang or "auto")),
            duration=float(result.get("duration", 0.0) or 0.0),
            processing_time=monotonic() - t0,
            backend=f"mlx-whisper:{self.model_name}",
        )


def _mlx_repo_name(model_name: str) -> str:
    suffixes = {
        "large-v3-turbo": "",
        "large-v3": "-mlx",
        "medium": "-mlx",
        "small": "-mlx",
        "base": "-mlx",
        "tiny": "-mlx",
    }
    return f"mlx-community/whisper-{model_name}{suffixes.get(model_name, '-mlx')}"
