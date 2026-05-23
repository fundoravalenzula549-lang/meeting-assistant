from __future__ import annotations

from functools import lru_cache
from typing import Protocol


class _Converter(Protocol):
    def convert(self, text: str) -> str: ...


_FALLBACK_T2S = str.maketrans(
    {
        "體": "体",
        "會": "会",
        "議": "议",
        "錄": "录",
        "轉": "转",
        "譯": "译",
        "聽": "听",
        "嘍": "喽",
        "實": "实",
        "時": "时",
        "顯": "显",
        "測": "测",
        "試": "试",
        "這": "这",
        "裡": "里",
        "裏": "里",
        "個": "个",
        "麼": "么",
        "嗎": "吗",
        "為": "为",
        "應": "应",
        "該": "该",
        "問": "问",
        "題": "题",
        "對": "对",
        "語": "语",
        "聲": "声",
        "麥": "麦",
        "話": "话",
        "員": "员",
        "與": "与",
        "開": "开",
        "關": "关",
        "後": "后",
        "項": "项",
        "數": "数",
        "據": "据",
        "檔": "档",
        "雲": "云",
    }
)


def normalize_chinese_text(text: str) -> str:
    """Normalize Chinese text to Simplified Chinese when possible."""
    if not text:
        return text
    converter = _opencc_converter()
    if converter is not None:
        try:
            return converter.convert(text)
        except Exception:
            pass
    return text.translate(_FALLBACK_T2S)


@lru_cache(maxsize=1)
def _opencc_converter() -> _Converter | None:
    try:
        from opencc import OpenCC
    except Exception:
        return None
    return OpenCC("t2s")
