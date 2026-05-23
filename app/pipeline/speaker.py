from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..asr.base import ASRSegment
from ..models import SourceKind


@dataclass(slots=True)
class SourceAwareSpeakerAssigner:
    mic_label: str = "Me"
    system_default_label: str = "Speaker 1"

    def assign_realtime(self, source: SourceKind) -> str:
        if source == SourceKind.MIC:
            return self.mic_label
        return self.system_default_label


class OptionalSpeakerDiarizer:
    """Best-effort post-meeting diarization for Speaker 1/2/3 labels.

    This is optional because diarization packages and CPU time are expensive.
    If dependencies are missing, callers can keep source-based labels.
    """

    def diarize(self, wav_path: Path, segments: list[ASRSegment], num_speakers: int | None = None) -> list[str] | None:
        if not wav_path.is_file() or not segments:
            return None
        try:
            import numpy as np
            from resemblyzer import VoiceEncoder, preprocess_wav
            from spectralcluster import SpectralClusterer
        except ImportError:
            return None

        wav = preprocess_wav(str(wav_path))
        encoder = VoiceEncoder("cpu")
        embeddings = []
        valid_indices = []
        sample_rate = 16000
        for idx, seg in enumerate(segments):
            start = max(0, int(seg.start * sample_rate))
            end = max(start, int(seg.end * sample_rate))
            audio = wav[start:end]
            if len(audio) < int(0.35 * sample_rate):
                embeddings.append(None)
                continue
            try:
                emb = encoder.embed_utterance(audio)
            except Exception:
                embeddings.append(None)
                continue
            embeddings.append(emb)
            valid_indices.append(idx)
        if not valid_indices:
            return None
        mat = np.array([embeddings[i] for i in valid_indices])
        clusters = num_speakers if num_speakers and num_speakers > 0 else None
        clusterer = SpectralClusterer(
            min_clusters=clusters or 1,
            max_clusters=clusters or 3,
        )
        labels = clusterer.predict(mat)
        out = ["Speaker 1"] * len(segments)
        order: dict[int, int] = {}
        next_id = 1
        for local_idx, seg_idx in enumerate(valid_indices):
            raw = int(labels[local_idx])
            if raw not in order:
                order[raw] = next_id
                next_id += 1
            out[seg_idx] = f"Speaker {order[raw]}"
        return out

