from __future__ import annotations

import argparse
import tempfile
from dataclasses import asdict
from pathlib import Path
from time import monotonic

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse

from .asr.local_faster import LocalFasterWhisperASR
from .models import Language


app = FastAPI(title="Meeting Workbench Remote ASR")
_models: dict[str, LocalFasterWhisperASR] = {}


def _get_backend(model: str) -> LocalFasterWhisperASR:
    if model not in _models:
        _models[model] = LocalFasterWhisperASR(model_name=model, device="auto", compute_type="float16")
    return _models[model]


@app.get("/health")
def health():
    try:
        import torch

        gpu = bool(torch.cuda.is_available())
    except Exception:
        gpu = False
    return {"status": "ok", "gpu": gpu, "cached_models": list(_models)}


@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: UploadFile = File(...),
    model: str = Form("large-v3-turbo"),
    language: str = Form("auto"),
):
    work_dir = Path(tempfile.gettempdir()) / "meeting-workbench-remote"
    work_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "audio.wav").suffix or ".wav"
    path = work_dir / f"upload_{int(monotonic() * 1000)}{suffix}"
    try:
        path.write_bytes(await file.read())
        backend = _get_backend(model)
        result = await backend.transcribe_file(path, Language(language or "auto"))
        return {
            "text": result.text,
            "segments": [asdict(s) for s in result.segments],
            "language": result.language,
            "duration": result.duration,
            "processing_time": result.processing_time,
            "backend": result.backend,
        }
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    finally:
        try:
            path.unlink()
        except OSError:
            pass

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8978)
    args = parser.parse_args(argv)
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
