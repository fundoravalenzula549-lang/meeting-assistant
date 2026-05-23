from __future__ import annotations

import asyncio
from pathlib import Path
from time import monotonic

import numpy as np

from .base import ASRResult, ASRSegment, temp_wav_path, write_wav
from ..models import Language
from ..text_normalizer import normalize_chinese_text


class LocalFasterWhisperASR:
    def __init__(
        self,
        model_name: str = "large-v3-turbo",
        device: str = "auto",
        compute_type: str = "int8",
        beam_size: int = 1,
    ):
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.beam_size = beam_size
        self._model = None
        self._lock = asyncio.Lock()

    async def transcribe_pcm(
        self,
        pcm: np.ndarray,
        sample_rate: int,
        language: Language,
        work_dir: Path,
    ) -> ASRResult:
        wav_path = temp_wav_path(work_dir, "local_asr")
        write_wav(wav_path, pcm, sample_rate)
        try:
            return await asyncio.to_thread(self._transcribe_file, wav_path, language)
        finally:
            try:
                wav_path.unlink()
            except OSError:
                pass

    async def transcribe_file(self, wav_path: Path, language: Language) -> ASRResult:
        return await asyncio.to_thread(self._transcribe_file, wav_path, language)

    def _load_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type,
            )
        return self._model

    def _transcribe_file(self, wav_path: Path, language: Language) -> ASRResult:
        model = self._load_model()
        t0 = monotonic()
        lang = None if language == Language.AUTO else str(language)
        segments_iter, info = model.transcribe(
            str(wav_path),
            language=lang,
            beam_size=self.beam_size,
            best_of=1,
            temperature=0,
            vad_filter=True,
            condition_on_previous_text=False,
            max_new_tokens=48,
        )
        segments: list[ASRSegment] = []
        texts: list[str] = []
        for seg in segments_iter:
            text = normalize_chinese_text(seg.text.strip())
            if not text:
                continue
            segments.append(ASRSegment(start=float(seg.start), end=float(seg.end), text=text))
            texts.append(text)
        return ASRResult(
            text=" ".join(texts),
            segments=segments,
            language=getattr(info, "language", str(language)),
            duration=float(getattr(info, "duration", 0.0) or 0.0),
            processing_time=monotonic() - t0,
            backend=f"faster-whisper:{self.model_name}",
        )
