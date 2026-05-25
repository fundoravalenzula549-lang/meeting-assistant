from __future__ import annotations

import asyncio
import gc
import re
from pathlib import Path
from time import monotonic

import numpy as np

from .base import ASRResult, ASRSegment
from ..models import Language
from ..text_normalizer import normalize_chinese_text


QWEN3_ASR_MODEL = "Qwen/Qwen3-ASR-0.6B"


class QwenASR:
    warmup_before_live = True

    def __init__(
        self,
        model_name: str = QWEN3_ASR_MODEL,
        max_new_tokens: int = 256,
        context: str = "",
        chunk_seconds: float = 20.0,
    ):
        self.model_name = model_name
        self.max_new_tokens = max_new_tokens
        self.context = context
        self.chunk_seconds = chunk_seconds
        self._model = None
        self._device = ""
        self._lock = asyncio.Lock()

    async def warmup(self, language: Language, work_dir: Path) -> None:
        del language, work_dir
        async with self._lock:
            await asyncio.to_thread(self._load_model)

    async def transcribe_pcm(
        self,
        pcm: np.ndarray,
        sample_rate: int,
        language: Language,
        work_dir: Path,
    ) -> ASRResult:
        del work_dir
        async with self._lock:
            return await asyncio.to_thread(self._transcribe_array, pcm, sample_rate, language)

    def _load_model(self):
        if self._model is not None:
            return self._model

        from qwen_asr import Qwen3ASRModel

        kwargs = _preferred_load_kwargs()
        try:
            self._model = Qwen3ASRModel.from_pretrained(
                self.model_name,
                max_inference_batch_size=1,
                max_new_tokens=self.max_new_tokens,
                **kwargs,
            )
            self._device = str(kwargs.get("device_map", "auto"))
        except Exception:
            if kwargs.get("device_map") == "cpu":
                raise
            self._model = Qwen3ASRModel.from_pretrained(
                self.model_name,
                max_inference_batch_size=1,
                max_new_tokens=self.max_new_tokens,
                **_cpu_load_kwargs(),
            )
            self._device = "cpu"
        return self._model

    def _transcribe_array(self, pcm: np.ndarray, sample_rate: int, language: Language) -> ASRResult:
        try:
            return self._transcribe_array_once(pcm, sample_rate, language)
        except Exception:
            if self._model is None or self._device == "cpu":
                raise
            self._model = None
            self._device = ""
            gc.collect()
            return self._transcribe_array_once(pcm, sample_rate, language)

    def _transcribe_array_once(self, pcm: np.ndarray, sample_rate: int, language: Language) -> ASRResult:
        t0 = monotonic()
        arr = np.asarray(pcm, dtype=np.float32).reshape(-1)
        duration = float(arr.size / sample_rate) if sample_rate else 0.0
        if _is_silent(arr):
            return ASRResult(
                text="",
                segments=[],
                language=str(language),
                duration=duration,
                processing_time=monotonic() - t0,
                backend=f"qwen-asr:{self.model_name}:{self._device or 'auto'}",
            )
        model = self._load_model()
        qwen_language = _qwen_language(language)
        texts: list[str] = []
        segments: list[ASRSegment] = []
        languages: list[str] = []
        for chunk, offset, chunk_duration in _split_pcm(arr, sample_rate, self.chunk_seconds):
            if _is_silent(chunk):
                continue
            result = model.transcribe(
                audio=(chunk, sample_rate),
                context=self.context,
                language=qwen_language,
                return_time_stamps=False,
            )[0]
            text = normalize_chinese_text(str(result.text or "").strip())
            if not text:
                continue
            texts.append(text)
            languages.append(str(result.language or qwen_language or language))
            segments.extend(_text_to_segments(text, offset, offset + chunk_duration))
        text = normalize_chinese_text("".join(texts).strip())
        return ASRResult(
            text=text,
            segments=segments,
            language=_merge_languages(languages) or str(qwen_language or language),
            duration=duration,
            processing_time=monotonic() - t0,
            backend=f"qwen-asr:{self.model_name}:{self._device or 'auto'}",
        )


