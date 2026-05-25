from __future__ import annotations

import asyncio
import re
from fractions import Fraction
from pathlib import Path
from time import monotonic

import numpy as np

from .base import ASRResult, ASRSegment
from ..models import Language, SourceKind
from ..text_normalizer import normalize_chinese_text


PARAFORMER_ZH_STREAMING_MODEL = "funasr/paraformer-zh-streaming"
_TARGET_SAMPLE_RATE = 16000
_STREAM_CHUNK_SIZE = [0, 10, 5]
_STREAM_CHUNK_STRIDE = _STREAM_CHUNK_SIZE[1] * 960
_ENCODER_CHUNK_LOOK_BACK = 4
_DECODER_CHUNK_LOOK_BACK = 1


class ParaformerStreamingASR:
    warmup_before_live = True

    def __init__(
        self,
        model_name: str = PARAFORMER_ZH_STREAMING_MODEL,
        device: str = "cpu",
    ):
        self.model_name = model_name
        self.device = device
        self._model = None
        self._lock = asyncio.Lock()
        self._stream_caches: dict[str, dict] = {}

    async def warmup(self, language: Language, work_dir: Path) -> None:
        del language, work_dir
        async with self._lock:
            await asyncio.to_thread(self._load_model)

    async def transcribe_source_pcm(
        self,
        source: SourceKind,
        pcm: np.ndarray,
        sample_rate: int,
        language: Language,
        work_dir: Path,
    ) -> ASRResult:
        del work_dir
        async with self._lock:
            cache = self._stream_caches.setdefault(str(source), {})
            return await asyncio.to_thread(
                self._transcribe_stream,
                pcm,
                sample_rate,
                language,
                cache,
                False,
            )

    async def transcribe_pcm(
        self,
        pcm: np.ndarray,
        sample_rate: int,
        language: Language,
        work_dir: Path,
    ) -> ASRResult:
        del work_dir
        async with self._lock:
            return await asyncio.to_thread(
                self._transcribe_stream,
                pcm,
                sample_rate,
                language,
                {},
                True,
            )

    def _load_model(self):
        if self._model is not None:
            return self._model

        from funasr import AutoModel

        model_path = _resolve_model_path(self.model_name)
        self._model = AutoModel(
            model=str(model_path),
            disable_update=True,
            device=self.device,
        )
        return self._model

    def _transcribe_stream(
        self,
        pcm: np.ndarray,
        sample_rate: int,
        language: Language,
        cache: dict,
        finalize: bool,
    ) -> ASRResult:
        t0 = monotonic()
        arr = _prepare_pcm(pcm, sample_rate)
        duration = float(arr.size / _TARGET_SAMPLE_RATE) if _TARGET_SAMPLE_RATE else 0.0
        if _is_silent(arr):
            return ASRResult(
                text="",
                segments=[],
                language=_result_language(language),
                duration=duration,
                processing_time=monotonic() - t0,
                backend=self._backend_name(),
            )

        model = self._load_model()
        text_parts: list[str] = []
        first_text_offset: float | None = None
        last_text_end = 0.0
        chunks = _iter_stream_chunks(arr)
        for index, (offset, chunk) in enumerate(chunks):
            is_final = finalize and index == len(chunks) - 1
            result = model.generate(
                input=chunk,
                cache=cache,
                is_final=is_final,
                chunk_size=_STREAM_CHUNK_SIZE,
                encoder_chunk_look_back=_ENCODER_CHUNK_LOOK_BACK,
                decoder_chunk_look_back=_DECODER_CHUNK_LOOK_BACK,
                disable_pbar=True,
            )
            text = normalize_chinese_text(_extract_text(result))
            if not text:
                continue
            text_parts.append(text)
            chunk_end = min(duration, offset + float(chunk.size / _TARGET_SAMPLE_RATE))
            if first_text_offset is None:
                first_text_offset = offset
            last_text_end = max(last_text_end, chunk_end)

        text = normalize_chinese_text("".join(text_parts).strip())
        segments = _text_to_segments(text, first_text_offset or 0.0, last_text_end or duration)
        return ASRResult(
            text=text,
            segments=segments,
            language=_result_language(language),
            duration=duration,
            processing_time=monotonic() - t0,
            backend=self._backend_name(),
        )

    def _backend_name(self) -> str:
        return f"funasr-paraformer:{self.model_name}:streaming:{self.device}"


def is_paraformer_model(model_name: str) -> bool:
    return "paraformer" in (model_name or "").lower()


def _resolve_model_path(model_name: str) -> str:
    from huggingface_hub import snapshot_download

    try:
        return snapshot_download(model_name, local_files_only=True)
    except Exception:
        return model_name


def _prepare_pcm(pcm: np.ndarray, sample_rate: int) -> np.ndarray:
    arr = np.asarray(pcm, dtype=np.float32).reshape(-1)
    if sample_rate == _TARGET_SAMPLE_RATE or arr.size == 0:
        return arr
    if sample_rate <= 0:
        return arr
    from scipy.signal import resample_poly

    ratio = Fraction(_TARGET_SAMPLE_RATE, sample_rate).limit_denominator(1000)
    resampled = resample_poly(arr, ratio.numerator, ratio.denominator)
    return np.asarray(resampled, dtype=np.float32).reshape(-1)


def _iter_stream_chunks(pcm: np.ndarray) -> list[tuple[float, np.ndarray]]:
    chunks: list[tuple[float, np.ndarray]] = []
    if pcm.size == 0:
        return chunks
    for start in range(0, pcm.size, _STREAM_CHUNK_STRIDE):
        chunk = pcm[start : start + _STREAM_CHUNK_STRIDE]
        if chunk.size:
            chunks.append((start / _TARGET_SAMPLE_RATE, chunk))
    return chunks


def _extract_text(result) -> str:
    if not result:
        return ""
    if isinstance(result, list):
        return "".join(_extract_text(item) for item in result)
    if isinstance(result, dict):
        return str(result.get("text") or result.get("value") or "")
    return str(getattr(result, "text", "") or "")


def _result_language(language: Language) -> str:
    if language == Language.AUTO:
        return "zh"
    return str(language)


def _is_silent(pcm: np.ndarray) -> bool:
    if pcm.size == 0:
        return True
    rms = float(np.sqrt(np.mean(pcm * pcm)))
    return rms < 0.001


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
            part_end = min(end, cursor + max(0.25, part_duration))
        segments.append(ASRSegment(start=cursor, end=max(cursor, part_end), text=part))
        cursor = part_end
    return segments


def _split_text(text: str, max_chars: int = 48) -> list[str]:
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
