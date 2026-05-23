from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from time import time
from typing import Any


class SourceKind(StrEnum):
    SYSTEM = "system"
    MIC = "mic"
    MIXED = "mixed"


class AudioInputMode(StrEnum):
    LIVE = "live"
    FILE = "file"


class Language(StrEnum):
    AUTO = "auto"
    ZH = "zh"
    EN = "en"
    JA = "ja"


class TranslationDirection(StrEnum):
    NONE = "none"
    EN_TO_ZH = "en2zh"
    ZH_TO_EN = "zh2en"
    JA_TO_ZH = "ja2zh"
    ZH_TO_JA = "zh2ja"


class TaskStatus(StrEnum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(slots=True)
class DeviceInfo:
    id: int | str
    name: str
    channels: int
    sample_rate: int
    kind_hint: SourceKind | None = None


@dataclass(slots=True)
class MeetingSettings:
    title: str = "Untitled Meeting"
    topic: str = ""
    audio_input: AudioInputMode = AudioInputMode.LIVE
    imported_file_name: str = ""
    imported_system_file_name: str = ""
    imported_mic_file_name: str = ""
    language: Language = Language.ZH
    translation: TranslationDirection = TranslationDirection.NONE
    system_device_id: int | str | None = None
    mic_device_id: int | str | None = None
    asr_backend: str = "local"
    local_model: str = "large-v3-turbo"
    remote_url: str = "http://127.0.0.1:8978"
    record: bool = True
    enable_overlay: bool = False
    enable_post_meeting_ai: bool = True
    enable_speaker_diarization: bool = True


@dataclass(slots=True)
class Segment:
    session_id: str
    source: SourceKind
    speaker: str
    start: float
    end: float
    text: str
    language: Language | str = Language.AUTO
    translation: str = ""
    created_at: float = field(default_factory=time)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["source"] = str(self.source)
        data["language"] = str(self.language)
        return data


@dataclass(slots=True)
class SessionInfo:
    id: str
    title: str
    path: Path
    created_at: float
    status: TaskStatus = TaskStatus.IDLE
    settings: MeetingSettings = field(default_factory=MeetingSettings)
    auto_subject: str = ""
    files: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["path"] = str(self.path)
        data["status"] = str(self.status)
        return data


@dataclass(slots=True)
class RuntimeEvent:
    type: str
    session_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "session_id": self.session_id,
            "created_at": self.created_at,
            **self.payload,
        }
