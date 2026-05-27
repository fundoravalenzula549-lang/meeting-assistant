const $ = (id) => document.getElementById(id);
let ws = null;
let sentenceCount = 0;
let paused = false;
let mutedSystem = false;
let mutedMic = false;
let currentSession = "";
let stopRequested = false;
let stopCompleted = false;
let refreshTimer = null;
let refreshTimeout = null;
let audioDevices = [];
let running = false;
let stopping = false;
let activeAudioSource = "live";
let recordingStartedAt = 0;
let elapsedTimer = null;
let completionPollTimer = null;
let completionHandled = false;
let lastRenderedBySource = {};
let renderedSegmentKeys = new Set();
let waveformShown = false;
let waveformBars = [];
let waveformHistory = [];
let waveformLevels = {system: 0, mic: 0};
const waveformBarCount = 64;
let audioSampleRate = 16000;
const recordingTrackCount = 3;
let recordingHealth = createRecordingHealthState();
let filenamePreviewTimer = null;
const genericMeetingTitles = new Set(["", "online meeting", "untitled meeting", "meeting", "会议", "未命名会议"]);

function headers() {
  const token = $("token").value.trim();
  return token ? {"Content-Type": "application/json", "X-Auth-Token": token} : {"Content-Type": "application/json"};
}

function authHeaders() {
  const token = $("token").value.trim();
  return token ? {"X-Auth-Token": token} : {};
}

async function loadConfig() {
  const cfg = await (await fetch("/api/config")).json();
  if (cfg.data_dir) {
    $("outputPath").textContent = `Finder 默认打开：${cfg.data_dir}/会议输出逐字稿（录音和纪要在同级目录）`;
  }
  if (cfg.audio?.sample_rate) {
    audioSampleRate = Number(cfg.audio.sample_rate) || audioSampleRate;
  }
  if (cfg.asr) {
    const configuredModel = cfg.asr.local_model || "Qwen/Qwen3-ASR-0.6B";
    ensureModelOption(configuredModel);
    $("localModel").value = configuredModel;
    $("remoteUrl").value = cfg.asr.remote_url || "http://127.0.0.1:8978";
    $("asrBackend").value = cfg.asr.backend || "local";
    updateAsrFields();
  }
  updateDiarizationAvailability(false);
  await loadDevices();
  connectWs();
  await restoreRunningSession();
}

async function loadDevices() {
  const data = await (await fetch("/api/devices", {headers: headers()})).json();
  audioDevices = data.devices || [];
  const system = $("systemDevice");
  const mic = $("micDevice");
  system.innerHTML = '<option value="">自动检测</option>';
  mic.innerHTML = '<option value="">自动检测</option>';
  for (const d of audioDevices) {
    const label = `[${d.id}] ${d.name}`;
    const o1 = new Option(label, d.id);
    const o2 = new Option(label, d.id);
    system.add(o1);
    mic.add(o2);
    if (d.kind_hint === "system" && !system.value) system.value = d.id;
    if (d.kind_hint === "mic" && !mic.value) mic.value = d.id;
  }
}

async function openOutputDir(event) {
  event?.preventDefault();
  try {
    const res = await fetch("/api/open-output-dir", {method: "POST", headers: headers()});
    const data = await safeJson(res);
    if (!res.ok || !data.ok) {
      throw new Error(data.detail || data.error || "无法打开本地目录");
    }
    showToast("已打开本地目录", data.path || "逐字稿目录已在 Finder 中打开。");
  } catch (err) {
    showToast("打开失败", err.message || "无法打开本地目录。", "error");
  }
}

function bodyFromForm() {
  if (selectedAudioSource() === "live") normalizeAudioDeviceSelection();
  return {
    title: $("title").value,
    topic: $("topic").value,
    audio_input: selectedAudioSource(),
    imported_file_name: $("audioFile").files[0]?.name || "",
    imported_system_file_name: $("systemAudioFile").files[0]?.name || "",
    imported_mic_file_name: $("micAudioFile").files[0]?.name || "",
    language: $("language").value,
    translation: $("translation").value,
    system_device_id: selectedAudioSource() === "live" ? ($("systemDevice").value || null) : null,
    mic_device_id: selectedAudioSource() === "live" ? ($("micDevice").value || null) : null,
    asr_backend: $("asrBackend").value,
    local_model: $("localModel").value,
    remote_url: $("remoteUrl").value,
    record: selectedAudioSource() === "live" ? true : $("record").checked,
    enable_post_meeting_ai: false,
    enable_speaker_diarization: false
  };
}

async function startMeeting() {
  if (selectedAudioSource() === "file") {
    await startImportedAudio();
    return;
  }
  activeAudioSource = "live";
  prepareNewRun();
  if (!validateAudioDeviceSelection()) return;
  setStatus("启动中", false);
  showRecordingWaveform("recording");
  updateRecordingUi(true, "正在启动录音", "正在准备音频设备");
  const res = await fetch("/api/start", {method: "POST", headers: headers(), body: JSON.stringify(bodyFromForm())});
  const data = await res.json();
  if (!data.ok) {
    updateRecordingUi(false);
    setStatus(data.detail || data.error || "启动失败", false);
    return;
  }
  currentSession = data.session.id;
  $("sessionTitle").textContent = data.session.title;
  $("sessionId").textContent = currentSession;
  setStatus("运行中", true);
  updateRecordingUi(true, "录音进行中", "停止后自动整段转写");
  startCompletionPoll();
}

