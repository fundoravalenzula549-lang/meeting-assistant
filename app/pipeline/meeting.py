from __future__ import annotations

import asyncio
from contextlib import suppress
from collections import deque
from difflib import SequenceMatcher
import importlib.util
import platform
import shutil
import wave
from time import monotonic
from pathlib import Path

from ..artifacts import transcript_export_path
from ..asr.base import ASRBackend
from ..asr.local_faster import LocalFasterWhisperASR
from ..asr.mlx_whisper import MLXWhisperASR
from ..asr.remote import RemoteASRClient
from ..audio.capture import CaptureConfig, DualAudioCapture
from ..audio.importer import decode_audio_to_wav, write_mixed_wav
from ..audio.recorder import MeetingRecorder
from ..config import AppConfig
from ..events import EventBus
from ..models import (
    AudioInputMode,
    Language,
    MeetingSettings,
    RuntimeEvent,
    Segment,
    SourceKind,
    TaskStatus,
    TranslationDirection,
)
from ..text_normalizer import normalize_chinese_text
from ..sessions import SessionStore
from ..translation.base import NullTranslator, Translator
from ..translation.llm import LLMClient, LLMTranslator, MeetingPostProcessor
from .postprocess import run_post_meeting_ai, write_pending_notes
from .speaker import SourceAwareSpeakerAssigner


