from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from time import monotonic

import numpy as np

from .devices import default_mic_device_id, default_system_device_id, list_input_devices
from .recorder import to_mono_float32
from .types import AudioChunk
from ..models import SourceKind


@dataclass(slots=True)
class CaptureConfig:
    sample_rate: int = 16000
    window_seconds: float = 6.0
    hop_seconds: float = 3.0
    system_device_id: int | str | None = None
    mic_device_id: int | str | None = None


class RingBuffer:
    def __init__(self, sample_rate: int, seconds: float):
        self.sample_rate = sample_rate
        self.size = max(int(sample_rate * seconds), sample_rate)
        self._data = np.zeros(self.size, dtype=np.float32)
        self._write = 0
        self._filled = 0

    def write(self, pcm: np.ndarray) -> None:
        arr = to_mono_float32(pcm)
        if len(arr) >= self.size:
            arr = arr[-self.size :]
        n = len(arr)
        end = self._write + n
        if end <= self.size:
            self._data[self._write : end] = arr
        else:
            first = self.size - self._write
            self._data[self._write :] = arr[:first]
            self._data[: n - first] = arr[first:]
        self._write = (self._write + n) % self.size
        self._filled = min(self._filled + n, self.size)

    def ready(self) -> bool:
        return self._filled >= self.size

    def snapshot(self) -> np.ndarray:
        if self._filled < self.size:
            return self._data[: self._filled].copy()
        return np.roll(self._data.copy(), -self._write)


class DualAudioCapture:
    def __init__(self, config: CaptureConfig):
        self.config = config
        self._streams = []
        self._queue: asyncio.Queue[AudioChunk | None] = asyncio.Queue(maxsize=50)
        self._raw_queue: asyncio.Queue[tuple[SourceKind, np.ndarray] | None] = asyncio.Queue(maxsize=200)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._buffers = {
            SourceKind.SYSTEM: RingBuffer(config.sample_rate, config.window_seconds),
            SourceKind.MIC: RingBuffer(config.sample_rate, config.window_seconds),
        }
        self._muted: set[SourceKind] = set()
        self._last_emit = {SourceKind.SYSTEM: 0.0, SourceKind.MIC: 0.0}
        self._running = False

    def set_muted(self, source: SourceKind, muted: bool) -> None:
        if muted:
            self._muted.add(source)
        else:
            self._muted.discard(source)

    async def start(self) -> None:
        import sounddevice as sd

        self._loop = asyncio.get_running_loop()
        system_id = self.config.system_device_id
        mic_id = self.config.mic_device_id
        try:
            devices = list_input_devices()
        except Exception:
            devices = []
        valid_ids = {str(dev.id) for dev in devices}
        if system_id not in (None, "") and str(system_id) not in valid_ids:
            system_id = None
        if mic_id not in (None, "") and str(mic_id) not in valid_ids:
            mic_id = None
        if system_id in (None, ""):
            system = next((dev for dev in devices if dev.kind_hint == SourceKind.SYSTEM), None)
            system_id = system.id if system else default_system_device_id()
        if mic_id in (None, ""):
            mic = next(
                (
                    dev
                    for dev in devices
                    if dev.kind_hint == SourceKind.MIC
                    and (system_id in (None, "") or str(dev.id) != str(system_id))
                ),
                None,
            )
            mic_id = mic.id if mic else default_mic_device_id()
        if system_id is None and mic_id is None:
            raise RuntimeError("no input audio devices found")

        self._running = True
        if system_id is not None:
            self._streams.append(
                sd.InputStream(
                    device=int(system_id),
                    samplerate=self.config.sample_rate,
                    channels=1,
                    dtype="float32",
                    callback=self._callback(SourceKind.SYSTEM),
                )
            )
        if mic_id is not None:
            self._streams.append(
                sd.InputStream(
                    device=int(mic_id),
                    samplerate=self.config.sample_rate,
                    channels=1,
                    dtype="float32",
                    callback=self._callback(SourceKind.MIC),
                )
            )
        for stream in self._streams:
            stream.start()

    async def stop(self) -> None:
        self._running = False
        for stream in self._streams:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        self._streams.clear()
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._wake_chunks)
            self._loop.call_soon_threadsafe(self._wake_raw_frames)

    async def chunks(self) -> AsyncIterator[AudioChunk]:
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                break
            yield chunk

    async def raw_frames(self) -> AsyncIterator[tuple[SourceKind, np.ndarray]]:
        while True:
            item = await self._raw_queue.get()
            if item is None:
                break
            yield item

    def _callback(self, source: SourceKind):
        def callback(indata, frames, time_info, status):
            if not self._running or source in self._muted:
                return
            pcm = to_mono_float32(indata).copy()
            buffer = self._buffers[source]
            buffer.write(pcm)
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._put_raw_frame, source, pcm)
            now = monotonic()
            if not buffer.ready() or now - self._last_emit[source] < self.config.hop_seconds:
                return
            self._last_emit[source] = now
            pcm = buffer.snapshot()
            duration = len(pcm) / float(self.config.sample_rate)
            chunk = AudioChunk.now(source, pcm, self.config.sample_rate, duration)
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._put_chunk, chunk)

        return callback

    def _put_chunk(self, chunk: AudioChunk) -> None:
        try:
            self._queue.put_nowait(chunk)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(chunk)
            except asyncio.QueueFull:
                pass

    def _put_raw_frame(self, source: SourceKind, pcm: np.ndarray) -> None:
        try:
            self._raw_queue.put_nowait((source, pcm))
        except asyncio.QueueFull:
            try:
                self._raw_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._raw_queue.put_nowait((source, pcm))
            except asyncio.QueueFull:
                pass

    def _wake_chunks(self) -> None:
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    def _wake_raw_frames(self) -> None:
        try:
            self._raw_queue.put_nowait(None)
        except asyncio.QueueFull:
            try:
                self._raw_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._raw_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