async function startImportedAudio() {
  const mode = selectedImportMode();
  const file = $("audioFile").files[0];
  const systemFile = $("systemAudioFile").files[0];
  const micFile = $("micAudioFile").files[0];
  if (mode === "single" && !file) {
    showToast("请选择音频文件", "先添加一个离线音频文件，再点击开始。", "warning");
    return;
  }
  if (mode === "dual" && (!systemFile || !micFile)) {
    showToast("请补齐双轨音频", "需要同时添加系统音频和麦克风音频，才能合并并转写。", "warning");
    return;
  }
  activeAudioSource = "file";
  prepareNewRun();
  setStatus("转写中", true);
  const activeName = mode === "dual" ? "双轨音频" : file.name;
  updateRecordingUi(true, "离线转写中", `正在处理 ${activeName}`);
  showTranscribingLoader("离线转写中", `正在把 ${activeName} 转为逐字稿。`);
  const form = new FormData();
  form.append("settings_json", JSON.stringify(bodyFromForm()));
  if (mode === "dual") {
    form.append("system_file", systemFile);
    form.append("mic_file", micFile);
  } else {
    form.append("file", file);
  }
  const res = await fetch("/api/start-import", {method: "POST", headers: authHeaders(), body: form});
  const data = await safeJson(res);
  if (!res.ok || !data.ok) {
    updateRecordingUi(false);
    hideTranscribingLoader();
    setStatus(data.detail || data.error || "导入失败", false);
    showToast("导入失败", data.detail || data.error || "离线音频没有启动转写。", "error");
    return;
  }
  currentSession = data.session.id;
  $("sessionTitle").textContent = data.session.title;
  $("sessionId").textContent = currentSession;
  setStatus("离线转写中", true);
  updateRecordingUi(true, "离线转写中", `正在把 ${activeName} 转为逐字稿`);
  showTranscribingLoader("离线转写中", `正在把 ${activeName} 转为逐字稿。`);
  startCompletionPoll();
}

function prepareNewRun() {
  clearRefreshCountdown();
  clearCompletionPoll();
  closeImportCompleteModal();
  stopRequested = false;
  stopCompleted = false;
  completionHandled = false;
  stopping = false;
  sentenceCount = 0;
  lastRenderedBySource = {};
  renderedSegmentKeys = new Set();
  waveformShown = false;
  waveformBars = [];
  waveformHistory = [];
  waveformLevels = {system: 0, mic: 0};
  recordingHealth = createRecordingHealthState();
  $("transcript").innerHTML = "";
  $("files").innerHTML = "";
  $("stats").textContent = "0 句";
  setStopBusy(false);
}

function normalizeAudioDeviceSelection() {
  const system = $("systemDevice");
  const mic = $("micDevice");
  if (!system.value || !mic.value || system.value !== mic.value) return;
  const current = audioDevices.find(d => String(d.id) === String(system.value));
  const micCandidate = audioDevices.find(d => d.kind_hint === "mic" && String(d.id) !== String(system.value));
  const systemCandidate = audioDevices.find(d => d.kind_hint === "system" && String(d.id) !== String(mic.value));
  if (current?.kind_hint === "system" && micCandidate) {
    mic.value = micCandidate.id;
    showToast("已切换麦克风", `我的麦克风已改为 ${micCandidate.name}，避免两个轨道都录到系统音频。`, "warning");
    return;
  }
  if (current?.kind_hint === "mic" && systemCandidate) {
    system.value = systemCandidate.id;
    showToast("已切换系统音频", `系统音频已改为 ${systemCandidate.name}，避免两个轨道都录到麦克风。`, "warning");
  }
}

function validateAudioDeviceSelection() {
  normalizeAudioDeviceSelection();
  if ($("systemDevice").value && $("micDevice").value && $("systemDevice").value === $("micDevice").value) {
    showToast("设备选择冲突", "系统音频和我的麦克风现在选到了同一个输入设备，请把麦克风改成真实麦克风。", "error");
    setStatus("设备选择冲突", false);
    return false;
  }
  return true;
}

function selectedAudioSource() {
  return document.querySelector('input[name="audioSource"]:checked')?.value || "live";
}

function selectedImportMode() {
  return document.querySelector('input[name="importMode"]:checked')?.value || "single";
}

function updateAudioSourceUi() {
  const isFile = selectedAudioSource() === "file";
  $("importFilePanel").hidden = !isFile;
  $("deviceFields").hidden = isFile;
  $("recordOption").hidden = isFile;
  $("record").checked = true;
  $("record").disabled = !isFile;
  $("refreshBtn").hidden = isFile;
  $("sessionTitle").textContent = isFile ? "离线音频转写" : "录音转写";
  if (!running) {
    setStatus(isFile ? "待导入" : "待机", false);
  }
  updateImportModeUi();
  updateFilenamePreview();
}

function updateImportModeUi() {
  const isDual = selectedImportMode() === "dual";
  $("singleImportFields").hidden = isDual;
  $("dualImportFields").hidden = !isDual;
  updateFilenamePreview();
}

function updateImportFileName() {
  const file = $("audioFile").files[0];
  $("importFileName").textContent = file ? `${file.name} · ${formatBytes(file.size)}` : "点击选择，或拖入音频文件";
  const systemFile = $("systemAudioFile").files[0];
  $("systemImportFileName").textContent = systemFile ? `${systemFile.name} · ${formatBytes(systemFile.size)}` : "点击选择，或拖入对方音频";
  const micFile = $("micAudioFile").files[0];
  $("micImportFileName").textContent = micFile ? `${micFile.name} · ${formatBytes(micFile.size)}` : "点击选择，或拖入我方音频";
  syncFileDropState("audioFile");
  syncFileDropState("systemAudioFile");
  syncFileDropState("micAudioFile");
  updateFilenamePreview();
}

function setupImportDropZones() {
  for (const dropZone of document.querySelectorAll(".file-drop")) {
    const input = dropZone.querySelector('input[type="file"]');
    if (!input) continue;

    dropZone.addEventListener("dragenter", (event) => {
      event.preventDefault();
      dropZone.classList.add("drag-over");
    });
    dropZone.addEventListener("dragover", (event) => {
      event.preventDefault();
      dropZone.classList.add("drag-over");
    });
    dropZone.addEventListener("dragleave", (event) => {
      if (!dropZone.contains(event.relatedTarget)) {
        dropZone.classList.remove("drag-over");
      }
    });
    dropZone.addEventListener("drop", (event) => {
      event.preventDefault();
      dropZone.classList.remove("drag-over");
      const file = firstDroppedFile(event.dataTransfer);
      if (!file) {
        showToast("没有找到文件", "请拖入本地音频文件。", "warning");
        return;
      }
      setFileInput(input, file);
      updateImportFileName();
      showToast("已添加音频文件", file.name);
    });
  }

  window.addEventListener("dragover", (event) => event.preventDefault());
  window.addEventListener("drop", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target?.closest(".file-drop")) event.preventDefault();
  });
}

