from __future__ import annotations

from typing import Protocol

from ..models import TranslationDirection


class Translator(Protocol):
    async def translate(self, text: str, direction: TranslationDirection, topic: str = "") -> str:
        ...


class NullTranslator:
    async def translate(self, text: str, direction: TranslationDirection, topic: str = "") -> str:
        return ""