def is_qwen_asr_model(model_name: str) -> bool:
    normalized = (model_name or "").lower()
    return "qwen3-asr" in normalized


def _qwen_language(language: Language) -> str | None:
    if language == Language.ZH:
        return "Chinese"
    if language == Language.EN:
        return "English"
    if language == Language.JA:
        return "Japanese"
    return None


def _preferred_load_kwargs() -> dict:
    import torch

    if torch.cuda.is_available():
        return {"dtype": torch.float16, "device_map": "cuda:0"}
    if torch.backends.mps.is_available():
        return {"dtype": torch.float16, "device_map": "mps"}
    return _cpu_load_kwargs()


def _cpu_load_kwargs() -> dict:
    import torch

    return {"dtype": torch.float32, "device_map": "cpu"}


def _is_silent(pcm: np.ndarray) -> bool:
    if pcm.size == 0:
        return True
    rms = float(np.sqrt(np.mean(pcm * pcm)))
    return rms < 0.001


def _split_pcm(
    pcm: np.ndarray,
    sample_rate: int,
    chunk_seconds: float,
) -> list[tuple[np.ndarray, float, float]]:
    arr = np.asarray(pcm, dtype=np.float32).reshape(-1)
    if arr.size == 0 or sample_rate <= 0:
        return []
    max_len = max(1, int(chunk_seconds * sample_rate))
    if arr.size <= max_len:
        return [(arr, 0.0, arr.size / sample_rate)]

    chunks: list[tuple[np.ndarray, float, float]] = []
    start = 0
    total_len = arr.size
    expand = int(2.5 * sample_rate)
    win = max(4, int(0.08 * sample_rate))

    while total_len - start > max_len:
        cut = start + max_len
        left = max(start, cut - expand)
        right = min(total_len, cut + expand)
        boundary = cut
        if right - left > win:
            window = np.abs(arr[left:right])
            scores = np.convolve(window, np.ones(win, dtype=np.float32), mode="valid")
            boundary = left + int(np.argmin(scores))
        boundary = max(start + 1, min(boundary, total_len))
        chunk = arr[start:boundary]
        chunks.append((chunk, start / sample_rate, chunk.size / sample_rate))
        start = boundary

    tail = arr[start:total_len]
    if tail.size:
        chunks.append((tail, start / sample_rate, tail.size / sample_rate))
    return chunks


def _text_to_segments(text: str, start: float, end: float) -> list[ASRSegment]:
    parts = _split_text(text)
    if not parts:
        return []
    duration = max(0.1, end - start)
    total_chars = max(1, sum(len(part) for part in parts))
    cursor = start
    segments: list[ASRSegment] = []
    for index, part in enumerate(parts):
        if index == len(parts) - 1:
            part_end = end
        else:
            part_duration = duration * (len(part) / total_chars)
            part_end = min(end, cursor + max(0.4, part_duration))
        segments.append(ASRSegment(start=cursor, end=max(cursor, part_end), text=part))
        cursor = part_end
    return segments


def _split_text(text: str, max_chars: int = 72) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    rough = [item.strip() for item in re.split(r"(?<=[。！？!?；;])", text) if item.strip()]
    parts: list[str] = []
    for item in rough or [text]:
        while len(item) > max_chars:
            split_at = _best_split_index(item, max_chars)
            parts.append(item[:split_at].strip())
            item = item[split_at:].strip()
        if item:
            parts.append(item)
    return parts


def _best_split_index(text: str, max_chars: int) -> int:
    candidates = [text.rfind(mark, 0, max_chars) for mark in ("，", "、", ",", " ")]
    split_at = max(candidates)
    if split_at >= max_chars // 2:
        return split_at + 1
    return max_chars


def _merge_languages(languages: list[str]) -> str:
    seen = []
    for language in languages:
        if language and language not in seen:
            seen.append(language)
    return ",".join(seen)