function setupClearFileButtons() {
  for (const button of document.querySelectorAll("[data-clear-file]")) {
    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const input = $(button.dataset.clearFile || "");
      if (!input) return;
      clearFileInput(input);
      updateImportFileName();
    });
  }
}

function setupTopicControls() {
  for (const button of document.querySelectorAll("[data-topic]")) {
    button.addEventListener("click", () => {
      $("topic").value = button.dataset.topic || "";
      updateFilenamePreview();
      $("topic").focus();
    });
  }
  $("topic").addEventListener("input", updateFilenamePreview);
  $("clearTopicBtn").addEventListener("click", () => {
    $("topic").value = "";
    updateFilenamePreview();
    $("topic").focus();
  });
}

function startFilenamePreviewTimer() {
  updateFilenamePreview();
  if (filenamePreviewTimer) return;
  filenamePreviewTimer = setInterval(updateFilenamePreview, 30000);
}

function updateFilenamePreview() {
  const preview = $("filenamePreview");
  if (!preview) return;
  preview.textContent = `${previewTimestamp()}_${previewSubject()}_逐字稿.txt`;
}

function previewTimestamp(date = new Date()) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  const hour = String(date.getHours()).padStart(2, "0");
  const minute = String(date.getMinutes()).padStart(2, "0");
  return `${year}${month}${day}_${hour}${minute}`;
}

function previewSubject() {
  const topic = $("topic").value.trim();
  if (topic) return safeFilenamePart(topic, "会议记录");
  const imported = previewImportedSubject();
  if (imported) return safeFilenamePart(imported, "会议记录");
  const title = ($("title").value || "").trim();
  if (title && !genericMeetingTitles.has(title.toLowerCase())) {
    return safeFilenamePart(title, "会议记录");
  }
  return "会议记录";
}

function previewImportedSubject() {
  if (selectedAudioSource() !== "file") return "";
  if (selectedImportMode() === "dual") {
    const systemFile = $("systemAudioFile").files[0];
    const micFile = $("micAudioFile").files[0];
    return fileStem(systemFile?.name) || fileStem(micFile?.name);
  }
  return fileStem($("audioFile").files[0]?.name);
}

function fileStem(name = "") {
  const base = String(name || "").split(/[\\/]/).pop();
  if (!base) return "";
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(0, dot) : base;
}

function safeFilenamePart(text, fallback) {
  const cleaned = String(text || "")
    .replace(/[\/\\]/g, " ")
    .replace(/[\0:*?"<>|]/g, "-")
    .replace(/[\u0000-\u001f\u007f]/g, "")
    .trim()
    .replace(/^[.\s]+|[.\s]+$/g, "")
    .slice(0, 80);
  return cleaned || fallback;
}

function firstDroppedFile(dataTransfer) {
  return Array.from(dataTransfer?.files || []).find(file => file && file.name) || null;
}

function setFileInput(input, file) {
  const transfer = new DataTransfer();
  transfer.items.add(file);
  input.files = transfer.files;
  input.dispatchEvent(new Event("change", {bubbles: true}));
}

function clearFileInput(input) {
  input.value = "";
  input.dispatchEvent(new Event("change", {bubbles: true}));
}

function syncFileDropState(inputId) {
  const input = $(inputId);
  const dropZone = input?.closest(".file-drop");
  if (!dropZone) return;
  dropZone.classList.toggle("has-file", Boolean(input.files?.[0]));
}

function stopMeeting() {
  if (!running || stopping) return;
  openStopConfirm();
}

async function performStopMeeting() {
  if (stopping) return;
  stopRequested = true;
  stopCompleted = false;
  stopping = true;
  closeStopConfirm();
  setStatus("停止中", false);
  showTranscribingLoader();
  updateRecordingUi(true, "正在停止录音", "正在整段转写录音");
  setStopBusy(true);
  showToast("正在停止会议", "正在保存录音，并用所选模型整段生成逐字稿。", "warning", {persist: true});
  fetch("/api/stop", {method: "POST", headers: headers()}).then(async (res) => {
    if (!res.ok) {
      const data = await safeJson(res);
      throw new Error(data.detail || data.error || "停止失败");
    }
  }).catch(handleStopError);
  confirmCompletedAfterStop().catch(handleStopError);
}

async function togglePause() {
  paused = !paused;
  await fetch("/api/pause", {method: "POST", headers: headers(), body: JSON.stringify({paused})});
  $("pauseBtn").textContent = paused ? "继续" : "暂停";
  setStatus(paused ? "已暂停" : "运行中", !paused);
  setWaveformMode(paused ? "paused" : "recording");
  updateRecordingUi(
    true,
    paused ? "录音已暂停" : "录音进行中",
    paused ? "点击继续恢复录音" : "停止后自动整段转写"
  );
}

async function mute(source, muted) {
  await fetch("/api/mute", {method: "POST", headers: headers(), body: JSON.stringify({source, muted})});
}

function connectWs() {
  const token = $("token").value.trim();
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}/ws${token ? "?token=" + encodeURIComponent(token) : ""}`;
  ws = new WebSocket(url);
  ws.onopen = () => {
    if (currentSession) loadSessionSegments(currentSession).catch(() => {});
  };
  ws.onmessage = (e) => handleEvent(JSON.parse(e.data));
  ws.onclose = () => setTimeout(connectWs, 1500);
}

async function restoreRunningSession() {
  const res = await fetch("/api/status", {headers: headers()});
  if (!res.ok) return;
  const data = await res.json();
  if (!["starting", "running", "paused", "stopping"].includes(data.status)) return;
  updateRecordingHealthFromStatus(data);

  currentSession = data.id || currentSession;
  activeAudioSource = data.settings?.audio_input || activeAudioSource;
  paused = data.status === "paused";
  stopping = data.status === "stopping";
  if (data.title) $("sessionTitle").textContent = data.title;
  if (currentSession) $("sessionId").textContent = currentSession;
  setStatus(activeAudioSource === "file" ? "离线转写中" : paused ? "已暂停" : stopping ? "停止中" : "运行中", !paused && !stopping);
  updateRecordingUi(
    true,
    activeAudioSource === "file" ? "离线转写中" : "录音进行中",
    activeAudioSource === "file" ? "正在把导入音频转为逐字稿" : "停止后自动整段转写"
  );
  if (activeAudioSource === "file") {
    showTranscribingLoader("离线转写中", "正在把导入音频转为逐字稿。");
  } else {
    showRecordingWaveform(stopping ? "transcribing" : paused ? "paused" : "recording");
  }
  if (currentSession) await loadSessionSegments(currentSession);
  startCompletionPoll();
}

async function loadSessionSegments(sessionId) {
  const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/segments?limit=800`, {headers: headers()});
  if (!res.ok) return;
  const data = await res.json();
  for (const seg of data.segments || []) {
    addSegment(seg, "", {merge: false});
  }
}

