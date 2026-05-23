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
let lastRenderedBySource = {};

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
    $("outputPath").textContent = `Finder 输出目录：${cfg.data_dir}/会议输出`;
  }
  if (cfg.asr) {
    $("localModel").value = cfg.asr.local_model || "large-v3-turbo";
    $("remoteUrl").value = cfg.asr.remote_url || "http://127.0.0.1:8978";
    $("asrBackend").value = cfg.asr.backend || "local";
    updateAsrFields();
  }
  await loadDevices();
  connectWs();
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
  event.preventDefault();
  try {
    const res = await fetch("/api/open-output-dir", {method: "POST", headers: headers()});
    const data = await safeJson(res);
    if (!res.ok || !data.ok) {
      throw new Error(data.detail || data.error || "无法打开本地目录");
    }
    showToast("已打开本地目录", data.path || "会议输出目录已在 Finder 中打开。");
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
    record: $("record").checked,
    enable_post_meeting_ai: $("postAi").checked,
    enable_speaker_diarization: $("diarize").checked
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
  updateRecordingUi(true, "正在启动录音", "正在准备音频设备和 ASR");
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
  updateRecordingUi(true, "录音进行中", "正在监听系统音频和麦克风");
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
    setStatus(data.detail || data.error || "导入失败", false);
    showToast("导入失败", data.detail || data.error || "离线音频没有启动转写。", "error");
    return;
  }
  currentSession = data.session.id;
  $("sessionTitle").textContent = data.session.title;
  $("sessionId").textContent = currentSession;
  setStatus("离线转写中", true);
  updateRecordingUi(true, "离线转写中", `正在把 ${activeName} 转为逐字稿`);
}

function prepareNewRun() {
  clearRefreshCountdown();
  stopRequested = false;
  stopCompleted = false;
  stopping = false;
  sentenceCount = 0;
  lastRenderedBySource = {};
  $("transcript").innerHTML = "";
  $("files").innerHTML = "";
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
  $("refreshBtn").hidden = isFile;
  $("sessionTitle").textContent = isFile ? "离线音频转写" : "实时字幕";
  if (!running) {
    setStatus(isFile ? "待导入" : "待机", false);
  }
  updateImportModeUi();
}

function updateImportModeUi() {
  const isDual = selectedImportMode() === "dual";
  $("singleImportFields").hidden = isDual;
  $("dualImportFields").hidden = !isDual;
}

function updateImportFileName() {
  const file = $("audioFile").files[0];
  $("importFileName").textContent = file ? `${file.name} · ${formatBytes(file.size)}` : "尚未选择文件";
  const systemFile = $("systemAudioFile").files[0];
  $("systemImportFileName").textContent = systemFile ? `${systemFile.name} · ${formatBytes(systemFile.size)}` : "会议其他人的声音";
  const micFile = $("micAudioFile").files[0];
  $("micImportFileName").textContent = micFile ? `${micFile.name} · ${formatBytes(micFile.size)}` : "我的声音";
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
  updateRecordingUi(true, "正在停止录音", "正在保存录音、逐字稿和会后文件");
  setStopBusy(true);
  showToast("正在停止会议", "正在保存录音和逐字稿，AI 纪要会在后台继续整理。", "warning", {persist: true});
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
  updateRecordingUi(
    true,
    paused ? "转录已暂停" : "录音进行中",
    paused ? "当前会话仍在，点击继续恢复实时转录" : "正在监听系统音频和麦克风"
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
  ws.onmessage = (e) => handleEvent(JSON.parse(e.data));
  ws.onclose = () => setTimeout(connectWs, 1500);
}

function handleEvent(ev) {
  if (ev.type === "status") {
    setStatus(ev.message || ev.status || "处理中", ev.status === "running");
    return;
  }
  if (ev.type === "started") {
    activeAudioSource = ev.session?.settings?.audio_input || activeAudioSource;
    setStatus("运行中", true);
    if (activeAudioSource === "file") {
      updateRecordingUi(true, "离线转写中", "正在把导入音频转为逐字稿");
    } else {
      updateRecordingUi(true, "录音进行中", "正在监听系统音频和麦克风");
    }
    return;
  }
  if (ev.type === "segment") {
    addSegment(ev.segment, ev.asr);
    return;
  }
  if (ev.type === "completed") {
    setStatus("已完成", false);
    renderFiles(ev.session_id, ev.files || {});
    if (activeAudioSource === "file") {
      completeImportedAudio(ev.session_id, ev.files || {});
    }
    if (stopRequested) {
      completeStop(ev.session_id, ev.files || {});
    }
    return;
  }
  if (ev.type === "error") {
    setStatus(ev.message || "错误", false);
    showToast("运行错误", ev.message || "后端任务失败。", "error");
    return;
  }
  if (ev.type === "warning") {
    showToast("提醒", ev.message || "运行过程中出现提醒。", "warning");
  }
}

function addSegment(seg, asr) {
  const sourceKey = seg.source || "system";
  const previous = lastRenderedBySource[sourceKey];
  const merged = mergeRealtimeSegment(previous, seg);
  if (merged.action === "skip") return;
  if (merged.action === "replace") {
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
  sentenceCount += 1;
  $("stats").textContent = `${sentenceCount} 句`;
  scrollTranscriptToBottom();
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
      showToast("仍在收尾", "录音停止请求已经发出，正在等待后端确认完成。完成后会自动刷新。", "warning", {persist: true});
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
  showToast("停止失败", err.message || "停止请求没有完成，请稍后重试。", "error");
  setStatus(err.message || "停止失败", false);
}

function completeStop(sessionId, files) {
  if (stopCompleted) return;
  stopCompleted = true;
  stopping = false;
  setStopBusy(false);
  updateRecordingUi(false);
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
  showToast("转写完成", "离线音频已转成逐字稿，会后文件已经生成。");
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
  $("remoteUrlField").hidden = !isRemote;
  $("tokenField").hidden = !isRemote;
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

window.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("stopConfirm").hidden) closeStopConfirm();
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
$("pauseBtn").onclick = togglePause;
$("refreshBtn").onclick = loadDevices;
$("asrBackend").onchange = updateAsrFields;
$("openOutputDirLink").onclick = openOutputDir;
$("audioFile").onchange = updateImportFileName;
$("systemAudioFile").onchange = updateImportFileName;
$("micAudioFile").onchange = updateImportFileName;
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