class MeetingTask:
    def __init__(
        self,
        config: AppConfig,
        store: SessionStore,
        events: EventBus,
        settings: MeetingSettings,
        import_audio_paths: dict[SourceKind, Path] | None = None,
    ):
        self.config = config
        self.store = store
        self.events = events
        self.info = store.create(settings)
        self.settings = settings
        self.import_audio_paths = import_audio_paths or {}
        self.status = TaskStatus.STARTING
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._paused = asyncio.Event()
        self._muted: set[SourceKind] = set()
        self._capture: DualAudioCapture | None = None
        self._recorder: MeetingRecorder | None = None
        self._recording_task: asyncio.Task | None = None
        self._started_at = monotonic()
        self._asr_semaphore = asyncio.Semaphore(2)
        self._speaker = SourceAwareSpeakerAssigner()
        self._chunk_tasks: set[asyncio.Task] = set()
        self._background_tasks: set[asyncio.Task] = set()
        self._source_busy: set[SourceKind] = set()
        self._recent_text: dict[SourceKind, deque[str]] = {
            SourceKind.SYSTEM: deque(maxlen=8),
            SourceKind.MIC: deque(maxlen=8),
            SourceKind.MIXED: deque(maxlen=8),
        }

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("meeting task already started")
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self.status = TaskStatus.STOPPING
        self._stop.set()
        if self._capture:
            await self._capture.stop()
        if self._task:
            with suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(self._task), timeout=30)

    def pause(self, paused: bool) -> None:
        if paused:
            self._paused.set()
            self.status = TaskStatus.PAUSED
        else:
            self._paused.clear()
            self.status = TaskStatus.RUNNING

    def mute(self, source: SourceKind, muted: bool) -> None:
        if muted:
            self._muted.add(source)
        else:
            self._muted.discard(source)
        if self._capture:
            self._capture.set_muted(source, muted)

    def snapshot(self) -> dict:
        data = self.info.to_dict()
        data["status"] = str(self.status)
        data["muted"] = [str(s) for s in self._muted]
        data["paused"] = self._paused.is_set()
        return data

    async def _run(self) -> None:
        try:
            await self._publish("status", status=TaskStatus.STARTING, message="starting")
            asr = self._build_asr()
            await self._warmup_asr(asr)
            translator = self._build_translator()
            post_processor = self._build_post_processor()
            if self.settings.audio_input == AudioInputMode.FILE:
                await self._run_imported_audio(asr, translator, post_processor)
                return
            self._capture = DualAudioCapture(
                CaptureConfig(
                    sample_rate=self.config.audio.sample_rate,
                    window_seconds=self.config.audio.window_seconds,
                    hop_seconds=self.config.audio.hop_seconds,
                    system_device_id=self.settings.system_device_id,
                    mic_device_id=self.settings.mic_device_id,
                )
            )
            if self.settings.record:
                self._recorder = MeetingRecorder(self.info.path, self.config.audio.sample_rate)
            await self._capture.start()
            if self._recorder:
                self._recording_task = asyncio.create_task(self._record_frames())
            self.status = TaskStatus.RUNNING
            self.info.status = self.status
            self.store.save_info(self.info)
            await self._publish("started", session=self.info.to_dict())
            async for chunk in self._capture.chunks():
                if self._stop.is_set():
                    break
                if self._paused.is_set():
                    continue
                if chunk.source in self._muted:
                    continue
                if chunk.source in self._source_busy:
                    continue
                self._source_busy.add(chunk.source)
                self._track_chunk_task(
                    asyncio.create_task(self._process_chunk(asr, translator, chunk)),
                    chunk.source,
                )
            await self._finish(asr, post_processor)
        except Exception as exc:
            self.status = TaskStatus.FAILED
            self.info.status = self.status
            self.store.save_info(self.info)
            await self._publish("error", message=str(exc))

    async def _run_imported_audio(
        self,
        asr: ASRBackend,
        translator: Translator,
        post_processor: MeetingPostProcessor | None,
    ) -> None:
        if not self.import_audio_paths:
            raise RuntimeError("missing imported audio file")
        imports_dir = self.info.path / "imports"
        imports_dir.mkdir(exist_ok=True)
        imported_paths: dict[SourceKind, Path] = {}
        for source, temp_path in self.import_audio_paths.items():
            imported_name = self._imported_name_for_source(source, temp_path)
            imported_path = imports_dir / imported_name
            shutil.move(str(temp_path), imported_path)
            imported_paths[source] = imported_path
            if source == SourceKind.SYSTEM:
                self.settings.imported_system_file_name = imported_path.name
            elif source == SourceKind.MIC:
                self.settings.imported_mic_file_name = imported_path.name
            else:
                self.settings.imported_file_name = imported_path.name
        self.info.settings = self.settings
        self.store.save_info(self.info)

        self.status = TaskStatus.RUNNING
        self.info.status = self.status
        self.store.save_info(self.info)
        await self._publish("started", session=self.info.to_dict())
        await self._publish("status", status=TaskStatus.RUNNING, message="读取离线音频")

        decoded: dict[SourceKind, tuple[Path, object, int]] = {}
        for source, imported_path in imported_paths.items():
            wav_path = self._recording_path_for_import_source(source)
            pcm, sample_rate = await asyncio.to_thread(
                decode_audio_to_wav,
                imported_path,
                wav_path,
                self.config.audio.sample_rate,
            )
            decoded[source] = (wav_path, pcm, sample_rate)
            if _is_silent(pcm):
                label = "系统音频" if source == SourceKind.SYSTEM else "麦克风" if source == SourceKind.MIC else "导入音频"
                await self._publish("warning", message=f"{label}没有检测到有效声音。")
        if SourceKind.SYSTEM in decoded or SourceKind.MIC in decoded:
            system_pcm = decoded.get(SourceKind.SYSTEM, (None, None, None))[1]
            mic_pcm = decoded.get(SourceKind.MIC, (None, None, None))[1]
            await asyncio.to_thread(
                write_mixed_wav,
                self.info.path / "recordings" / "mixed.wav",
                self.config.audio.sample_rate,
                system_pcm,
                mic_pcm,
            )
        await self._publish("status", status=TaskStatus.RUNNING, message="离线音频转写中")
        source_order = (SourceKind.SYSTEM, SourceKind.MIC, SourceKind.MIXED)
        all_segments = []
        for source in source_order:
            if source not in decoded:
                continue
            wav_path, pcm, sample_rate = decoded[source]
            result = await asr.transcribe_pcm(
                pcm,
                sample_rate,
                self._language_for_source(source),
                self.info.path / "chunks",
            )
            speakers = self._offline_speakers(wav_path, result.segments, source)
            for idx, part in enumerate(result.segments):
                text = normalize_chinese_text(_clean_text(part.text))
                if not text:
                    continue
                all_segments.append((source, speakers, idx, part, result))
        all_segments.sort(key=lambda item: (float(item[3].start), 0 if item[0] == SourceKind.SYSTEM else 1))
        for source, speakers, idx, part, result in all_segments:
            translation = await self._translate_text(translator, part.text)
            segment = Segment(
                session_id=self.info.id,
                source=source,
                speaker=speakers[idx] if speakers and idx < len(speakers) else self._default_import_speaker(source),
                start=max(0.0, float(part.start)),
                end=max(0.0, float(part.end)),
                text=normalize_chinese_text(_clean_text(part.text)),
                language=result.language,
                translation=translation,
            )
            self.store.append_segment(self.info, segment)
            await self._publish("segment", segment=segment.to_dict(), asr=result.backend)
        await self._finalize_session(post_processor)

    def _imported_name_for_source(self, source: SourceKind, temp_path: Path) -> str:
        if source == SourceKind.SYSTEM:
            return self.settings.imported_system_file_name or temp_path.name
        if source == SourceKind.MIC:
            return self.settings.imported_mic_file_name or temp_path.name
        return self.settings.imported_file_name or temp_path.name

    def _recording_path_for_import_source(self, source: SourceKind) -> Path:
        rec_dir = self.info.path / "recordings"
        if source == SourceKind.SYSTEM:
            return rec_dir / "system.wav"
        if source == SourceKind.MIC:
            return rec_dir / "mic.wav"
        return rec_dir / "imported.wav"

    async def _translate_text(self, translator: Translator, text: str) -> str:
        if self.settings.translation == TranslationDirection.NONE:
            return ""
        try:
            translation = await translator.translate(
                text,
                self.settings.translation,
                self.settings.topic,
            )
            if self.settings.translation in (
                TranslationDirection.EN_TO_ZH,
                TranslationDirection.JA_TO_ZH,
            ):
                translation = normalize_chinese_text(translation)
            return translation
        except Exception as exc:
            await self._publish("warning", message=f"translation failed: {exc}")
            return ""

    def _default_import_speaker(self, source: SourceKind) -> str:
        if source == SourceKind.MIC:
            return "Me"
        return "Speaker 1"

    async def _finish(self, asr: ASRBackend, post_processor: MeetingPostProcessor | None) -> None:
        if self._capture:
            await self._capture.stop()
        if self._recording_task:
            with suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(self._recording_task), timeout=5)
        await self._wait_for_chunk_tasks(timeout=10)
        if self._recorder:
            self._recorder.close()
            await self._rewrite_transcript_from_recordings(asr)
        await self._finalize_session(post_processor)

    async def _finalize_session(self, post_processor: MeetingPostProcessor | None) -> None:
        self.store.finalize_subject_and_exports(self.info)
        if self.settings.enable_post_meeting_ai:
            reason = (
                "录音和逐字稿已保存。AI 纪要正在后台生成，完成后会自动更新本文件。"
                if post_processor
                else "未启用 LLM，会后只生成基础纪要。"
            )
            notes = write_pending_notes(self.info, reason)
            if notes:
                await self._publish("artifact", file=str(notes.relative_to(self.info.path)))
        self.store.publish_outputs(self.info)
        self.store.collect_files(self.info)
        self.status = TaskStatus.COMPLETED
        self.info.status = self.status
        self.store.save_info(self.info)
        await self._publish("completed", files=self.info.files)
        if self.settings.enable_post_meeting_ai and post_processor:
            task = asyncio.create_task(self._run_post_meeting_ai_background(post_processor))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def _run_post_meeting_ai_background(self, post_processor: MeetingPostProcessor) -> None:
        try:
            notes = await run_post_meeting_ai(self.info, post_processor)
        except Exception as exc:
            await self._publish("warning", message=f"post meeting AI failed: {exc}")
            return
        if notes:
            self.store.publish_outputs(self.info)
            self.store.collect_files(self.info)
            await self._publish("artifact", file=str(notes.relative_to(self.info.path)))
            await self._publish("post_ai_completed", file=str(notes.relative_to(self.info.path)))

    async def _process_chunk(self, asr: ASRBackend, translator: Translator, chunk) -> None:
        try:
            async with self._asr_semaphore:
                if _is_silent(chunk.pcm):
                    return
                result = await asr.transcribe_pcm(
                    chunk.pcm,
                    chunk.sample_rate,
                    self._language_for_source(chunk.source),
                    self.info.path / "chunks",
                )
            for part in result.segments:
                text = normalize_chinese_text(_clean_text(part.text))
                if not text:
                    continue
                if self._is_duplicate_text(chunk.source, text):
                    continue
                start = chunk.started_at - self._started_at + part.start
                end = chunk.started_at - self._started_at + part.end
                speaker = self._speaker.assign_realtime(chunk.source)
                translation = ""
                if self.settings.translation != TranslationDirection.NONE:
                    try:
                        translation = await translator.translate(
                            text,
                            self.settings.translation,
                            self.settings.topic,
                        )
                        if self.settings.translation in (
                            TranslationDirection.EN_TO_ZH,
                            TranslationDirection.JA_TO_ZH,
                        ):
                            translation = normalize_chinese_text(translation)
                    except Exception as exc:
                        await self._publish("warning", message=f"translation failed: {exc}")
                segment = Segment(
                    session_id=self.info.id,
                    source=chunk.source,
                    speaker=speaker,
                    start=max(0.0, start),
                    end=max(0.0, end),
                    text=text,
                    language=result.language,
                    translation=translation,
                )
                self.store.append_segment(self.info, segment)
                await self._publish("segment", segment=segment.to_dict(), asr=result.backend)
        except Exception as exc:
            await self._publish("warning", message=f"chunk processing failed: {exc}")

    async def _record_frames(self) -> None:
        if self._capture is None or self._recorder is None:
            return
        async for source, pcm in self._capture.raw_frames():
            self._recorder.write(source, pcm)

    async def _rewrite_transcript_from_recordings(self, asr: ASRBackend) -> None:
        rec_dir = self.info.path / "recordings"
        realtime_count = _count_realtime_segments(self.info.path / "segments.ndjson")
        sources = (
            (SourceKind.SYSTEM, rec_dir / "system.wav"),
            (SourceKind.MIC, rec_dir / "mic.wav"),
        )
        final_segments: list[dict] = []
        for source, wav_path in sources:
            if not wav_path.is_file():
                continue
            try:
                pcm, sample_rate = _read_wav_mono_float32(wav_path)
            except Exception as exc:
                await self._publish("warning", message=f"cannot read recording {wav_path.name}: {exc}")
                continue
            if _is_silent(pcm):
                if source == SourceKind.SYSTEM:
                    await self._publish(
                        "warning",
                        message=(
                            "系统音频轨没有检测到声音。若正在录微信语音，请确认微信输出已经路由到 "
                            "BlackHole/系统音频设备，否则只能录到自己的麦克风。"
                        ),
                    )
                elif source == SourceKind.MIC:
                    await self._publish(
                        "warning",
                        message=(
                            "麦克风轨没有检测到声音。请确认麦克风设备已连接、未被系统静音，"
                            "并在 macOS 隐私设置中允许启动会议助手的程序访问麦克风。"
                        ),
                    )
                continue
            await self._publish(
                "status",
                status=TaskStatus.STOPPING,
                message=f"生成{('对方' if source == SourceKind.SYSTEM else '我方')}干净逐字稿",
            )
            try:
                result = await asyncio.wait_for(
                    asr.transcribe_pcm(
                        pcm,
                        sample_rate,
                        self._language_for_source(source),
                        self.info.path / "chunks",
                    ),
                    timeout=75,
                )
            except Exception as exc:
                await self._publish("warning", message=f"final transcript ASR failed: {exc}")
                continue
            for part in result.segments:
                text = normalize_chinese_text(_clean_text(part.text))
                if not text:
                    continue
                final_segments.append(
                    {
                        "source": source,
                        "speaker": "Me" if source == SourceKind.MIC else "Speaker 1",
                        "start": max(0.0, float(part.start)),
                        "end": max(0.0, float(part.end)),
                        "text": text,
                    }
                )
        if not final_segments:
            return
        final_segments = _dedupe_final_segments(final_segments)
        if _should_keep_realtime_transcript(final_segments, realtime_count):
            await self._publish(
                "warning",
                message=(
                    "会后整段录音重转写质量不足，已保留实时逐字稿，避免用不完整结果覆盖。"
                ),
            )
            return
        final_segments.sort(key=lambda item: (item["start"], 0 if item["source"] == SourceKind.SYSTEM else 1))
        lines = []
        for item in final_segments:
            src = "我方" if item["source"] == SourceKind.MIC else "对方"
            lines.append(
                f"[{item['start']:.1f}-{item['end']:.1f}] [{src}] [{item['speaker']}] {item['text']}"
            )
        transcript_export_path(self.info).write_text("\n\n".join(lines).strip() + "\n", encoding="utf-8")

    def _track_chunk_task(self, task: asyncio.Task, source: SourceKind) -> None:
        self._chunk_tasks.add(task)
        task.add_done_callback(lambda done: self._finish_chunk_task(done, source))

    def _finish_chunk_task(self, task: asyncio.Task, source: SourceKind) -> None:
        self._chunk_tasks.discard(task)
        self._source_busy.discard(source)

    async def _wait_for_chunk_tasks(self, timeout: float) -> None:
        if not self._chunk_tasks:
            return
        _, pending = await asyncio.wait(list(self._chunk_tasks), timeout=timeout)
        if not pending:
            return
        await self._publish("warning", message=f"cancelled {len(pending)} pending ASR task(s) on stop")
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    def _build_asr(self) -> ASRBackend:
        backend = self.settings.asr_backend or self.config.asr.backend
        if backend == "remote":
            return RemoteASRClient(self.settings.remote_url or self.config.asr.remote_url, self.settings.local_model)
        if _should_use_mlx():
            return MLXWhisperASR(self.settings.local_model or self.config.asr.local_model)
        return LocalFasterWhisperASR(self.settings.local_model or self.config.asr.local_model)

    async def _warmup_asr(self, asr: ASRBackend) -> None:
        warmup = getattr(asr, "warmup", None)
        if not warmup:
            return
        await self._publish("status", status=TaskStatus.STARTING, message="warming up ASR")
        try:
            await asyncio.wait_for(
                warmup(self._language_for_source(SourceKind.MIC), self.info.path / "chunks"),
                timeout=45,
            )
        except Exception as exc:
            await self._publish("warning", message=f"ASR warmup skipped: {exc}")

    def _build_translator(self) -> Translator:
        if self.settings.translation == TranslationDirection.NONE or not self.config.llm.enabled:
            return NullTranslator()
        return LLMTranslator(
            LLMClient(
                provider=self.config.llm.provider,
                base_url=self.config.llm.base_url,
                model=self.config.llm.model,
            )
        )

    def _build_post_processor(self) -> MeetingPostProcessor | None:
        if not self.config.llm.enabled:
            return None
        return MeetingPostProcessor(
            LLMClient(
                provider=self.config.llm.provider,
                base_url=self.config.llm.base_url,
                model=self.config.llm.model,
                timeout=300,
                num_predict=4096,
            )
        )

    def _language_for_source(self, source: SourceKind) -> Language:
        if self.settings.language != Language.AUTO:
            return self.settings.language
        if self.settings.translation == TranslationDirection.EN_TO_ZH:
            return Language.EN
        if self.settings.translation in (TranslationDirection.ZH_TO_EN, TranslationDirection.ZH_TO_JA):
            return Language.ZH
        if self.settings.translation == TranslationDirection.JA_TO_ZH:
            return Language.JA
        return Language.AUTO

    def _offline_speakers(self, wav_path: Path, segments, source: SourceKind) -> list[str] | None:
        if source == SourceKind.MIC:
            return ["Me"] * len(segments)
        if not self.settings.enable_speaker_diarization:
            return None
        try:
            from .speaker import OptionalSpeakerDiarizer

            return OptionalSpeakerDiarizer().diarize(wav_path, segments)
        except Exception:
            return None

    def _is_duplicate_text(self, source: SourceKind, text: str) -> bool:
        normalized = "".join(text.lower().split())
        if len(normalized) < 2:
            return True
        for old in self._recent_text[source]:
            old_normalized = "".join(old.lower().split())
            if normalized == old_normalized:
                return True
            if normalized in old_normalized or old_normalized in normalized:
                if min(len(normalized), len(old_normalized)) >= 4:
                    return True
            if SequenceMatcher(None, normalized, old_normalized).ratio() >= 0.88:
                return True
        self._recent_text[source].append(text)
        return False

    async def _publish(self, event_type: str, **payload) -> None:
        event = RuntimeEvent(type=event_type, session_id=self.info.id, payload=payload)
        self.store.append_event(self.info, event)
        await self.events.publish(event)


