from __future__ import annotations

import asyncio
import json
import urllib.request
from dataclasses import dataclass

from ..models import TranslationDirection
from ..text_normalizer import normalize_chinese_text


_DIRECTION_LABELS = {
    TranslationDirection.EN_TO_ZH: ("English", "Simplified Chinese"),
    TranslationDirection.ZH_TO_EN: ("Chinese", "English"),
    TranslationDirection.JA_TO_ZH: ("Japanese", "Simplified Chinese"),
    TranslationDirection.ZH_TO_JA: ("Chinese", "Japanese"),
}


@dataclass(slots=True)
class LLMClient:
    provider: str
    base_url: str
    model: str
    timeout: int = 120
    num_predict: int | None = None

    async def complete(self, prompt: str) -> str:
        return await asyncio.to_thread(self._complete_sync, prompt)

    def _complete_sync(self, prompt: str) -> str:
        provider = self.provider.lower()
        if provider == "ollama":
            return self._complete_ollama(prompt)
        return self._complete_openai_compatible(prompt)

    def _complete_ollama(self, prompt: str) -> str:
        url = self.base_url.rstrip("/") + "/api/generate"
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1},
        }
        if self.num_predict is not None:
            payload["options"]["num_predict"] = self.num_predict
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        done_reason = data.get("done_reason") or "unknown"
        response = _strip_think(data.get("response", "")).strip()
        if done_reason == "length":
            raise RuntimeError(
                f"{self.model} 达到输出上限，结果可能被截断（done_reason=length）。"
                "建议换用非 thinking 的 instruct 模型，或使用远端更强模型生成纪要。"
            )
        if not response and data.get("thinking"):
            raise RuntimeError(
                f"{self.model} 只生成了思考过程，没有产出正文（done_reason={done_reason}）。"
                "建议提高输出上限，或换用非 thinking 的 instruct 模型。"
            )
        return response

    def _complete_openai_compatible(self, prompt: str) -> str:
        url = self.base_url.rstrip("/") + "/v1/chat/completions"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "stream": False,
        }
        if self.num_predict is not None:
            payload["max_tokens"] = self.num_predict
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return _strip_think(data["choices"][0]["message"]["content"]).strip()


class LLMTranslator:
    def __init__(self, client: LLMClient):
        self.client = client

    async def translate(self, text: str, direction: TranslationDirection, topic: str = "") -> str:
        if not text.strip() or direction == TranslationDirection.NONE:
            return ""
        src, dst = _DIRECTION_LABELS[direction]
        topic_line = f"\nMeeting topic and terminology context: {topic}" if topic else ""
        prompt = f"""You are a professional live meeting interpreter.
Translate the following {src} meeting transcript segment into {dst}.

Rules:
- Output only the translation.
- When translating into Chinese, use Simplified Chinese only.
- Preserve technical terms and names.
- Do not add commentary.
- If the input is noise or empty, output an empty string.{topic_line}

Segment:
---
{text}
---"""
        result = await self.client.complete(prompt)
        if direction in (TranslationDirection.EN_TO_ZH, TranslationDirection.JA_TO_ZH):
            return normalize_chinese_text(result)
        return result


class MeetingPostProcessor:
    def __init__(self, client: LLMClient):
        self.client = client

    async def generate(self, transcript: str, topic: str = "") -> str:
        if not transcript.strip():
            return ""
        transcript, truncated = _fit_post_meeting_transcript(transcript)
        topic_line = f"\n会议主题：{topic}" if topic else ""
        truncated_line = (
            "\n注意：逐字稿较长，以下只提供自动截取的关键片段；不要猜测未提供部分。"
            if truncated
            else ""
        )
        prompt = f"""你是专业会议记录整理员。请基于逐字稿生成会后纪要。{topic_line}{truncated_line}

要求：
- 使用简洁、准确的简体中文。
- 不编造逐字稿中没有出现的信息。
- 保留关键人名、项目名、数字和时间点。
- 如果信息不足，写“未提及”。
- 不要输出思考过程。

请输出以下结构：

# 会议整理

## 会议主题
用一句话概括本次会议主题。

## 重点摘要
列出 3-8 条关键结论。

## 行动项
用列表输出：负责人 / 事项 / 截止时间（没有就写“未指定”）。

## 决策项
列出会议中已经形成的决定。

## 待确认问题
列出还没有结论的问题。

## 逐字稿质量提示
只列出明显可能识别错误或信息不足的地方。

逐字稿：
---
{transcript}
---"""
        return normalize_chinese_text(await self.client.complete(prompt))


def _fit_post_meeting_transcript(transcript: str, limit: int = 12000) -> tuple[str, bool]:
    transcript = transcript.strip()
    if len(transcript) <= limit:
        return transcript, False
    head = transcript[:8000].rstrip()
    tail = transcript[-4000:].lstrip()
    return f"{head}\n\n[中间逐字稿过长，已省略]\n\n{tail}", True


def _strip_think(text: str) -> str:
    import re

    text = re.sub(r"<think>[\s\S]*?</think>", "", text)
    text = re.sub(r"<think>[\s\S]*", "", text)
    return text
