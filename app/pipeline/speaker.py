from __future__ import annotations

from dataclasses import dataclass

from ..models import SourceKind


@dataclass(slots=True)
class SourceAwareSpeakerAssigner:
    mic_label: str = "Me"
    system_default_label: str = "Speaker 1"

    def assign_realtime(self, source: SourceKind) -> str:
        if source == SourceKind.MIC:
            return self.mic_label
        return self.system_default_label