class MeetingRuntime:
    def __init__(self, config: AppConfig, store: SessionStore, events: EventBus):
        self.config = config
        self.store = store
        self.events = events
        self.current: MeetingTask | None = None

    async def start(
        self,
        settings: MeetingSettings,
        import_audio_paths: dict[SourceKind, Path] | None = None,
    ) -> MeetingTask:
        if self.current and self.current.status in {TaskStatus.STARTING, TaskStatus.RUNNING, TaskStatus.PAUSED}:
            raise RuntimeError("a meeting is already running")
        task = MeetingTask(self.config, self.store, self.events, settings, import_audio_paths=import_audio_paths)
        self.current = task
        task.start()
        return task

    async def stop(self) -> None:
        if self.current:
            await self.current.stop()

    def pause(self, paused: bool) -> None:
        if self.current:
            self.current.pause(paused)

    def mute(self, source: SourceKind, muted: bool) -> None:
        if self.current:
            self.current.mute(source, muted)

    def status(self) -> dict:
        if not self.current:
            return {"status": str(TaskStatus.IDLE)}
        return self.current.snapshot()


def _clean_text(text: str) -> str:
    text = " ".join(text.replace("\uFFFD", "").split())
    bad_phrases = (
        "thanks for watching",
        "thank you for watching",
        "感谢观看",
        "感谢您的观看",
        "谢谢观看",
        "谢谢您的观看",
        "请使用简体中文",
        "简体中文字幕",
        "字幕志愿者",
        "字幕由",
        "字幕提供",
        "请不吝点赞",
        "請不吝點贊",
    )
    lowered = text.lower()
    if any(p in lowered for p in bad_phrases):
        return ""
    if _has_repeated_fragment(text):
        return ""
    return text


