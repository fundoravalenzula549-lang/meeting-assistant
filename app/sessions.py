from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .artifacts import (
    export_stem,
    find_transcript_export,
    infer_subject_from_transcript,
    meeting_notes_export_path,
    transcript_export_path,
    unique_path,
)
from .models import AudioInputMode, MeetingSettings, RuntimeEvent, Segment, SessionInfo, SourceKind, TaskStatus
from .security import ensure_child_path, safe_slug, sanitize_filename


class SessionStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.root = data_dir / "sessions"
        self.output_root = data_dir / "会议输出"
        self.root.mkdir(parents=True, exist_ok=True)
        self.output_root.mkdir(parents=True, exist_ok=True)

    def create(self, settings: MeetingSettings) -> SessionInfo:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        sid = f"{stamp}_{safe_slug(settings.title)}"
        path = ensure_child_path(self.root, self.root / sid)
        path.mkdir(parents=True, exist_ok=False)
        for sub in ("recordings", "exports", "chunks"):
            (path / sub).mkdir(exist_ok=True)
        info = SessionInfo(
            id=sid,
            title=settings.title,
            path=path,
            created_at=datetime.now().timestamp(),
            status=TaskStatus.STARTING,
            settings=settings,
        )
        self.save_info(info)
        return info

    def save_info(self, info: SessionInfo) -> None:
        payload = info.to_dict()
        payload["settings"] = asdict(info.settings)
        (info.path / "session.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def append_event(self, info: SessionInfo, event: RuntimeEvent) -> None:
        with (info.path / "events.ndjson").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

    def append_segment(self, info: SessionInfo, segment: Segment) -> None:
        with (info.path / "segments.ndjson").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(segment.to_dict(), ensure_ascii=False) + "\n")
        with transcript_export_path(info).open("a", encoding="utf-8") as fh:
            speaker = f"[{segment.speaker}]"
            src = _source_label(segment.source)
            fh.write(f"[{segment.start:.1f}-{segment.end:.1f}] [{src}] {speaker} {segment.text}\n")
            if segment.translation:
                fh.write(f"[译文] {segment.translation}\n")
            fh.write("\n")

    def finalize_subject_and_exports(self, info: SessionInfo) -> None:
        transcript = find_transcript_export(info)
        if transcript is None:
            return
        if not info.settings.topic.strip():
            text = transcript.read_text(encoding="utf-8", errors="replace")
            info.auto_subject = infer_subject_from_transcript(text)
        target = transcript_export_path(info)
        if transcript.resolve() != target.resolve():
            target = unique_path(target)
            transcript.rename(target)
        self.save_info(info)

    def list_sessions(self) -> list[dict]:
        items = []
        for path in sorted(self.root.iterdir(), reverse=True):
            meta = path / "session.json"
            if meta.is_file():
                try:
                    items.append(json.loads(meta.read_text(encoding="utf-8")))
                except json.JSONDecodeError:
                    continue
        return items

    def safe_artifact(self, session_id: str, relative: str) -> Path:
        session_dir = ensure_child_path(self.root, self.root / sanitize_filename(session_id))
        return ensure_child_path(session_dir, session_dir / relative)

    def collect_files(self, info: SessionInfo) -> dict[str, str]:
        files = {}
        for path in info.path.rglob("*"):
            if path.is_file() and path.name not in {"events.ndjson", "segments.ndjson", ".DS_Store"}:
                rel = path.relative_to(info.path).as_posix()
                files[rel] = rel
        info.files = files
        self.save_info(info)
        return files

    def publish_outputs(self, info: SessionInfo) -> dict[str, str]:
        self.output_root.mkdir(parents=True, exist_ok=True)
        stem = export_stem(info)
        published: dict[str, str] = {}
        for src, output_name in self._human_output_specs(info, stem):
            if src is None or not src.is_file():
                continue
            flat_dest = self.output_root / output_name
            _replace_with_link_or_copy(src, flat_dest)
            published[output_name] = str(flat_dest)
        return published

    def _human_output_specs(self, info: SessionInfo, stem: str) -> list[tuple[Path | None, str]]:
        rec_dir = info.path / "recordings"
        if info.settings.audio_input == AudioInputMode.FILE:
            imported = _imported_audio_path(info)
            system_import = _track_import_path(info, SourceKind.SYSTEM)
            mic_import = _track_import_path(info, SourceKind.MIC)
            specs = [
                (find_transcript_export(info), f"{stem}_逐字稿.txt"),
                (meeting_notes_export_path(info), f"{stem}_会议纪要.md"),
                (imported, f"{stem}_导入音频{_suffix(imported)}") if imported else (None, ""),
                (system_import, f"{stem}_导入系统音频{_suffix(system_import)}") if system_import else (None, ""),
                (mic_import, f"{stem}_导入麦克风{_suffix(mic_import)}") if mic_import else (None, ""),
                (rec_dir / "imported.wav", f"{stem}_转写音频.wav"),
                (rec_dir / "system.wav", f"{stem}_系统音频.wav"),
                (rec_dir / "mic.wav", f"{stem}_我的麦克风.wav"),
                (rec_dir / "mixed.wav", f"{stem}_混合录音.wav"),
            ]
            return [(src, name) for src, name in specs if name]
        return [
            (find_transcript_export(info), f"{stem}_逐字稿.txt"),
            (meeting_notes_export_path(info), f"{stem}_会议纪要.md"),
            (rec_dir / "mixed.wav", f"{stem}_混合录音.wav"),
            (rec_dir / "mic.wav", f"{stem}_我的麦克风.wav"),
            (rec_dir / "system.wav", f"{stem}_系统音频.wav"),
        ]


def _source_label(source: SourceKind) -> str:
    if source == SourceKind.MIC:
        return "我方"
    if source == SourceKind.SYSTEM:
        return "对方"
    return "音频"


def _imported_audio_path(info: SessionInfo) -> Path | None:
    imports_dir = info.path / "imports"
    if info.settings.imported_system_file_name or info.settings.imported_mic_file_name:
        return None
    name = info.settings.imported_file_name
    if name:
        path = imports_dir / name
        if path.is_file():
            return path
    candidates = [path for path in imports_dir.glob("*") if path.is_file()]
    return candidates[0] if candidates else None


def _track_import_path(info: SessionInfo, source: SourceKind) -> Path | None:
    name = ""
    if source == SourceKind.SYSTEM:
        name = info.settings.imported_system_file_name
    elif source == SourceKind.MIC:
        name = info.settings.imported_mic_file_name
    if not name:
        return None
    path = info.path / "imports" / name
    return path if path.is_file() else None


def _suffix(path: Path | None) -> str:
    if path is None:
        return ""
    return path.suffix or ""


def _replace_with_link_or_copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        if dest.exists() or dest.is_symlink():
            dest.unlink()
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)