function handleEvent(ev) {
  if (ev.type === "status") {
    setStatus(ev.message || ev.status || "处理中", ev.status === "running");
    if (activeAudioSource === "file") {
      showTranscribingLoader("离线转写中", ev.message || "正在把导入音频转为逐字稿。");
    }
    if (activeAudioSource !== "file" && ev.status === "stopping") {
      setWaveformMode("transcribing");
    }
    return;
  }
  if (ev.type === "started") {
    currentSession = ev.session_id || ev.session?.id || currentSession;
    if (ev.session?.title) $("sessionTitle").textContent = ev.session.title;
    if (currentSession) $("sessionId").textContent = currentSession;
    activeAudioSource = ev.session?.settings?.audio_input || activeAudioSource;
    setStatus("运行中", true);
    if (activeAudioSource === "file") {
      updateRecordingUi(true, "离线转写中", "正在把导入音频转为逐字稿");
      showTranscribingLoader("离线转写中", "正在把导入音频转为逐字稿。");
    } else {
      showRecordingWaveform("recording");
      updateRecordingUi(true, "录音进行中", "停止后自动整段转写");
    }
    return;
  }
  if (ev.type === "audio_level") {
    if (currentSession && ev.session_id && ev.session_id !== currentSession) return;
    updateRecordingWaveform(ev.source, ev.level, ev.peak);
    return;
  }
  if (ev.type === "segment") {
    if (currentSession && ev.session_id && ev.session_id !== currentSession) return;
    addSegment(ev.segment, ev.asr);
    return;
  }
  if (ev.type === "completed") {
    completeRun(ev.session_id, ev.files || {});
    return;
  }
  if (ev.type === "error") {
    clearCompletionPoll();
    completionHandled = true;
    updateRecordingUi(false);
    hideTranscribingLoader();
    setStatus(ev.message || "错误", false);
    showToast("运行错误", ev.message || "后端任务失败。", "error");
    return;
  }
  if (ev.type === "warning") {
    showToast("提醒", ev.message || "运行过程中出现提醒。", "warning");
  }
}

function addSegment(seg, asr, options = {}) {
  if (waveformShown) hideRecordingWaveform(true);
  hideTranscribingLoader();
  const key = segmentKey(seg);
  if (renderedSegmentKeys.has(key)) return;
  const sourceKey = seg.source || "system";
  const previous = lastRenderedBySource[sourceKey];
  const merged = options.merge === false ? {action: "append", segment: seg} : mergeRealtimeSegment(previous, seg);
  if (merged.action === "skip") return;
  if (merged.action === "replace") {
    renderedSegmentKeys.add(key);
    previous.segment = merged.segment;
    previous.asr = asr || previous.asr;
    renderSegmentElement(previous.element, previous.segment, previous.asr);
    scrollTranscriptToBottom();
    return;
  }
  const div = document.createElement("div");
  div.className = `msg ${segmentClass(seg.source)}`;
  renderSegmentElement(div, seg, asr);
  const transcript = $("transcript");
  transcript.appendChild(div);
  lastRenderedBySource[sourceKey] = {element: div, segment: {...seg}, asr: asr || ""};
  renderedSegmentKeys.add(key);
  sentenceCount += 1;
  $("stats").textContent = `${sentenceCount} 句`;
  scrollTranscriptToBottom();
}

function segmentKey(seg) {
  return [
    seg.session_id || currentSession || "",
    seg.source || "",
    Number(seg.start || 0).toFixed(3),
    Number(seg.end || 0).toFixed(3),
    seg.text || ""
  ].join("|");
}

function renderSegmentElement(element, seg, asr) {
  const source = sourceLabel(seg.source);
  element.innerHTML = `
    <div class="meta">${timeRange(seg)} · ${source} · ${escapeHtml(asr || "")}</div>
    <div class="text"><span class="speaker">${escapeHtml(seg.speaker)}</span>${escapeHtml(seg.text)}</div>
    ${seg.translation ? `<div class="translation">${escapeHtml(seg.translation)}</div>` : ""}
  `;
}

function sourceLabel(source) {
  if (source === "mic") return "我方";
  if (source === "system") return "对方";
  return "音频";
}

function segmentClass(source) {
  if (source === "mic") return "mic";
  if (source === "mixed") return "mixed";
  return "system";
}

function scrollTranscriptToBottom() {
  const transcript = $("transcript");
  requestAnimationFrame(() => {
    transcript.scrollTop = transcript.scrollHeight;
  });
}

function showRecordingWaveform(mode = "recording") {
  const transcript = $("transcript");
  waveformShown = true;
  waveformHistory = new Array(waveformBarCount).fill(0.04);
  waveformLevels = {system: 0, mic: 0};
  transcript.innerHTML = `
    <section id="recordingWaveform" class="recording-waveform" data-mode="${mode}">
      <div class="waveform-status">
        <span class="rec-light"></span>
        <strong id="waveformTitle">${waveformTitle(mode)}</strong>
        <span id="waveformSubtitle">${waveformSubtitle(mode)}</span>
      </div>
      <div id="waveformBars" class="waveform-bars" aria-hidden="true">
        ${Array.from({length: waveformBarCount}, () => '<span class="waveform-bar"></span>').join("")}
      </div>
      <div class="waveform-tracks">
        <div class="waveform-track">
          <span>对方</span>
          <i><b id="systemWaveMeter"></b></i>
        </div>
        <div class="waveform-track">
          <span>我方</span>
          <i><b id="micWaveMeter"></b></i>
        </div>
      </div>
    </section>
  `;
  waveformBars = [...document.querySelectorAll(".waveform-bar")];
  $("stats").textContent = "录音中";
  renderWaveform();
}

