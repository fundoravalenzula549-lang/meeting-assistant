from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .models import SessionInfo
from .security import safe_slug

_GENERIC_TITLES = {
    "",
    "online meeting",
    "untitled meeting",
    "meeting",
    "会议",
    "未命名会议",
}


def session_timestamp(info: SessionInfo) -> str:
    return datetime.fromtimestamp(info.created_at).strftime("%Y%m%d_%H%M%S")


def session_subject(info: SessionInfo) -> str:
    topic = (info.settings.topic or "").strip()
    if topic:
        return topic
    auto_subject = (info.auto_subject or "").strip()
    if auto_subject:
        return auto_subject
    title = (info.title or "").strip()
    if title and title.lower() not in _GENERIC_TITLES:
        return title
    return "会议记录"


def export_stem(info: SessionInfo) -> str:
    return f"{session_timestamp(info)}_{safe_slug(session_subject(info), 'meeting')}"


def transcript_export_path(info: SessionInfo) -> Path:
    return info.path / "exports" / f"{export_stem(info)}_逐字稿.txt"


def meeting_notes_export_path(info: SessionInfo) -> Path:
    return info.path / "exports" / f"{export_stem(info)}_会议纪要.md"


def infer_subject_from_transcript(transcript: str) -> str:
    lines = [_clean_transcript_line(line) for line in transcript.splitlines()]
    lines = [line for line in lines if _is_meaningful_subject_line(line)]
    sample = " ".join(lines[:80])
    if not sample:
        return "会议记录"
    if any(word in sample for word in ("字幕", "延迟", "实时", "显示", "慢", "卡", "质量")):
        return "实时字幕转录测试"
    if "测试" in sample:
        return "转录测试"
    if any(word in sample for word in ("面试", "候选人", "招聘")):
        return "面试沟通"
    if any(word in sample for word in ("客户", "访谈", "需求")):
        return "客户需求访谈"
    if any(word in sample for word in ("周会", "同步", "进度")):
        return "项目同步会议"
    subject = re.sub(r"\s+", "", lines[0])
    subject = re.sub(r"[，。,.!?！？；;：:、]+", "-", subject).strip("-")
    return subject[:28] or "会议记录"


def find_transcript_export(info: SessionInfo) -> Path | None:
    candidates = [
        transcript_export_path(info),
        info.path / "exports" / "transcript.txt",
    ]
    candidates.extend(sorted((info.path / "exports").glob("*_逐字稿.txt")))
    for path in candidates:
        if path.is_file():
            return path
    return None


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for idx in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{idx}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"too many duplicate artifact names: {path}")


def _clean_transcript_line(line: str) -> str:
    line = line.strip()
    if not line or line.startswith("[译文]"):
        return ""
    line = re.sub(r"^\[[^\]]+\]\s*", "", line)
    line = re.sub(r"^\[[^\]]+\]\s*", "", line)
    line = re.sub(r"^\[[^\]]+\]\s*", "", line)
    return line.strip()


def _is_meaningful_subject_line(line: str) -> bool:
    if len(line.strip()) < 5:
        return False
    noise = (
        "哈喽",
        "哈嘍",
        "hello",
        "hi",
        "alô",
        "能听到吗",
        "能聽到嗎",
        "听得到吗",
        "聽得到嗎",
        "喂喂",
        "もしもし",
    )
    lowered = line.lower()
    return not any(item in lowered for item in noise)
