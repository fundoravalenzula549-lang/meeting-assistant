from __future__ import annotations

from .types import AudioDependencyError
from ..models import DeviceInfo, SourceKind


LOOPBACK_HINTS = (
    "blackhole",
    "loopback",
    "monitor",
    "aggregate",
    "multi-output",
    "stereo mix",
    "wasapi",
)

MIC_EXCLUDE_HINTS = ("blackhole", "loopback", "monitor", "aggregate", "stereo mix")


def _sounddevice():
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise AudioDependencyError("sounddevice is required for audio capture") from exc
    return sd


def classify_device(name: str) -> SourceKind | None:
    lowered = name.lower()
    if any(hint in lowered for hint in LOOPBACK_HINTS):
        return SourceKind.SYSTEM
    if not any(hint in lowered for hint in MIC_EXCLUDE_HINTS):
        return SourceKind.MIC
    return None


def list_input_devices() -> list[DeviceInfo]:
    sd = _sounddevice()
    devices: list[DeviceInfo] = []
    for idx, dev in enumerate(sd.query_devices()):
        channels = int(dev.get("max_input_channels", 0))
        if channels <= 0:
            continue
        name = str(dev.get("name", f"Device {idx}"))
        devices.append(
            DeviceInfo(
                id=idx,
                name=name,
                channels=channels,
                sample_rate=int(dev.get("default_samplerate", 16000)),
                kind_hint=classify_device(name),
            )
        )
    return devices


def default_mic_device_id() -> int | None:
    sd = _sounddevice()
    default = sd.default.device[0]
    if default is None or default < 0:
        for dev in list_input_devices():
            if dev.kind_hint == SourceKind.MIC:
                return int(dev.id)
        return None
    return int(default)


def default_system_device_id() -> int | None:
    for dev in list_input_devices():
        if dev.kind_hint == SourceKind.SYSTEM:
            return int(dev.id)
    return None

