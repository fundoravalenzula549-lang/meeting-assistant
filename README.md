# 会议助手

能够免费将会议的实时录音进行转文字。个人/小团队在线视频会议转录工作台，第一版目标是支持任意会议平台
（腾讯会议、Google Meet、Zoom、飞书、Teams 等），通过系统音频 + 麦克风双轨采集，
实时生成逐字稿、录音和会后整理结果。

## First Release Scope

- 实时采集系统音频，也就是会议里其他人的声音。
- 实时采集我的麦克风。
- 双轨显示：对方 / 我方。
- 会中录音：系统音频轨、麦克风轨、混合轨。
- 本机 faster-whisper ASR。
- 远端 GPU ASR API。
- 中文、英文、日文转录。
- 英中、日中、中英、中日翻译。
- 会议主题和术语增强。
- 会后 AI 校正逐字稿、摘要、行动项、决策项。
- 导出 TXT 和录音文件。
- WebUI 实时字幕和会后结果打开。
- 离线音频导入：支持单个音频文件转写，也支持系统音频/麦克风双轨音频合并后转写。
- 可选桌面悬浮字幕。
- 设备检测、静音、暂停、断线恢复。
- 基础多人识别：先区分我方/对方/来源轨道，并为对方轨道预留 Speaker 1/2/3 diarization。
- 安全默认：只监听本机，token 鉴权，文件名清洗，所有会议数据隔离到 Session 目录。

## Fixed Model Choices

- ASR: `large-v3-turbo`
- Apple Silicon local ASR auto-prefers `mlx-whisper`/Metal when available; CPU fallback uses `faster-whisper`.
- Translation and post-meeting AI: `qwen3:4b` first, so the full flow is easy to run locally
- Realtime audio chunks default to `3s` window / `3s` hop to avoid duplicate live captions from overlapping windows.

## Run

```bash
cd meeting-transcription-workbench
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[overlay,diarization]"
cp config.example.json config.json
meeting-workbench server
```

Then open `http://127.0.0.1:8765`.

> macOS 的系统音频捕获通常需要 BlackHole、Loopback 或聚合设备。只录麦克风可以直接运行；要录“会议里其他人的声音”，请先把会议软件输出路由到一个可见的输入设备。

离线导入依赖 `ffmpeg` 解码音频。系统会把可解码的音频统一转成内部 WAV，再进入 ASR 流程。

## Commands

```bash
meeting-workbench init-config
meeting-workbench server
meeting-workbench remote-asr --host 127.0.0.1 --port 8978
meeting-workbench overlay --url ws://127.0.0.1:8765/ws
```

## Project Shape

- `app/server.py`: FastAPI WebUI and control API.
- `app/pipeline/meeting.py`: runtime meeting task orchestration.
- `app/audio/`: device discovery, dual capture, recording.
- `app/asr/`: local faster-whisper and remote GPU clients.
- `app/translation/`: LLM translation and post-meeting generation.
- `app/sessions.py`: session directories and artifacts.
- `app/static/`: browser UI.
- `docs/PRODUCT_MAP.md`: v1 user story map and function list.
- `docs/ARCHITECTURE.md`: runtime architecture, data flow, and session layout.
- `docs/MODEL_POLICY.md`: fixed model choices and upgrade paths.

Export files use this naming pattern. If you do not type a topic, the app infers one from the transcript after the meeting stops:

```text
YYYYMMDD_HHMMSS_<meeting-topic-or-title>_逐字稿.txt
YYYYMMDD_HHMMSS_<meeting-topic-or-title>_会议纪要.md
```

Human-facing outputs are also mirrored into:

```text
data/会议输出录音
data/会议输出逐字稿
data/会议输出纪要
```

`config.json`、会议录音、逐字稿、上传文件和日志默认不会进入 Git 仓库。

## License

MIT License. See [LICENSE](LICENSE).
