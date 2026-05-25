from __future__ import annotations

import json
import os
import platform
import subprocess
from datetime import datetime
from dataclasses import asdict
from pathlib import Path

import asyncio

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .audio.devices import list_input_devices
from .config import AppConfig, PROJECT_ROOT, public_config
from .events import EventBus
from .models import Language, MeetingSettings, SourceKind, TranslationDirection
from .models import AudioInputMode
from .pipeline.meeting import MeetingRuntime
from .sessions import SessionStore
from .security import sanitize_filename


def create_app(config: AppConfig) -> FastAPI:
    events = EventBus()
    store = SessionStore(config.data_path)
    runtime = MeetingRuntime(config, store, events)
    app = FastAPI(title="Meeting Transcription Workbench")
    app.state.config = config
    app.state.events = events
    app.state.store = store
    app.state.runtime = runtime

    static_dir = PROJECT_ROOT / "app" / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(
            static_dir / "index.html",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/config")
    async def api_config():
        data = public_config(config)
        data["features"] = {
            "speaker_diarization_available": False,
        }
        return data

    @app.get("/api/devices")
    async def api_devices(_auth=Depends(_auth_dependency)):
        try:
            return {"devices": [asdict(d) for d in list_input_devices()]}
        except Exception as exc:
            return JSONResponse({"devices": [], "error": str(exc)}, status_code=200)

    @app.post("/api/start")
    async def api_start(body: dict, _auth=Depends(_auth_dependency)):
        settings = _settings_from_body(body, config)
        task = await runtime.start(settings)
        return {"ok": True, "session": task.info.to_dict()}

    @app.post("/api/start-import")
    async def api_start_import(
        settings_json: str = Form("{}"),
        file: UploadFile | None = File(default=None),
        system_file: UploadFile | None = File(default=None),
        mic_file: UploadFile | None = File(default=None),
        _auth=Depends(_auth_dependency),
    ):
        settings = _settings_from_body(_json_form(settings_json), config)
        has_single = bool(file and file.filename)
        has_system = bool(system_file and system_file.filename)
        has_mic = bool(mic_file and mic_file.filename)
        if has_single and (has_system or has_mic):
            raise HTTPException(400, "请在单个音频和双轨音频之间二选一")
        if not has_single and (has_system != has_mic):
            raise HTTPException(400, "双轨音频需要同时上传系统音频和麦克风音频")
        if not has_single and not (has_system and has_mic):
            raise HTTPException(400, "请上传一个音频文件，或同时上传系统音频和麦克风音频")
        import_paths: dict[SourceKind, Path] = {}
        if has_single and file:
            upload_path, original_name = await _save_import_upload(file, config)
            settings.imported_file_name = original_name
            import_paths[SourceKind.MIXED] = upload_path
        if has_system and system_file:
            upload_path, original_name = await _save_import_upload(system_file, config)
            settings.imported_system_file_name = original_name
            import_paths[SourceKind.SYSTEM] = upload_path
        if has_mic and mic_file:
            upload_path, original_name = await _save_import_upload(mic_file, config)
            settings.imported_mic_file_name = original_name
            import_paths[SourceKind.MIC] = upload_path
        if _is_generic_title(settings.title):
            settings.title = _title_from_import_names(settings) or "Imported Audio"
        settings.audio_input = AudioInputMode.FILE
        task = await runtime.start(settings, import_audio_paths=import_paths)
        return {"ok": True, "session": task.info.to_dict()}

    @app.post("/api/stop")
    async def api_stop(_auth=Depends(_auth_dependency)):
        await runtime.stop()
        return {"ok": True}

    @app.post("/api/pause")
    async def api_pause(body: dict, _auth=Depends(_auth_dependency)):
        runtime.pause(bool(body.get("paused", True)))
        return {"ok": True, "status": runtime.status()}

    @app.post("/api/mute")
    async def api_mute(body: dict, _auth=Depends(_auth_dependency)):
        source = SourceKind(body.get("source", "system"))
        runtime.mute(source, bool(body.get("muted", True)))
        return {"ok": True, "status": runtime.status()}

    @app.get("/api/status")
    async def api_status(_auth=Depends(_auth_dependency)):
        return runtime.status()

    @app.get("/api/sessions")
    async def api_sessions(_auth=Depends(_auth_dependency)):
        return {"sessions": store.list_sessions()}

    @app.get("/api/sessions/{session_id}/files/{file_path:path}")
    async def api_session_file(session_id: str, file_path: str, _auth=Depends(_auth_dependency)):
        safe_id = sanitize_filename(session_id)
        path = store.safe_artifact(safe_id, file_path)
        if not path.is_file():
            raise HTTPException(404, "file not found")
        return FileResponse(path)

    @app.get("/api/sessions/{session_id}/segments")
    async def api_session_segments(
        session_id: str,
        limit: int = 500,
        _auth=Depends(_auth_dependency),
    ):
        safe_id = sanitize_filename(session_id)
        return {"segments": store.read_segments(safe_id, limit=min(max(1, limit), 2000))}

    @app.post("/api/open-output-dir")
    async def api_open_output_dir(request: Request, _auth=Depends(_auth_dependency)):
        if not _is_local(request):
            raise HTTPException(403, "opening local paths is only allowed from this computer")
        store.ensure_output_roots()
        _open_local_path(store.transcript_output_root)
        return {"ok": True, "path": str(store.transcript_output_root)}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        if not _ws_authorized(config, ws):
            await ws.close(code=4001)
            return
        await ws.accept()
        try:
            async for message in events.subscribe(replay=False):
                await ws.send_text(message)
        except (WebSocketDisconnect, asyncio.CancelledError):
            return

    return app


async def _auth_dependency(
    request: Request,
    x_auth_token: str | None = Header(default=None),
):
    config: AppConfig = request.app.state.config
    if _is_local(request) and not config.security.allow_remote:
        return True
    if not config.security.require_token:
        return True
    token = x_auth_token or request.query_params.get("token", "")
    if token and token == config.server.auth_token:
        return True
    raise HTTPException(401, "missing or invalid auth token")


def _is_local(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1", "localhost"}


def _ws_authorized(config: AppConfig, ws: WebSocket) -> bool:
    host = ws.client.host if ws.client else ""
    if host in {"127.0.0.1", "::1", "localhost"} and not config.security.allow_remote:
        return True
    if not config.security.require_token:
        return True
    return ws.query_params.get("token", "") == config.server.auth_token


def _settings_from_body(body: dict, config: AppConfig) -> MeetingSettings:
    system_device_id, mic_device_id = _resolve_audio_device_ids(
        body.get("system_device_id"),
        body.get("mic_device_id"),
    )
    return MeetingSettings(
        title=(body.get("title") or "Online Meeting").strip(),
        topic=(body.get("topic") or "").strip(),
        audio_input=AudioInputMode(body.get("audio_input") or "live"),
        imported_file_name=sanitize_filename(body.get("imported_file_name") or "", "audio") if body.get("imported_file_name") else "",
        imported_system_file_name=sanitize_filename(body.get("imported_system_file_name") or "", "system") if body.get("imported_system_file_name") else "",
        imported_mic_file_name=sanitize_filename(body.get("imported_mic_file_name") or "", "mic") if body.get("imported_mic_file_name") else "",
        language=Language(body.get("language") or "zh"),
        translation=TranslationDirection(body.get("translation") or "none"),
        system_device_id=system_device_id,
        mic_device_id=mic_device_id,
        asr_backend=body.get("asr_backend") or config.asr.backend,
        local_model=body.get("local_model") or config.asr.local_model,
        remote_url=body.get("remote_url") or config.asr.remote_url,
        record=bool(body.get("record", True)),
        enable_overlay=bool(body.get("enable_overlay", False)),
        enable_post_meeting_ai=False,
        enable_speaker_diarization=bool(body.get("enable_speaker_diarization", False)),
    )


def _json_form(raw: str) -> dict:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "invalid settings payload") from exc
    if not isinstance(data, dict):
        raise HTTPException(400, "invalid settings payload")
    return data


async def _save_import_upload(
    file: UploadFile,
    config: AppConfig,
) -> tuple[Path, str]:
    original = sanitize_filename(file.filename or "audio", "audio")
    upload_dir = config.data_path / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{original}"
    size = 0
    limit = 1024 * 1024 * 1024
    try:
        with target.open("wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, "音频文件过大，请先压缩或拆分后再导入")
                fh.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    if size == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(400, "导入文件为空")
    return target, original


def _title_from_import_names(settings: MeetingSettings) -> str:
    if settings.imported_file_name:
        return Path(settings.imported_file_name).stem
    if settings.imported_system_file_name:
        return Path(settings.imported_system_file_name).stem
    if settings.imported_mic_file_name:
        return Path(settings.imported_mic_file_name).stem
    return ""


def _is_generic_title(title: str) -> bool:
    return title.strip().lower() in {"", "online meeting", "untitled meeting", "meeting"}


def _resolve_audio_device_ids(
    system_device_id: int | str | None,
    mic_device_id: int | str | None,
) -> tuple[int | str | None, int | str | None]:
    try:
        devices = list_input_devices()
    except Exception:
        return system_device_id, mic_device_id

    by_id = {str(dev.id): dev for dev in devices}
    requested_system = by_id.get(_device_value(system_device_id))
    requested_mic = by_id.get(_device_value(mic_device_id))

    system = requested_system if requested_system and requested_system.kind_hint != SourceKind.MIC else None
    mic = requested_mic if requested_mic and requested_mic.kind_hint != SourceKind.SYSTEM else None

    if system is None:
        system = next((dev for dev in devices if dev.kind_hint == SourceKind.SYSTEM), None)
    if mic is None:
        mic = next(
            (
                dev
                for dev in devices
                if dev.kind_hint == SourceKind.MIC
                and (system is None or str(dev.id) != str(system.id))
            ),
            None,
        )

    if system and mic and str(system.id) == str(mic.id):
        mic = next(
            (dev for dev in devices if dev.kind_hint == SourceKind.MIC and str(dev.id) != str(system.id)),
            None,
        )

    return (system.id if system else None, mic.id if mic else None)


def _device_value(value: int | str | None) -> str:
    return "" if value in (None, "") else str(value)


def _open_local_path(path: Path) -> None:
    target = str(path.resolve())
    system = platform.system()
    if system == "Darwin":
        subprocess.Popen(["open", target])
        return
    if system == "Windows":
        os.startfile(target)  # type: ignore[attr-defined]
        return
    subprocess.Popen(["xdg-open", target])