def _count_realtime_segments(path) -> int:
    import json

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    count = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            text = str(json.loads(line).get("text", ""))
        except json.JSONDecodeError:
            continue
        if _clean_text(text):
            count += 1
    return count


def _should_keep_realtime_transcript(final_segments: list[dict], realtime_count: int) -> bool:
    final_count = len(final_segments)
    if realtime_count < 6:
        return False
    if final_count < 3:
        return True
    return final_count < max(4, int(realtime_count * 0.45))


def _read_wav_mono_float32(path):
    import numpy as np

    with wave.open(str(path), "rb") as wf:
        sample_rate = wf.getframerate()
        channels = wf.getnchannels()
        frames = wf.readframes(wf.getnframes())
    pcm = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1)
    return pcm, sample_rate


def _dedupe_final_segments(segments: list[dict]) -> list[dict]:
    cleaned: list[dict] = []
    for segment in sorted(segments, key=lambda item: (item["source"], item["start"])):
        if cleaned and _is_repeated_final_segment(cleaned[-1], segment):
            if _prefer_segment(segment, cleaned[-1]):
                cleaned[-1] = segment
            continue
        cleaned.append(segment)
    return cleaned


def _is_repeated_final_segment(left: dict, right: dict) -> bool:
    if left["source"] != right["source"]:
        return False
    if right["start"] > left["end"] + 0.35:
        return False
    left_text = "".join(str(left["text"]).lower().split())
    right_text = "".join(str(right["text"]).lower().split())
    if not left_text or not right_text:
        return False
    if left_text in right_text or right_text in left_text:
        return min(len(left_text), len(right_text)) >= 4
    return SequenceMatcher(None, left_text, right_text).ratio() >= 0.72


def _prefer_segment(candidate: dict, existing: dict) -> bool:
    candidate_text = str(candidate["text"])
    existing_text = str(existing["text"])
    if len(candidate_text) >= len(existing_text) + 2:
        return True
    return candidate["end"] > existing["end"] and len(candidate_text) >= len(existing_text)


def _has_repeated_fragment(text: str) -> bool:
    compact = "".join(text.split())
    if len(compact) < 24:
        return False
    for size in range(4, 13):
        repeats = 0
        previous = ""
        for idx in range(0, len(compact) - size + 1, size):
            current = compact[idx : idx + size]
            if current == previous:
                repeats += 1
                if repeats >= 2:
                    return True
            else:
                repeats = 0
            previous = current
    return False


def _is_silent(pcm) -> bool:
    import numpy as np

    arr = np.asarray(pcm, dtype=np.float32)
    if arr.size == 0:
        return True
    rms = float(np.sqrt(np.mean(arr * arr)))
    return rms < 0.001


def _should_use_mlx() -> bool:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return False
    return importlib.util.find_spec("mlx_whisper") is not None
