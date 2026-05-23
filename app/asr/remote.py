from __future__ import annotations

import asyncio
import json
import urllib.request
import uuid
from pathlib import Path
from time import monotonic

import numpy as np

from .base import ASRResult, ASRSegment, temp_wav_path, write_wav
from ..models import Language


class RemoteASRClient:
    def __init__(self, base_url: str, model_name: str = "large-v3-turbo", timeout: int = 180):
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.timeout = timeout

    async def transcribe_pcm(
        self,
        pcm: np.ndarray,
        sample_rate: int,
        language: Language,
        work_dir: Path,
    ) -> ASRResult:
        wav_path = temp_wav_path(work_dir, "remote_asr")
        write_wav(wav_path, pcm, sample_rate)
        try:
            return await asyncio.to_thread(self._upload_file, wav_path, language)
        finally:
            try:
                wav_path.unlink()
            except OSError:
                pass

    def health(self) -> dict:
        with urllib.request.urlopen(f"{self.base_url}/health", timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _upload_file(self, wav_path: Path, language: Language) -> ASRResult:
        boundary = f"----mtw-{uuid.uuid4().hex}"
        lang = "" if language == Language.AUTO else str(language)
        fields = {
            "model": self.model_name,
            "language": lang,
        }
        body = bytearray()
        for name, value in fields.items():
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            body.extend(str(value).encode())
            body.extend(b"\r\n")
        content = wav_path.read_bytes()
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            (
                'Content-Disposition: form-data; name="file"; '
                f'filename="{wav_path.name}"\r\nContent-Type: audio/wav\r\n\r\n'
            ).encode()
        )
        body.extend(content)
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(
            f"{self.base_url}/v1/audio/transcriptions",
            data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        t0 = monotonic()
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        segments = [
            ASRSegment(start=float(s.get("start", 0)), end=float(s.get("end", 0)), text=s.get("text", "").strip())
            for s in data.get("segments", [])
            if s.get("text", "").strip()
        ]
        return ASRResult(
            text=data.get("text", " ".join(s.text for s in segments)),
            segments=segments,
            language=data.get("language", str(language)),
            duration=float(data.get("duration", 0.0) or 0.0),
            processing_time=float(data.get("processing_time", monotonic() - t0)),
            backend=f"remote:{self.base_url}",
        )