function hideRecordingWaveform(clear = false) {
  waveformShown = false;
  waveformBars = [];
  waveformHistory = [];
  waveformLevels = {system: 0, mic: 0};
  if (clear && $("recordingWaveform")) {
    $("recordingWaveform").remove();
  }
}

function showTranscribingLoader(title = "整段转写中", message = "录音已经停止，正在用所选模型生成逐字稿。") {
  const transcript = $("transcript");
  hideRecordingWaveform(false);
  transcript.innerHTML = `
    <section id="transcribingLoader" class="transcribing-loader">
      <div class="loader-ring" aria-hidden="true"></div>
      <strong>${escapeHtml(title)}</strong>
      <p>${escapeHtml(message)}</p>
      <div class="loader-steps" aria-hidden="true">
        <span></span>
        <span></span>
        <span></span>
      </div>
    </section>
  `;
  $("stats").textContent = "转写中";
}

function hideTranscribingLoader() {
  const loader = $("transcribingLoader");
  if (loader) loader.remove();
}

function setWaveformMode(mode) {
  const panel = $("recordingWaveform");
  if (!panel) {
    if (mode === "transcribing") showTranscribingLoader();
    return;
  }
  if (mode === "transcribing") {
    showTranscribingLoader();
    return;
  }
  panel.dataset.mode = mode;
  $("waveformTitle").textContent = waveformTitle(mode);
  $("waveformSubtitle").textContent = waveformSubtitle(mode);
  if (mode === "transcribing") {
    $("stats").textContent = "转写中";
  } else if (mode === "paused") {
    $("stats").textContent = "已暂停";
  } else {
    $("stats").textContent = "录音中";
  }
}

function waveformTitle(mode) {
  if (mode === "transcribing") return "整段转写中";
  if (mode === "paused") return "录音暂停";
  return "录音中";
}

function waveformSubtitle(mode) {
  if (mode === "transcribing") return "正在用所选模型生成逐字稿";
  if (mode === "paused") return "继续后恢复写入录音";
  return "停止后自动生成逐字稿";
}

function updateRecordingWaveform(source, level, peak = 0) {
  if (activeAudioSource === "file") return;
  if (!waveformShown && running && !stopping) showRecordingWaveform("recording");
  const key = source === "mic" ? "mic" : "system";
  waveformLevels[key] = clamp01(Number(level) || 0);
  updateRecordingHealthFromLevel(key, level, peak);
  const combined = Math.max(waveformLevels.system, waveformLevels.mic, 0.03);
  waveformHistory.push(combined);
  waveformHistory = waveformHistory.slice(-waveformBarCount);
  renderWaveform();
}

function renderWaveform() {
  if (!waveformBars.length) return;
  const padded = new Array(Math.max(0, waveformBarCount - waveformHistory.length)).fill(0.04).concat(waveformHistory);
  waveformBars.forEach((bar, index) => {
    const value = clamp01(padded[index] || 0.04);
    const shaped = Math.max(0.08, Math.sqrt(value));
    bar.style.height = `${Math.round(12 + shaped * 112)}px`;
    bar.style.opacity = String(0.34 + shaped * 0.66);
  });
  const systemMeter = $("systemWaveMeter");
  const micMeter = $("micWaveMeter");
  if (systemMeter) systemMeter.style.width = `${Math.round(clamp01(waveformLevels.system) * 100)}%`;
  if (micMeter) micMeter.style.width = `${Math.round(clamp01(waveformLevels.mic) * 100)}%`;
}

function createRecordingHealthState() {
  return {
    diskFreeBytes: null,
    diskTotalBytes: null,
    system: createTrackHealth(),
    mic: createTrackHealth()
  };
}

function createTrackHealth() {
  return {
    lastSeenAt: 0,
    lastAudibleAt: 0,
    samples: []
  };
}

function updateRecordingHealthFromLevel(source, level, peak = 0) {
  const track = recordingHealth[source === "mic" ? "mic" : "system"];
  const now = Date.now();
  const sample = {
    time: now,
    level: clamp01(Number(level) || 0),
    peak: clamp01(Number(peak) || 0)
  };
  track.lastSeenAt = now;
  track.samples.push(sample);
  track.samples = track.samples.filter(item => now - item.time <= 6000);
  if (sample.level >= 0.035) {
    track.lastAudibleAt = now;
  }
  renderRecordingHealth();
}

function updateRecordingHealthFromStatus(data) {
  if (!data?.disk) return;
  recordingHealth.diskFreeBytes = Number.isFinite(Number(data.disk.free)) ? Number(data.disk.free) : null;
  recordingHealth.diskTotalBytes = Number.isFinite(Number(data.disk.total)) ? Number(data.disk.total) : null;
  renderRecordingHealth();
}

function renderRecordingHealth() {
  const panel = $("recordingHealth");
  if (!panel) return;
  const show = running && activeAudioSource !== "file";
  panel.hidden = !show;
  if (!show) return;

  const elapsed = recordingStartedAt ? Math.max(0, Math.floor((Date.now() - recordingStartedAt) / 1000)) : 0;
  renderTrackHealth("system", "systemHealth", "systemHealthText", elapsed);
  renderTrackHealth("mic", "micHealth", "micHealthText", elapsed);

  $("healthElapsed").textContent = formatDuration(elapsed);
  const estimatedBytes = estimateRecordingBytes(elapsed);
  $("healthSize").textContent = formatBytes(estimatedBytes);

  const diskMetric = $("healthDiskMetric");
  if (recordingHealth.diskFreeBytes == null) {
    $("healthDisk").textContent = "检测中";
    diskMetric.dataset.state = "waiting";
  } else {
    $("healthDisk").textContent = formatBytes(recordingHealth.diskFreeBytes);
    diskMetric.dataset.state = diskState(recordingHealth.diskFreeBytes, estimatedBytes);
  }
}

