from __future__ import annotations

import json
from dataclasses import MISSING, asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .security import generate_token


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"


@dataclass(slots=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    auth_token: str = ""


@dataclass(slots=True)
class AudioConfig:
    sample_rate: int = 16000
    window_seconds: float = 3.0
    hop_seconds: float = 3.0


@dataclass(slots=True)
class ASRConfig:
    backend: str = "local"
    local_model: str = "Qwen/Qwen3-ASR-0.6B"
    remote_url: str = "http://127.0.0.1:8978"


@dataclass(slots=True)
class LLMConfig:
    enabled: bool = False
    provider: str = "ollama"
    base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3:4b"


@dataclass(slots=True)
class SecurityConfig:
    allow_remote: bool = False
    require_token: bool = True


@dataclass(slots=True)
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    asr: ASRConfig = field(default_factory=ASRConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    data_dir: str = str(DEFAULT_DATA_DIR)

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir).expanduser().resolve()


def _merge_dataclass(cls: type, data: dict[str, Any]):
    values = {}
    for item in fields(cls):
        if item.name in data:
            values[item.name] = data[item.name]
        elif item.default is not MISSING:
            values[item.name] = item.default
        elif item.default_factory is not MISSING:
            values[item.name] = item.default_factory()
    return cls(**values)


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    if not path.exists():
        cfg = AppConfig()
        cfg.server.auth_token = generate_token()
        save_config(cfg, path)
        return cfg
    raw = json.loads(path.read_text(encoding="utf-8"))
    cfg = AppConfig(
        server=_merge_dataclass(ServerConfig, raw.get("server", {})),
        audio=_merge_dataclass(AudioConfig, raw.get("audio", {})),
        asr=_merge_dataclass(ASRConfig, raw.get("asr", {})),
        llm=_merge_dataclass(LLMConfig, raw.get("llm", {})),
        security=_merge_dataclass(SecurityConfig, raw.get("security", {})),
        data_dir=raw.get("data_dir", str(DEFAULT_DATA_DIR)),
    )
    if cfg.security.require_token and not cfg.server.auth_token:
        cfg.server.auth_token = generate_token()
        save_config(cfg, path)
    return cfg


def save_config(config: AppConfig, path: Path = DEFAULT_CONFIG_PATH) -> None:
    path.write_text(json.dumps(asdict(config), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def public_config(config: AppConfig) -> dict[str, Any]:
    data = asdict(config)
    if data.get("server"):
        data["server"]["auth_token"] = bool(config.server.auth_token)
    return data
