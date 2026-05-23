from __future__ import annotations

import secrets
import string
import unicodedata
from pathlib import Path


_SAFE_ASCII_CHARS = f"-_.() {string.ascii_letters}{string.digits}"
_UNSAFE_FILENAME_CHARS = set('/\\:*?"<>|\0')


def _safe_filename_char(ch: str) -> bool:
    if ch in _SAFE_ASCII_CHARS:
        return True
    if ch in _UNSAFE_FILENAME_CHARS:
        return False
    return not unicodedata.category(ch).startswith("C")


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def sanitize_filename(name: str, fallback: str = "file") -> str:
    cleaned = "".join(ch for ch in name if _safe_filename_char(ch)).strip(" .")
    if not cleaned:
        return fallback
    return cleaned[:160]


def safe_slug(text: str, fallback: str = "meeting") -> str:
    text = text.strip().replace("/", " ").replace("\\", " ")
    cleaned = "".join(ch if _safe_filename_char(ch) else "-" for ch in text).strip("- .")
    return (cleaned or fallback)[:80]


def ensure_child_path(root: Path, candidate: Path) -> Path:
    root = root.resolve()
    candidate = candidate.resolve()
    if root != candidate and root not in candidate.parents:
        raise ValueError(f"path escapes root: {candidate}")
    return candidate