function renderTrackHealth(source, itemId, textId, elapsed) {
  const status = trackHealthStatus(recordingHealth[source], elapsed);
  $(itemId).dataset.state = status.state;
  $(textId).textContent = status.text;
}

function trackHealthStatus(track, elapsed) {
  const now = Date.now();
  if (!track.lastSeenAt) {
    return {state: "waiting", text: "等待音频"};
  }
  if (now - track.lastSeenAt > 3000) {
    return {state: "bad", text: "信号中断"};
  }
  const recent = track.samples.filter(item => now - item.time <= 5000);
  const maxLevel = Math.max(0, ...recent.map(item => item.level));
  const maxPeak = Math.max(0, ...recent.map(item => item.peak));
  if (maxPeak >= 0.98) {
    return {state: "bad", text: "爆音风险"};
  }
  if (elapsed >= 10 && (!track.lastAudibleAt || now - track.lastAudibleAt > 15000)) {
    return {state: "warn", text: "持续无声"};
  }
  if (maxLevel < 0.035) {
    return {state: "waiting", text: "当前安静"};
  }
  if (maxLevel < 0.08) {
    return {state: "warn", text: "声音偏小"};
  }
  return {state: "ok", text: "正常"};
}

function estimateRecordingBytes(elapsedSeconds) {
  const wavHeaderBytes = 44 * recordingTrackCount;
  const bytesPerSecond = audioSampleRate * 2 * recordingTrackCount;
  return wavHeaderBytes + Math.max(0, elapsedSeconds) * bytesPerSecond;
}

function diskState(freeBytes, estimatedBytes) {
  if (freeBytes < 1024 ** 3 || freeBytes < estimatedBytes * 1.25) return "bad";
  if (freeBytes < 5 * 1024 ** 3) return "warn";
  return "ok";
}

function clamp01(value) {
  return Math.max(0, Math.min(1, value));
}

function mergeRealtimeSegment(previous, seg) {
  if (!previous) return {action: "append", segment: seg};
  const prev = previous.segment;
  if (seg.start > Number(prev.end || 0) + 1.2) {
    return {action: "append", segment: seg};
  }
  const merged = mergeRealtimeText(prev.text, seg.text);
  if (!merged.text) return {action: "skip"};
  if (merged.mode === "replace") {
    return {
      action: "replace",
      segment: {
        ...seg,
        start: Math.min(Number(prev.start || 0), Number(seg.start || 0)),
        end: Math.max(Number(prev.end || 0), Number(seg.end || 0)),
        text: merged.text,
        translation: seg.translation || prev.translation || ""
      }
    };
  }
  return {action: "append", segment: seg};
}

function mergeRealtimeText(oldText, newText) {
  const oldCompact = compactText(oldText);
  const newCompact = compactText(newText);
  if (!oldCompact || !newCompact) return {mode: "append", text: newText};
  if (oldCompact === newCompact) return {mode: "skip", text: ""};
  if (oldCompact.includes(newCompact) && newCompact.length >= 3) return {mode: "skip", text: ""};
  if (newCompact.includes(oldCompact) && oldCompact.length >= 3) return {mode: "replace", text: newText};

  const overlap = findPrefixOverlap(oldCompact, newCompact);
  if (overlap >= 3) {
    if (overlap >= oldCompact.length - 1) {
      return {mode: "replace", text: newText};
    }
    const suffix = sliceByCompactChars(newText, overlap);
    return {mode: "replace", text: joinRealtimeText(oldText, suffix)};
  }

  const common = longestCommonSubstring(oldCompact, newCompact);
  if (common.length >= Math.ceil(Math.min(oldCompact.length, newCompact.length) * 0.65)) {
    if (newCompact.length >= oldCompact.length - 1) {
      return {mode: "replace", text: newText};
    }
    return {mode: "skip", text: ""};
  }

  const similarity = positionalSimilarity(
    oldCompact.slice(0, Math.min(oldCompact.length, newCompact.length)),
    newCompact.slice(0, Math.min(oldCompact.length, newCompact.length))
  );
  if (Math.min(oldCompact.length, newCompact.length) >= 5 && similarity >= 0.72) {
    return {mode: "replace", text: newText};
  }
  return {mode: "append", text: newText};
}

function compactText(text) {
  return String(text || "").replace(/[\s，。,.!?！？；;：:、]/g, "").toLowerCase();
}

function findPrefixOverlap(oldCompact, newCompact) {
  const max = Math.min(oldCompact.length, newCompact.length);
  for (let size = max; size >= 2; size -= 1) {
    const left = oldCompact.slice(-size);
    const right = newCompact.slice(0, size);
    if (left === right) return size;
    if (size >= 4 && positionalSimilarity(left, right) >= 0.74) return size;
  }
  return 0;
}

function positionalSimilarity(a, b) {
  const len = Math.max(a.length, b.length);
  if (!len) return 0;
  let same = 0;
  for (let i = 0; i < Math.min(a.length, b.length); i += 1) {
    if (a[i] === b[i]) same += 1;
  }
  return same / len;
}

function longestCommonSubstring(a, b) {
  let best = {length: 0, indexA: 0, indexB: 0};
  const row = new Array(b.length + 1).fill(0);
  for (let i = 1; i <= a.length; i += 1) {
    let prev = 0;
    for (let j = 1; j <= b.length; j += 1) {
      const temp = row[j];
      row[j] = a[i - 1] === b[j - 1] ? prev + 1 : 0;
      if (row[j] > best.length) {
        best = {length: row[j], indexA: i - row[j], indexB: j - row[j]};
      }
      prev = temp;
    }
  }
  return best;
}

function sliceByCompactChars(text, count) {
  let seen = 0;
  for (let i = 0; i < text.length; i += 1) {
    if (!/[\s，。,.!?！？；;：:、]/.test(text[i])) {
      seen += 1;
    }
    if (seen >= count) {
      return text.slice(i + 1).trim();
    }
  }
  return "";
}

