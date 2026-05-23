from __future__ import annotations

import asyncio
import re
from pathlib import Path

from ..artifacts import meeting_notes_export_path, session_subject, session_timestamp, transcript_export_path
from ..models import SessionInfo
from ..text_normalizer import normalize_chinese_text
from ..translation.llm import MeetingPostProcessor

POST_AI_TIMEOUT_SECONDS = 360


async def run_post_meeting_ai(
    info: SessionInfo,
    processor: MeetingPostProcessor | None,
    timeout_seconds: float = POST_AI_TIMEOUT_SECONDS,
) -> Path | None:
    transcript_path = transcript_export_path(info)
    if not transcript_path.is_file():
        legacy_path = info.path / "exports" / "transcript.txt"
        transcript_path = legacy_path if legacy_path.is_file() else transcript_path
    if not transcript_path.is_file():
        return None
    transcript = normalize_chinese_text(transcript_path.read_text(encoding="utf-8").strip())
    if not transcript:
        return None
    out = meeting_notes_export_path(info)
    if processor is None:
        out.write_text(
            _fallback_notes(info, transcript, "未启用 LLM，会后只生成基础纪要。"),
            encoding="utf-8",
        )
        return out
    try:
        result = await asyncio.wait_for(
            processor.generate(transcript, topic=info.settings.topic),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        reason = (
            "AI 纪要生成超时：当前 qwen3:4b 对较长逐字稿处理会比较慢。"
            "系统已保留基础纪要和完整逐字稿，可以稍后重新生成或切换更强的 LLM 模型。"
        )
        out.write_text(_fallback_notes(info, transcript, reason), encoding="utf-8")
        return out
    except Exception as exc:
        out.write_text(
            _fallback_notes(info, transcript, f"AI 纪要生成失败：{_describe_exception(exc)}"),
            encoding="utf-8",
        )
        return out
    if not result.strip():
        out.write_text(
            _fallback_notes(info, transcript, "AI 纪要生成结果为空：模型没有返回可写入的正文。"),
            encoding="utf-8",
        )
        return out
    out.write_text(result + "\n", encoding="utf-8")
    return out


def write_pending_notes(info: SessionInfo, reason: str) -> Path | None:
    transcript_path = transcript_export_path(info)
    if not transcript_path.is_file():
        legacy_path = info.path / "exports" / "transcript.txt"
        transcript_path = legacy_path if legacy_path.is_file() else transcript_path
    if not transcript_path.is_file():
        return None
    transcript = normalize_chinese_text(transcript_path.read_text(encoding="utf-8").strip())
    if not transcript:
        return None
    out = meeting_notes_export_path(info)
    out.write_text(_fallback_notes(info, transcript, reason), encoding="utf-8")
    return out


def _fallback_notes(info: SessionInfo, transcript: str, reason: str) -> str:
    excerpts = _select_excerpts(transcript)
    excerpt_text = "\n".join(f"- {line}" for line in excerpts) or "- 暂无可用摘录。"
    return normalize_chinese_text(f"""# 会议整理

## 会议信息

- 时间：{session_timestamp(info)}
- 主题：{session_subject(info)}
- 标题：{info.title}

## 状态

{reason}

## 基础纪要

- 本文件为自动兜底版本，完整逐字稿和录音文件已经保存。
- 下方内容来自逐字稿摘录，未做大模型改写。

## 主要内容摘录

{excerpt_text}

## 行动项

- 基础规则未识别到明确行动项。

## 决策项

- 基础规则未识别到明确决策项。

## 完整逐字稿

{transcript}
""")


def _describe_exception(exc: Exception) -> str:
    message = str(exc).strip()
    if message:
        return message
    return exc.__class__.__name__


def _select_excerpts(transcript: str, limit: int = 12) -> list[str]:
    seen: set[str] = set()
    excerpts: list[str] = []
    for raw_line in transcript.splitlines():
        line = _clean_transcript_line(raw_line)
        if not _is_useful_excerpt(line):
            continue
        key = re.sub(r"\s+", "", line).lower()
        if key in seen:
            continue
        seen.add(key)
        excerpts.append(line)
        if len(excerpts) >= limit:
            break
    return excerpts


def _clean_transcript_line(line: str) -> str:
    line = line.strip()
    if not line or line.startswith("[译文]"):
        return ""
    line = re.sub(r"^\[[^\]]+\]\s*", "", line)
    line = re.sub(r"^\[[^\]]+\]\s*", "", line)
    line = re.sub(r"^\[[^\]]+\]\s*", "", line)
    return line.strip()


def _is_useful_excerpt(line: str) -> bool:
    if len(line) < 4:
        return False
    lowered = line.lower()
    noise_phrases = (
        "thanks for watching",
        "thank you for watching",
        "请使用简体中文字幕",
        "我认为简体中文输出",
        "字幕志愿者",
        "字幕由",
        "字幕提供",
        "李宗盛",
    )
    return not any(phrase in lowered for phrase in noise_phrases)