function joinRealtimeText(left, right) {
  if (!right) return left;
  if (!left) return right;
  if (/[\s，。,.!?！？；;：:、]$/.test(left) || /^[\s，。,.!?！？；;：:、]/.test(right)) {
    return `${left}${right}`.trim();
  }
  return `${left}${right}`.trim();
}

function renderFiles(sessionId, files) {
  const root = $("files");
  root.innerHTML = "";
  for (const rel of Object.keys(files)) {
    const a = document.createElement("a");
    a.href = `/api/sessions/${encodeURIComponent(sessionId)}/files/${rel}`;
    a.target = "_blank";
    a.textContent = rel;
    root.appendChild(a);
  }
}

function setStatus(text, live) {
  $("statusText").textContent = text;
  $("liveDot").classList.toggle("on", !!live);
  document.body.classList.toggle("is-running", !!live || (running && !stopping));
  document.body.classList.toggle("is-stopping", stopping);
}

function setStopBusy(busy) {
  $("stopBtn").disabled = busy;
  $("pauseBtn").disabled = busy;
  $("startBtn").disabled = busy;
  $("confirmStopBtn").disabled = busy;
  $("stopBtn").textContent = busy ? "停止中" : "停止";
}

function startCompletionPoll() {
  clearCompletionPoll();
  completionPollTimer = setInterval(() => {
    pollRuntimeCompletion().catch(() => {});
  }, 2000);
  pollRuntimeCompletion().catch(() => {});
}

function clearCompletionPoll() {
  if (completionPollTimer) {
    clearInterval(completionPollTimer);
    completionPollTimer = null;
  }
}

async function pollRuntimeCompletion() {
  if (completionHandled || (!running && !stopping)) return;
  const res = await fetch("/api/status", {headers: headers()});
  if (!res.ok) return;
  const data = await res.json();
  updateRecordingHealthFromStatus(data);
  if (currentSession && data.id && data.id !== currentSession) return;
  if (data.status === "completed") {
    completeRun(data.id || currentSession, data.files || {});
    return;
  }
  if (data.status === "failed") {
    completionHandled = true;
    clearCompletionPoll();
    updateRecordingUi(false);
    hideTranscribingLoader();
    setStatus("转写失败", false);
    showToast("运行错误", "后端任务失败，请查看日志。", "error");
  }
}

function completeRun(sessionId, files) {
  if (completionHandled) return;
  completionHandled = true;
  clearCompletionPoll();
  stopping = false;
  if (activeAudioSource !== "file" && sentenceCount === 0) hideRecordingWaveform(true);
  hideTranscribingLoader();
  updateRecordingUi(false);
  setStatus("已完成", false);
  renderFiles(sessionId, files || {});
  if (activeAudioSource === "file") {
    completeImportedAudio(sessionId, files || {});
  }
  if (stopRequested) {
    completeStop(sessionId, files || {});
  }
}

async function confirmCompletedAfterStop() {
  for (let attempt = 0; attempt < 600 && stopRequested && !stopCompleted; attempt += 1) {
    const res = await fetch("/api/status", {headers: headers()});
    if (res.ok) {
      const data = await res.json();
      if (data.status === "completed") {
        completeStop(data.id || currentSession, data.files || {});
        return;
      }
      if (data.status === "failed") {
        throw new Error("停止失败，请查看后端日志。");
      }
    }
    if (attempt === 15) {
      showToast("仍在转写", "录音已经停止，后端正在整段生成逐字稿。完成后会自动刷新。", "warning", {persist: true});
    }
    await sleep(1000);
  }
  if (!stopCompleted) {
    setStopBusy(false);
    showToast("停止处理中", "页面没有等到完成状态。可以手动刷新查看最近会议文件。", "warning", {persist: true});
  }
}

function handleStopError(err) {
  if (stopCompleted) return;
  stopRequested = false;
  stopping = false;
  setStopBusy(false);
  updateRecordingUi(false);
  hideTranscribingLoader();
  showToast("停止失败", err.message || "停止请求没有完成，请稍后重试。", "error");
  setStatus(err.message || "停止失败", false);
}

function completeStop(sessionId, files) {
  if (stopCompleted) return;
  stopCompleted = true;
  stopping = false;
  setStopBusy(false);
  updateRecordingUi(false);
  hideTranscribingLoader();
  setStatus("已完成", false);
  if (sessionId && files) {
    renderFiles(sessionId, files);
  }
  startRefreshCountdown(5);
}

function completeImportedAudio(sessionId, files) {
  updateRecordingUi(false);
  setStatus("转写完成", false);
  if (sessionId && files) {
    renderFiles(sessionId, files);
  }
  openImportCompleteModal(files || {});
}

function openImportCompleteModal(files = {}) {
  const rels = Object.keys(files);
  const transcript = rels.find(rel => rel.includes("逐字稿")) || rels[0] || "";
  $("importCompleteMessage").textContent = "离线音频已经转成逐字稿，会后文件已经生成。";
  $("importCompleteFiles").textContent = transcript ? `已生成：${transcript}` : "";
  $("importCompleteModal").hidden = false;
  $("completeCloseBtn").focus();
}

function closeImportCompleteModal() {
  $("importCompleteModal").hidden = true;
}

function startRefreshCountdown(seconds) {
  clearRefreshCountdown();
  let remaining = seconds;
  const render = () => {
    showToast(
      "停止成功",
      `会议文件已保存，页面将在 <span class="toast-countdown">${remaining}</span> 秒后刷新。`,
      "success",
      {persist: true, html: true}
    );
  };
  render();
  refreshTimer = setInterval(() => {
    remaining -= 1;
    if (remaining <= 0) {
      clearRefreshCountdown();
      window.location.reload();
      return;
    }
    render();
  }, 1000);
  refreshTimeout = setTimeout(() => {
    clearRefreshCountdown();
    window.location.reload();
  }, seconds * 1000 + 250);
}

function clearRefreshCountdown() {
  if (refreshTimer) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
  if (refreshTimeout) {
    clearTimeout(refreshTimeout);
    refreshTimeout = null;
  }
}

function showToast(title, message, tone = "success", options = {}) {
  const host = $("toastHost");
  host.innerHTML = "";
  const toast = document.createElement("div");
  toast.className = `toast ${tone}`;
  const body = options.html ? message : escapeHtml(message);
  toast.innerHTML = `
    <div class="toast-title">${escapeHtml(title)}</div>
    <div class="toast-message">${body}</div>
  `;
  host.appendChild(toast);
  if (!options.persist) {
    setTimeout(() => {
      if (toast.parentNode === host) toast.remove();
    }, 4200);
  }
}

async function safeJson(res) {
  try {
    return await res.json();
  } catch {
    return {};
  }
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function updateRecordingUi(active, label = "", hint = "") {
  running = active;
  const isFile = activeAudioSource === "file";
  $("startBtn").hidden = active;
  $("pauseBtn").hidden = !active || isFile;
  $("stopBtn").hidden = !active || isFile;
  $("recordingBanner").hidden = !active;
  $("topRecordingBadge").hidden = !active;
  $("recordingHealth").hidden = !active || isFile;
  document.body.classList.toggle("is-running", active && !stopping);
  document.body.classList.toggle("is-stopping", stopping);
  if (label) $("recordingLabel").textContent = label;
  if (hint) $("recordingHint").textContent = hint;
  $("topRecordingLabel").textContent = isFile ? "离线转写" : "正在录音";
  if (active) {
    if (!recordingStartedAt) recordingStartedAt = Date.now();
    startElapsedTimer();
  } else {
    stopElapsedTimer();
    recordingStartedAt = 0;
  }
}

function startElapsedTimer() {
  renderElapsed();
  if (elapsedTimer) return;
  elapsedTimer = setInterval(renderElapsed, 1000);
}

function stopElapsedTimer() {
  if (elapsedTimer) {
    clearInterval(elapsedTimer);
    elapsedTimer = null;
  }
}

function renderElapsed() {
  const elapsed = recordingStartedAt ? Math.max(0, Math.floor((Date.now() - recordingStartedAt) / 1000)) : 0;
  const text = formatDuration(elapsed);
  $("recordingElapsed").textContent = text;
  $("topRecordingElapsed").textContent = text;
  renderRecordingHealth();
}

function formatDuration(totalSeconds) {
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) {
    return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function formatBytes(bytes) {
  const units = ["B", "KB", "MB", "GB"];
  let value = Number(bytes || 0);
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function updateAsrFields() {
  const isRemote = $("asrBackend").value === "remote";
  const localOnlyModels = ["Qwen3-ASR", "paraformer"];
  for (const option of $("localModel").options) {
    option.disabled = isRemote && localOnlyModels.some(model => option.value.includes(model));
  }
  if (isRemote && localOnlyModels.some(model => $("localModel").value.includes(model))) {
    $("localModel").value = "large-v3-turbo";
  }
  $("remoteUrlField").hidden = !isRemote;
  $("tokenField").hidden = !isRemote;
}

function ensureModelOption(value) {
  if (!value || [...$("localModel").options].some(option => option.value === value)) return;
  $("localModel").add(new Option(value, value));
}

function updateDiarizationAvailability(available) {
  const option = $("diarizeOption");
  if (!option) return;
  option.hidden = true;
  $("diarize").disabled = !available;
  $("diarize").checked = false;
  option.title = available ? "已启用离线 Speaker 1/2/3 聚类" : "需要安装 diarization 依赖后才能识别多个 Speaker";
  option.lastChild.textContent = available ? " Speaker 识别" : " Speaker 识别（未安装）";
}

function openStopConfirm() {
  $("stopConfirm").hidden = false;
  $("confirmStopBtn").focus();
}

function closeStopConfirm() {
  $("stopConfirm").hidden = true;
}

$("stopConfirm").addEventListener("click", (event) => {
  if (event.target === $("stopConfirm")) closeStopConfirm();
});

$("importCompleteModal").addEventListener("click", (event) => {
  if (event.target === $("importCompleteModal")) closeImportCompleteModal();
});

window.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("stopConfirm").hidden) closeStopConfirm();
  if (event.key === "Escape" && !$("importCompleteModal").hidden) closeImportCompleteModal();
});

function timeRange(seg) {
  return `${Number(seg.start || 0).toFixed(1)}-${Number(seg.end || 0).toFixed(1)}s`;
}

function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

$("startBtn").onclick = startMeeting;
$("stopBtn").onclick = stopMeeting;
$("confirmStopBtn").onclick = performStopMeeting;
$("cancelStopBtn").onclick = closeStopConfirm;
$("completeCloseBtn").onclick = closeImportCompleteModal;
$("completeOpenDirBtn").onclick = async () => {
  await openOutputDir();
  closeImportCompleteModal();
};
$("pauseBtn").onclick = togglePause;
$("refreshBtn").onclick = loadDevices;
$("asrBackend").onchange = updateAsrFields;
$("openOutputDirLink").onclick = openOutputDir;
$("audioFile").onchange = updateImportFileName;
$("systemAudioFile").onchange = updateImportFileName;
$("micAudioFile").onchange = updateImportFileName;
setupImportDropZones();
setupClearFileButtons();
setupTopicControls();
startFilenamePreviewTimer();
for (const input of document.querySelectorAll('input[name="audioSource"]')) {
  input.onchange = updateAudioSourceUi;
}
for (const input of document.querySelectorAll('input[name="importMode"]')) {
  input.onchange = updateImportModeUi;
}
$("muteSystemBtn").onclick = async () => {
  mutedSystem = !mutedSystem;
  $("muteSystemBtn").textContent = mutedSystem ? "恢复对方" : "静音对方";
  await mute("system", mutedSystem);
};
$("muteMicBtn").onclick = async () => {
  mutedMic = !mutedMic;
  $("muteMicBtn").textContent = mutedMic ? "恢复我方" : "静音我方";
  await mute("mic", mutedMic);
};

updateRecordingUi(false);
updateAsrFields();
updateAudioSourceUi();
updateImportModeUi();
updateImportFileName();
loadConfig().catch(err => setStatus(err.message, false));
