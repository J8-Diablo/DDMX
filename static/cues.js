// static/cues.js
// Système de playback avec VERROU DMX pendant transitions

// Au cas où
window.dmxLocked = window.dmxLocked || false;
window.identMode = window.identMode || false;

// ============================================================================
// PLAYBACK STATE
// ============================================================================
let playbackCueIndex = -1;        // Current cue index during playback (-1 = none)
let playbackPaused = false;       // Is playback paused?
let liveWaitAdjust = 0;           // Live adjustment to wait time (ms)
let playbackPhase = "idle";       // "idle" | "waiting" | "fading"
let playbackWaitRemaining = 0;    // Remaining phase time (ms)
let playbackPhaseEndHostMs = 0;
let playbackCueName = "";
let playbackPrevPhase = "idle";
let backendLastCueToken = 0;      // Incremented by backend when a cue starts
let backendPlaybackStarting = false;
let backendPlaybackPlan = [];
let backendAppliedPlanIndex = -1;
let uiFollowRunId = 0;
let uiFollowStopFlag = false;
let skipToNextCue = false;
let playbackSpeed = 1.0;
let ctcEnabled = false;
let ctcKeybind = "F8";
let ctcCaptureRelease = false;
let ctcActive = false;
let ctcStartedAt = 0;
let ctcLastMarkAt = 0;
let ctcCaptureCount = 0;
let ctcUiTimer = null;
let playbackUiTimer = null;
let cueDragContext = null;
let ctcKeyHeld = false;

function normalizePlaybackSpeedValue(raw) {
  const allowed = [0.25, 0.5, 1, 1.5, 2];
  const num = Number.parseFloat(String(raw ?? "1"));
  if (!Number.isFinite(num)) return 1.0;
  let best = allowed[0];
  let bestDelta = Math.abs(best - num);
  for (const cand of allowed) {
    const delta = Math.abs(cand - num);
    if (delta < bestDelta) {
      best = cand;
      bestDelta = delta;
    }
  }
  return best;
}

function playbackSpeedToOptionValue(speed) {
  const normalized = normalizePlaybackSpeedValue(speed);
  return Number.isInteger(normalized) ? String(normalized) : String(normalized);
}

function getSelectedPlaybackSpeed() {
  const speedSelect = $id("playback-speed-select");
  if (speedSelect) {
    return normalizePlaybackSpeedValue(speedSelect.value);
  }
  return normalizePlaybackSpeedValue(playbackSpeed);
}

function normalizeCuePanelPlaybackLayout() {
  $id("playback-bar-top")?.remove();
  $id("ctc-bar-top")?.remove();

  const playbackBar = $id("playback-bar");
  if (playbackBar) {
    const phase = playbackBar.querySelector("#playback-phase-top");
    const countdown = playbackBar.querySelector("#playback-countdown-top");
    const speed = playbackBar.querySelector("#playback-speed-display-top");
    const waitMinus = playbackBar.querySelector("#wait-minus-top");
    const waitPlus = playbackBar.querySelector("#wait-plus-top");
    const adjust = playbackBar.querySelector("#wait-adjust-display-top");

    if (phase) phase.id = "playback-phase";
    if (countdown) countdown.id = "playback-countdown";
    if (speed) speed.id = "playback-speed-display";
    if (waitMinus) waitMinus.id = "wait-minus";
    if (waitPlus) waitPlus.id = "wait-plus";
    if (adjust) adjust.id = "wait-adjust-display";
  }

  const ctcBar = $id("ctc-bar");
  if (ctcBar) {
    const key = ctcBar.querySelector("#ctc-keybind-display-top");
    const count = ctcBar.querySelector("#ctc-capture-count-top");
    const elapsed = ctcBar.querySelector("#ctc-elapsed-display-top");
    const stop = ctcBar.querySelector("#ctc-stop-top");

    if (key) key.id = "ctc-keybind-display";
    if (count) count.id = "ctc-capture-count";
    if (elapsed) elapsed.id = "ctc-elapsed-display";
    if (stop) stop.id = "ctc-stop";
  }
}

function normalizeCtcKeybindValue(raw) {
  const source = String(raw || "").trim();
  if (!source) return "F8";

  if (/^Key[A-Z]$/.test(source) || /^Digit[0-9]$/.test(source) || /^F([1-9]|1[0-2])$/.test(source)) {
    return source;
  }

  if (source === " ") return "Space";

  const lower = source.toLowerCase();
  if (lower === "space" || lower === "spacebar") return "Space";
  if (lower === "enter" || lower === "return") return "Enter";
  if (lower === "escape" || lower === "esc") return "Escape";
  if (lower === "tab") return "Tab";
  if (lower === "backspace") return "Backspace";
  if (lower === "delete") return "Delete";

  if (/^[a-z]$/i.test(source)) return `Key${source.toUpperCase()}`;
  if (/^[0-9]$/.test(source)) return `Digit${source}`;

  return source.replace(/\s+/g, "") || "F8";
}

function formatCtcKeybindDisplay(raw) {
  const code = normalizeCtcKeybindValue(raw);
  if (/^Key[A-Z]$/.test(code)) return code.slice(3);
  if (/^Digit[0-9]$/.test(code)) return code.slice(5);
  if (code === "Space") return "Space";
  if (code.startsWith("Arrow")) return `Arrow ${code.slice(5)}`;
  return code;
}

window.normalizeCtcKeybindValue = normalizeCtcKeybindValue;
window.formatCtcKeybindDisplay = formatCtcKeybindDisplay;

function formatCtcDuration(ms) {
  const rounded = Math.max(0, Math.round(ms || 0));
  return `${rounded}ms`;
}

function showCtcBar() {
  const bar = $id("ctc-bar");
  if (bar) bar.classList.remove("hidden");
}

function hideCtcBar() {
  const bar = $id("ctc-bar");
  if (bar) bar.classList.add("hidden");
}

function updateCtcUI() {
  const keyEl = $id("ctc-keybind-display");
  const countEl = $id("ctc-capture-count");
  const elapsedEl = $id("ctc-elapsed-display");

  if (!ctcActive) {
    hideCtcBar();
    return;
  }

  showCtcBar();

  if (keyEl) keyEl.textContent = `Key: ${formatCtcKeybindDisplay(ctcKeybind)}`;
  if (countEl) countEl.textContent = `${ctcCaptureCount} cue${ctcCaptureCount > 1 ? "s" : ""}`;
  if (elapsedEl) {
    const splitMs = ctcLastMarkAt > 0 ? (performance.now() - ctcLastMarkAt) : 0;
    elapsedEl.textContent = `Split: ${formatCtcDuration(splitMs)}`;
  }
}

function resetCtcSplitBase(markAt) {
  const now = Number.isFinite(markAt) ? markAt : performance.now();
  ctcLastMarkAt = now;
  if (ctcStartedAt <= 0) ctcStartedAt = now;
  updateCtcUI();
}

function startCtcUiTimer() {
  if (ctcUiTimer != null) return;
  ctcUiTimer = window.setInterval(updateCtcUI, 60);
}

function stopCtcUiTimer() {
  if (ctcUiTimer == null) return;
  window.clearInterval(ctcUiTimer);
  ctcUiTimer = null;
}

function ensurePlaybackUiTimer() {
  if (playbackUiTimer != null) return;
  playbackUiTimer = window.setInterval(() => {
    if (!playbackActive || playbackPaused || playbackPhaseEndHostMs <= 0) return;
    if (playbackPhase !== "waiting" && playbackPhase !== "fading") return;
    const nextRemaining = Math.max(0, playbackPhaseEndHostMs - Date.now());
    if (Math.abs(nextRemaining - playbackWaitRemaining) >= 50) {
      playbackWaitRemaining = nextRemaining;
      updatePlaybackUI();
    }
  }, 100);
}

function setCtcSettings(settings) {
  const next = (settings && typeof settings === "object") ? settings : {};
  ctcEnabled = Boolean(next.enabled);
  ctcKeybind = normalizeCtcKeybindValue(next.keybind || ctcKeybind || "F8");
  ctcCaptureRelease = Boolean(next.capture_release);
  updateCtcUI();
}

window.setCtcSettings = setCtcSettings;
window.getCtcSettings = function getCtcSettings() {
  return {
    enabled: ctcEnabled,
    keybind: ctcKeybind,
    capture_release: ctcCaptureRelease,
  };
};

function startCtcCapture() {
  if (ctcActive) return false;
  const now = performance.now();
  ctcActive = true;
  ctcStartedAt = now;
  ctcLastMarkAt = now;
  ctcCaptureCount = 0;
  ctcKeyHeld = false;
  startCtcUiTimer();
  updateCtcUI();
  toast(`CTC started (${formatCtcKeybindDisplay(ctcKeybind)})`, "info");
  return true;
}

function stopCtcCapture(silent = false) {
  ctcActive = false;
  ctcStartedAt = 0;
  ctcLastMarkAt = 0;
  ctcCaptureCount = 0;
  ctcKeyHeld = false;
  stopCtcUiTimer();
  hideCtcBar();
  if (!silent) {
    toast("CTC stopped", "info");
  }
}

function maybeStartCtcForPlayback() {
  if (!ctcEnabled || ctcActive) return false;
  return startCtcCapture();
}

function shouldIgnoreCtcShortcutTarget(target) {
  if (!target || !(target instanceof Element)) return false;
  return Boolean(target.closest("input, textarea, select, [contenteditable='true'], .dmx-modal, .swal2-container"));
}

function appendCtcCue(markAt) {
  const now = Number.isFinite(markAt) ? markAt : performance.now();
  const baseMark = ctcLastMarkAt > 0 ? ctcLastMarkAt : ctcStartedAt;
  const sleepMs = Math.max(0, Math.round(now - baseMark));
  ctcCaptureCount += 1;

  const seq = cuesObj.sequence || [];
  seq.push({
    name: `CTC ${ctcCaptureCount}`,
    sleep: String(sleepMs),
    duration: "0",
    devices: {},
    device_order: [],
    device_groups: {},
  });
  cuesObj.sequence = seq;
  resetCtcSplitBase(now);

  renderCueTable();
  updateCtcUI();
}

// Boucle d'animation des effets
let effectRenderLoopStarted = false;

function startEffectRenderLoop() {
  if (effectRenderLoopStarted) return;
  effectRenderLoopStarted = true;

  const tick = async () => {
    if (!effectRenderLoopStarted) return;

    // Identify mode is now handled ENTIRELY by Python engine
    // We only need to redraw the UI for visual feedback
    if (window.identMode) {
      // Just redraw - Python handles the actual DMX blink
      drawRig();
      syncRgbWidgetFromFirstDevice();
      syncPosWidgetFromFirstDevice();
    } else if (playbackActive && !window.dmxLocked) {
      if (!window.backendPlaybackOwned && (typeof window.isBackendMode !== "function" || !window.isBackendMode())) {
        await sendToEngineWithEffects(1.0);
      }
      drawRig();
      syncRgbWidgetFromFirstDevice();
      syncPosWidgetFromFirstDevice();
    }

    requestAnimationFrame(tick);
  };

  requestAnimationFrame(tick);
}

async function runSyncVideoCue(step) {
  if (window.syncVideo && typeof window.syncVideo.runCueAction === "function") {
    await window.syncVideo.runCueAction(step);
  }
}


///////////////////////
// FICHIERS DE CUE
///////////////////////

async function refreshCueFileList() {
  const sel = $id("cue-file-select");
  if (!sel) return;
  sel.innerHTML = "";

  // The cue dropdown is scoped to the ACTIVE PROJECT's cue lists, not the raw
  // cue/ directory. With no project (or a blank one) the list is empty, and we
  // never auto-load a cue. Loose cue files only appear once part of a project.
  const scoped = Array.isArray(window.projectCueFiles) ? window.projectCueFiles : [];
  try {
    const r = await fetch("/api/cue_files");
    const data = await r.json();
    const onDisk = new Set(data.files || []);
    const files = scoped.filter((f) => onDisk.has(f));

    for (const f of files) {
      const opt = document.createElement("option");
      opt.value = f;
      opt.textContent = f;
      sel.appendChild(opt);
    }

    sel.value = (currentCueFilename && files.includes(currentCueFilename))
      ? currentCueFilename : "";
  } catch (e) {
    console.error(e);
  }
}

function normalizeCueFilename(name) {
  if (!name) return null;
  name = String(name).trim();
  if (!name) return null;
  if (!name.toLowerCase().endsWith(".json")) {
    name = name.replace(/\.+$/g, "") + ".json";
  }
  return name;
}

function makeUniqueCueName(baseName = "New.json") {
  baseName = normalizeCueFilename(baseName) || "New.json";
  const sel = $id("cue-file-select");
  const existing = new Set();
  
  if (sel) {
    for (const opt of sel.options) existing.add(opt.value);
  }
  
  if (!existing.has(baseName)) return baseName;
  
  const m = baseName.match(/^(.*?)(\.json)$/i);
  const stem = m ? m[1] : baseName;
  const ext = m ? m[2] : ".json";
  
  let i = 2;
  while (existing.has(`${stem}-${i}${ext}`)) i++;
  return `${stem}-${i}${ext}`;
}

async function loadCueFile(filename) {
  if (!filename) return;
  
  try {
    const r = await fetch(`/api/cues/${filename}`);
    const data = await r.json();
    cuesObj = data || { 
      loop: false, 
      loop_count: null, 
      devices_def: {}, 
      virtual_groups: {}, 
      sequence: [] 
    };
    currentCueFilename = filename;
    selectedCueIndex = null;
    selectedCueIndices.clear();

    rebuildVirtualGroupsFromCues();
    // When a project is active the rig is owned by the project, so switching
    // cue list must NOT redefine the rig (decoupling). Otherwise (standalone
    // cue editing) rebuild the rig from the cue's embedded devices_def.
    if (!window.projectRigLocked) {
      rebuildRigFromCueFile();
    }
    renderCueTable();
    fillCuePropsFromSelected();
    toast(`Loaded ${filename}`, "success");
  } catch (e) {
    console.error(e);
    toast("Failed to load cue file", "error");
  }
}

async function saveCurrentCueFile() {
  if (!currentCueFilename) return toast("No cue file selected.", "error");
  
  cuesObj.devices_def = buildDevicesDefFromRig();
  cuesObj.virtual_groups = virtualGroups;
  
  try {
    await fetch(`/api/cues/${currentCueFilename}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cuesObj),
    });
    toast("Saved", "success");
  } catch (e) {
    console.error(e);
    toast("Save failed", "error");
  }
}

async function saveCueFileAs() {
  let name = await promptModal("New cue filename", "New.json", "ex: myshow.json");
  if (!name) return;
  
  name = makeUniqueCueName(name);
  cuesObj.devices_def = buildDevicesDefFromRig();
  cuesObj.virtual_groups = virtualGroups;
  
  try {
    await fetch(`/api/cues/${name}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cuesObj),
    });
    currentCueFilename = name;
    // Register the new cue list into the active project's scope.
    if (!Array.isArray(window.projectCueFiles)) window.projectCueFiles = [];
    if (!window.projectCueFiles.includes(name)) window.projectCueFiles.push(name);
    await refreshCueFileList();

    const sel = $id("cue-file-select");
    if (sel) sel.value = name;
    toast("Saved as " + name, "success");
  } catch (e) {
    console.error(e);
    toast("Save as failed", "error");
  }
}

///////////////////////
// BUILD DEVICES BLOCK
///////////////////////

function buildDevicesBlockFromSelection() {
  if (selectedDeviceOrder.length === 0) {
    toast("Select at least one device in the rig.", "error");
    return { devices: null, deviceGroups: null };
  }
  
  const devices = {};
  const deviceGroups = {};
  
  for (const id of selectedDeviceOrder) {
    const dev = rigDevices[id];
    if (!dev) continue;
    
    const fi = fixtures[dev.fixture] || {};
    const addrCount = fi.addr_count || 1;
    const localVals = deviceLocalValues[id] || {};
    
    const channels = { Universe: dev.universe };
    for (let li = 0; li < addrCount; li++) {
      const v = localVals[li] ?? 0;
      const absCh = dev.address + li;
      channels[String(absCh)] = v;
    }
    devices[id] = { channels };
    
    const groups = Array.from(deviceCurrentGroups[id] || []);
    deviceGroups[id] = groups;
  }
  
  return { devices, deviceGroups };
}

///////////////////////
// CUE OPS
///////////////////////

// Find all group IDs used by OTHER cues (not the current one being edited)
function getGroupsUsedByOtherCues(excludeCueIndex) {
  const usedGroups = new Set();
  const seq = cuesObj.sequence || [];

  for (let i = 0; i < seq.length; i++) {
    if (i === excludeCueIndex) continue;
    const step = seq[i];
    const dg = step.device_groups || {};
    for (const groups of Object.values(dg)) {
      if (Array.isArray(groups)) {
        groups.forEach(gid => usedGroups.add(gid));
      }
    }
  }
  return usedGroups;
}

// Clone groups that are shared with other cues to make them independent
function cloneSharedGroups(deviceGroups, excludeCueIndex) {
  const sharedGroups = getGroupsUsedByOtherCues(excludeCueIndex);
  const oldToNew = {};
  const newDeviceGroups = {};

  for (const [devId, groups] of Object.entries(deviceGroups)) {
    if (!Array.isArray(groups)) continue;

    newDeviceGroups[devId] = groups.map(gid => {
      // If this group is used by another cue, clone it
      if (sharedGroups.has(gid) && !oldToNew[gid]) {
        const originalGroup = virtualGroups[gid];
        if (originalGroup) {
          const newGid = allocVirtualGroupId();
          const clonedGroup = JSON.parse(JSON.stringify(originalGroup));
          clonedGroup.id = newGid;
          virtualGroups[newGid] = clonedGroup;
          cuesObj.virtual_groups = virtualGroups;
          oldToNew[gid] = newGid;
        }
      }
      return oldToNew[gid] || gid;
    });
  }

  return newDeviceGroups;
}

function cueAddFromSelection() {
  const { devices, deviceGroups } = buildDevicesBlockFromSelection();
  if (!devices) return;

  // For new cues, clone any groups that are already used by existing cues
  const independentGroups = cloneSharedGroups(deviceGroups, -1);

  const seq = cuesObj.sequence || [];
  const step = {
    name: `Cue ${seq.length + 1}`,
    sleep: "0",
    duration: "0",
    devices,
    device_order: [...selectedDeviceOrder],
    device_groups: independentGroups,
  };
  seq.push(step);
  cuesObj.sequence = seq;

  selectedCueIndex = seq.length - 1;
  fillCuePropsFromSelected();
  renderCueTable();
  toast("Cue added");
}

function cueUpdateFromSelection() {
  if (selectedCueIndex == null) return toast("Select a cue first.", "error");

  const { devices, deviceGroups } = buildDevicesBlockFromSelection();
  if (!devices) return;

  // Clone groups that are shared with other cues
  const independentGroups = cloneSharedGroups(deviceGroups, selectedCueIndex);

  const step = cuesObj.sequence[selectedCueIndex];

  // REPLACE devices entirely with only the selected ones
  step.devices = devices;
  step.device_order = [...selectedDeviceOrder];

  // REPLACE device_groups entirely with only the selected ones
  step.device_groups = {};
  for (const [id, groups] of Object.entries(independentGroups)) {
    if (groups && groups.length) step.device_groups[id] = groups;
  }

  renderCueTable();
  toast("Cue updated", "info");
}

async function cueDelete() {
  const count = selectedCueIndices.size;
  if (count === 0) {
    toast("Select a cue first.", "error");
    return;
  }

  const msg = count > 1 ? `Delete ${count} selected cues?` : "Delete selected cue?";
  const ok = await confirmModal("Delete cue(s)", msg);
  if (!ok) return;

  // Delete in reverse order to keep indices valid
  const indicesToDelete = [...selectedCueIndices].sort((a, b) => b - a);
  for (const idx of indicesToDelete) {
    cuesObj.sequence.splice(idx, 1);
  }

  selectedCueIndex = null;
  selectedCueIndices.clear();
  renderCueTable();
  fillCuePropsFromSelected();
  toast(`${count} cue(s) deleted`, "info");
}

// Deep copy helper for cue duplication
function deepCopyCue(step) {
  return JSON.parse(JSON.stringify(step));
}

// Clone effect groups for a single cue copy, returning the modified copy
function cloneEffectsForCueCopy(copy) {
  if (!copy.device_groups || Object.keys(copy.device_groups).length === 0) return;

  const oldToNew = {};

  for (const [devId, groups] of Object.entries(copy.device_groups)) {
    if (!Array.isArray(groups)) continue;

    for (const oldGid of groups) {
      if (oldToNew[oldGid]) continue;

      const originalGroup = virtualGroups[oldGid];
      if (!originalGroup) continue;

      const newGid = allocVirtualGroupId();
      const newGroup = JSON.parse(JSON.stringify(originalGroup));
      newGroup.id = newGid;

      virtualGroups[newGid] = newGroup;
      cuesObj.virtual_groups = virtualGroups;

      oldToNew[oldGid] = newGid;
    }
  }

  for (const [devId, groups] of Object.entries(copy.device_groups)) {
    if (!Array.isArray(groups)) continue;
    copy.device_groups[devId] = groups.map(gid => oldToNew[gid] || gid);
  }
}

function cueDuplicate() {
  const count = selectedCueIndices.size;
  if (count === 0) {
    toast("Select a cue first.", "error");
    return;
  }

  // Sort indices and duplicate in order, inserting after the last selected
  const sortedIndices = [...selectedCueIndices].sort((a, b) => a - b);
  const insertAfter = sortedIndices[sortedIndices.length - 1];

  const newCues = [];
  for (const idx of sortedIndices) {
    const original = cuesObj.sequence[idx];
    const copy = deepCopyCue(original);
    copy.name = (original.name || "Cue") + " (copy)";
    cloneEffectsForCueCopy(copy);
    newCues.push(copy);
  }

  // Insert all copies after the last selected cue
  cuesObj.sequence.splice(insertAfter + 1, 0, ...newCues);

  // Select the new cues
  selectedCueIndices.clear();
  for (let i = 0; i < newCues.length; i++) {
    selectedCueIndices.add(insertAfter + 1 + i);
  }
  selectedCueIndex = insertAfter + 1;

  renderCueTable();
  fillCuePropsFromSelected();
  toast(`${count} cue(s) duplicated`, "success");
}

function fillCuePropsFromSelected() {
  const step = selectedCueIndex != null ? cuesObj.sequence[selectedCueIndex] : null;
  $id("cue-prop-name") && ($id("cue-prop-name").value = step?.name ?? "");
  $id("cue-prop-sleep") && ($id("cue-prop-sleep").value = step?.sleep ?? "0");
  $id("cue-prop-duration") && ($id("cue-prop-duration").value = step?.duration ?? "0");
}

function applyCueProps() {
  if (selectedCueIndex == null) return;
  
  const step = cuesObj.sequence[selectedCueIndex];
  step.name = $id("cue-prop-name")?.value || "";
  step.sleep = $id("cue-prop-sleep")?.value || "0";
  step.duration = $id("cue-prop-duration")?.value || "0";

  if (typeof window.isTimelineEditorMode === "function" && window.isTimelineEditorMode() && step.timeline) {
    const text = String(step.duration || "0").trim();
    const operators = ["<>", "><", "||", "|", ">", "<", "?"];
    let operator = "";
    let baseMs = Math.max(0, parseInt(text, 10) || 0);
    let spreadMs = 0;
    for (const candidate of operators) {
      if (!text.includes(candidate)) continue;
      operator = candidate;
      const parts = text.split(candidate);
      baseMs = Math.max(0, parseInt(parts[0], 10) || 0);
      spreadMs = Math.max(0, parseInt(parts[1], 10) || 0);
      break;
    }
    step.timeline.fade_operator = operator;
    step.timeline.fade_start_ms = 0;
    step.timeline.fade_end_ms = Math.max(0, Math.min(parseInt(step.timeline.length_ms, 10) || 0, baseMs + spreadMs));
  }
  
  renderCueTable();
  toast("Cue props updated", "info");
}

let lastClickedCueIndex = null; // For shift+click range selection

function renderCueTable() {
  const tbody = $id("cue-table-body");
  if (!tbody) return;

  const timelineMode = typeof window.isTimelineEditorMode === "function" && window.isTimelineEditorMode();
  const timelineEditor = $id("timeline-editor");
  const tableContainer = document.querySelector(".cue-table-container");
  if (timelineEditor) timelineEditor.classList.toggle("hidden", !timelineMode);
  if (tableContainer) tableContainer.classList.toggle("hidden", timelineMode);

  if (timelineMode) {
    tbody.innerHTML = "";
    updateCueSelectionCount();
    updatePlayFromButtonState();
    if (typeof window.renderTimelineEditor === "function") {
      window.renderTimelineEditor();
    }
    updatePlayingHighlight();
    return;
  }

  tbody.innerHTML = "";

  const seq = cuesObj.sequence || [];

  seq.forEach((step, idx) => {
    const tr = document.createElement("tr");
    tr.dataset.index = String(idx);            // <-- important pour le DnD

    // Primary selection (for single ops like update/props)
    if (idx === selectedCueIndex) tr.classList.add("selected");
    // Multi-selection (for batch ops)
    if (selectedCueIndices.has(idx)) tr.classList.add("multi-selected");
    // Currently playing highlight
    if (idx === playbackCueIndex) tr.classList.add("playing");
    // Loop group indicator
    if (step.loopGroup) {
      tr.classList.add("loop-group");
      tr.dataset.loopGroup = step.loopGroup;
    }

    const tdIdx = document.createElement("td");
    tdIdx.textContent = idx + 1;
    // Show loop indicator if part of a group
    if (step.loopGroup) {
      const loopBadge = document.createElement("span");
      loopBadge.className = "loop-badge";
      loopBadge.textContent = `🔁${step.loopCount || 1}`;
      loopBadge.title = `Loop group: ${step.loopGroup}, ${step.loopCount || 1}x`;
      tdIdx.appendChild(loopBadge);
    }

    const tdName = document.createElement("td");
    tdName.textContent = step.name || "";

    const tdDelay = document.createElement("td");
    tdDelay.textContent = (step.sleep ?? 0) + "ms";

    const tdFade = document.createElement("td");
    tdFade.textContent = step.duration != null ? String(step.duration) : "0";

    const tdDevs = document.createElement("td");
    tdDevs.textContent = Object.keys(step.devices || {}).length;

    tr.appendChild(tdIdx);
    tr.appendChild(tdName);
    tr.appendChild(tdDelay);
    tr.appendChild(tdFade);
    tr.appendChild(tdDevs);

    tr.onclick = (e) => {
      if (e.ctrlKey || e.metaKey) {
        // Ctrl+click: toggle this cue in multi-selection
        if (selectedCueIndices.has(idx)) {
          selectedCueIndices.delete(idx);
        } else {
          selectedCueIndices.add(idx);
        }
        // Also set as primary if adding
        if (selectedCueIndices.has(idx)) {
          selectedCueIndex = idx;
        } else if (selectedCueIndex === idx) {
          // If removing primary, pick another from multi-selection
          selectedCueIndex = selectedCueIndices.size > 0 ? [...selectedCueIndices][0] : null;
        }
      } else if (e.shiftKey && lastClickedCueIndex != null) {
        // Shift+click: select range
        const start = Math.min(lastClickedCueIndex, idx);
        const end = Math.max(lastClickedCueIndex, idx);
        for (let i = start; i <= end; i++) {
          selectedCueIndices.add(i);
        }
        selectedCueIndex = idx;
      } else {
        // Normal click: single selection
        selectedCueIndices.clear();
        selectedCueIndices.add(idx);
        selectedCueIndex = idx;
      }
      lastClickedCueIndex = idx;
      fillCuePropsFromSelected();
      renderCueTable();
    };

    tr.ondblclick = () => {
      selectedCueIndices.clear();
      selectedCueIndices.add(idx);
      selectedCueIndex = idx;
      fillCuePropsFromSelected();
      renderCueTable();
      loadCueIntoUIAndRun(idx);
    };

    tbody.appendChild(tr);
  });

  updateCueSelectionCount();
  updatePlayFromButtonState();
  updatePlayingHighlight();
}

function updateCueSelectionCount() {
  const countEl = $id("cue-selection-count");
  if (countEl) {
    const count = selectedCueIndices.size;
    countEl.textContent = count > 1 ? `(${count} selected)` : "";
  }
}

///////////////////////
// LOOP GROUPS
///////////////////////

let nextLoopGroupId = 1;

function createLoopGroup() {
  const count = selectedCueIndices.size;
  if (count < 2) {
    toast("Select at least 2 cues to create a loop group.", "error");
    return;
  }

  const loopCountInput = $id("loop-count-input");
  const loopCount = parseInt(loopCountInput?.value) || 2;

  const sortedIndices = [...selectedCueIndices].sort((a, b) => a - b);

  // Check if indices are contiguous
  for (let i = 1; i < sortedIndices.length; i++) {
    if (sortedIndices[i] !== sortedIndices[i-1] + 1) {
      toast("Selected cues must be contiguous for loop group.", "error");
      return;
    }
  }

  const groupId = `loop_${nextLoopGroupId++}`;

  // Mark all selected cues with the loop group
  for (const idx of sortedIndices) {
    const step = cuesObj.sequence[idx];
    step.loopGroup = groupId;
    step.loopCount = loopCount;
  }

  renderCueTable();
  toast(`Loop group created (${count} cues, ${loopCount}x)`, "success");
}

function removeLoopGroup() {
  const count = selectedCueIndices.size;
  if (count === 0) {
    toast("Select cues to remove from loop group.", "error");
    return;
  }

  let removed = 0;
  for (const idx of selectedCueIndices) {
    const step = cuesObj.sequence[idx];
    if (step.loopGroup) {
      delete step.loopGroup;
      delete step.loopCount;
      removed++;
    }
  }

  if (removed > 0) {
    renderCueTable();
    toast(`Removed ${removed} cue(s) from loop group`, "info");
  } else {
    toast("No loop groups found in selection.", "warning");
  }
}

function newJSON() {
  cuesObj = {
    loop: false,
    loop_count: null,
    devices_def: {},
    virtual_groups: {},
    sequence: []
  };
  currentCueFilename = null;
  selectedCueIndex = null;
  selectedCueIndices.clear();
  virtualGroups = cuesObj.virtual_groups;
  nextVirtualGroupId = 1;
  nextLoopGroupId = 1;
  deviceCurrentGroups = {};
  renderCueTable();
  fillCuePropsFromSelected();
  if (typeof renderActualEffectsPanel === "function") {
    renderActualEffectsPanel();
  }
  toast("New cue list created", "success");
}

///////////////////////
// PLAYBACK CONTROL
///////////////////////

async function runCuesFromUI() {
  const seq = cuesObj.sequence || [];
  if (!seq.length) return toast("No cues to play.", "error");
  if (isBackendRenderMode()) {
    if (playbackActive) return toast("Playback already active.", "warning");
    try {
      await sendBackendCuePayload(buildBackendCuePayloadFromCurrentState());
      playbackActive = true;
      playbackPaused = false;
      playbackPhase = "waiting";
      playbackCueIndex = -1;
      playbackWaitRemaining = 0;
      liveWaitAdjust = 0;
      startEffectRenderLoop();
      showPlaybackBar();
      updatePlaybackUI();
      await runBackendSequence(seq, 0);
      toast("Playback started", "info");
    } catch (e) {
      playbackActive = false;
      hidePlaybackBar();
      updatePlaybackUI();
      console.error("[BACKEND-PLAYBACK]", e);
      toast("Backend playback failed", "error");
    }
    return;
  }
  if (playbackActive) return toast("Playback déjà actif.", "warning");
  
  playbackActive = true;
  uiFollowStopFlag = false;
  const runId = ++uiFollowRunId;

  // Démarrer la boucle d'animation des effets (si pas déjà démarrée)
  startEffectRenderLoop();
  
  // Pilotage 100% côté UI
  uiFollowSequence(seq, runId, 0).catch(e => console.error("[UI-FOLLOW]", e));
  toast("Playback started", "info");
  showPlaybackBar();
}

async function stopRun() {
  uiFollowStopFlag = true;
  uiFollowRunId++;
  playbackActive = false;
  playbackPaused = false;
  playbackCueIndex = -1;
  playbackPhase = "idle";
  liveWaitAdjust = 0;
  skipToNextCue = false;
  backendPlaybackPlan = [];
  backendLastCueToken = 0;
  backendPlaybackStarting = false;

  // Libérer le verrou DMX au cas où
  window.dmxLocked = false;

  hidePlaybackBar();
  updatePlaybackUI();

  if (isBackendRenderMode()) {
    await fetch("/api/playback/stop", { method: "POST" });
  } else {
    await fetch("/api/stop_run", { method: "POST" });
  }
  toast("Stopped", "info");
}

function resetRigStateForPlayback() {
  deviceLocalValues = {};
  deviceCurrentGroups = {};

  for (const [devId, dev] of Object.entries(rigDevices)) {
    const fi = fixtures[dev.fixture] || {};
    const addrCount = fi.addr_count || 1;
    const local = {};
    for (let li = 0; li < addrCount; li++) local[li] = 0;
    deviceLocalValues[devId] = local;
    deviceCurrentGroups[devId] = new Set();
  }

  if (typeof renderActualEffectsPanel === "function") {
    renderActualEffectsPanel();
  }
}

function applyCueFinalStateInstant(step) {
  if (!step) return;

  for (const devId of Object.keys(step.devices || {})) {
    const dev = rigDevices[devId];
    if (!dev) continue;
    deviceLocalValues[devId] = localValuesFromStepForDevice(dev, step);
  }

  restoreDeviceGroupsFromStep(step);
}

function fastForwardSequenceToIndex(seq, targetIdx) {
  let i = 0;
  while (i < targetIdx && i < seq.length) {
    const step = seq[i];
    if (step.loopGroup) {
      const groupId = step.loopGroup;
      const groupStart = i;
      let groupEnd = i;
      while (groupEnd + 1 < seq.length && seq[groupEnd + 1].loopGroup === groupId) {
        groupEnd++;
      }
      const loopCount = step.loopCount || 1;

      for (let loopIter = 0; loopIter < loopCount; loopIter++) {
        for (let j = groupStart; j <= groupEnd && j < targetIdx; j++) {
          applyCueFinalStateInstant(seq[j]);
        }
        if (targetIdx <= groupEnd) break; // Target is inside this loop group
      }

      i = groupEnd + 1;
    } else {
      applyCueFinalStateInstant(step);
      i++;
    }
  }
}

function findLoopGroupStart(seq, idx) {
  const step = seq[idx];
  if (!step?.loopGroup) return idx;

  let start = idx;
  while (start - 1 >= 0 && seq[start - 1]?.loopGroup === step.loopGroup) {
    start--;
  }
  return start;
}

async function playFromSelectedCue() {
  const seq = cuesObj.sequence || [];
  if (!seq.length) return toast("No cues to play.", "error");
  if (selectedCueIndex == null) return toast("Select a cue first.", "error");

  // If selection is inside a loop group, start from the beginning of that group
  const startIdx = findLoopGroupStart(seq, Math.min(selectedCueIndex, seq.length - 1));

  if (playbackActive) {
    await stopRun();
  }

  liveWaitAdjust = 0;
  playbackPaused = false;
  skipToNextCue = false;

  resetRigStateForPlayback();
  fastForwardSequenceToIndex(seq, startIdx);

  drawRig();
  syncRgbWidgetFromFirstDevice();
  syncPosWidgetFromFirstDevice();
  refreshControllerFromSelection();
  if (isBackendRenderMode()) {
    try {
      await sendBackendCuePayload(buildBackendCuePayloadFromCurrentState());
      playbackActive = true;
      playbackCueIndex = startIdx;
      playbackPhase = "waiting";
      playbackWaitRemaining = 0;
      startEffectRenderLoop();
      showPlaybackBar();
      updatePlaybackUI();
      await runBackendSequence(seq.slice(startIdx), startIdx);
      toast(`Playback from cue ${startIdx + 1}`, "info");
    } catch (e) {
      playbackActive = false;
      hidePlaybackBar();
      updatePlaybackUI();
      console.error("[BACKEND-PLAYBACK]", e);
      toast("Backend playback failed", "error");
    }
    return;
  }
  await sendToEngineWithEffects(1.0);

  playbackActive = true;
  const runId = ++uiFollowRunId;
  playbackCueIndex = startIdx;

  startEffectRenderLoop();
  showPlaybackBar();
  updatePlaybackUI();

  uiFollowSequence(seq.slice(startIdx), runId, startIdx).catch(e => console.error("[UI-FOLLOW]", e));
  toast(`Playback from cue ${startIdx + 1}`, "info");
}

// ============================================================================
// PLAYBACK UI FUNCTIONS
// ============================================================================

function showPlaybackBar() {
  const bar = $id("playback-bar");
  if (bar) bar.classList.remove("hidden");
  updatePlaybackButtons();
}

function hidePlaybackBar() {
  const bar = $id("playback-bar");
  if (bar) bar.classList.add("hidden");
  updatePlaybackButtons();
}

function updatePlaybackButtons() {
  const pauseBtn = $id("pause-cues");
  const skipBtn = $id("skip-cue");

  if (pauseBtn) {
    pauseBtn.disabled = !playbackActive;
    pauseBtn.textContent = playbackPaused ? "▶ Resume" : "⏸ Pause";
  }
  if (skipBtn) {
    skipBtn.disabled = !playbackActive;
  }
  updatePlayFromButtonState();
}

function updatePlayFromButtonState() {
  const btn = $id("cue-play-from");
  if (!btn) return;
  const hasSelection = selectedCueIndex != null;
  btn.disabled = !hasSelection || playbackActive || backendPlaybackStarting;
}

// NOTE: a second updatePlaybackUI() is defined later in this file at line 2709
// and (because function declarations hoist last-wins) is the one actually used.
// The duplicate kept here as a stub for any direct in-file reference.
// (Real implementation lives below.)

function updatePlayingHighlight() {
  const tbody = $id("cue-table-body");
  if (tbody) {
    const rows = Array.from(tbody.children);
    rows.forEach(tr => tr.classList.remove("playing"));
    if (playbackCueIndex != null && playbackCueIndex >= 0) {
      const row = rows.find(tr => parseInt(tr.dataset.index, 10) === playbackCueIndex);
      if (row) row.classList.add("playing");
    }
  }

  document.querySelectorAll(".timeline-block.playing").forEach((el) => el.classList.remove("playing"));
  if (playbackCueIndex == null || playbackCueIndex < 0) return;
  document.querySelectorAll(`.timeline-block[data-source-index="${playbackCueIndex}"]`).forEach((el) => {
    el.classList.add("playing");
  });
}

async function handleBackendCueStart(playbackState) {
  const planIndex = parseInt(playbackState?.plan_index, 10);
  if (!Number.isFinite(planIndex) || planIndex < 0 || planIndex >= backendPlaybackPlan.length) return;

  const step = backendPlaybackPlan[planIndex];
  if (!step) return;

  for (const id of Object.keys(rigDevices)) {
    const dev = rigDevices[id];
    if (!dev) continue;
    deviceLocalValues[id] = localValuesFromStepForDevice(dev, step);
  }
  restoreDeviceGroupsFromStep(step);
  drawRig();
  refreshControllerFromSelection();

  try {
    await runSyncVideoCue(step);
  } catch (err) {
    console.warn("[SYNC-VIDEO] cue action failed:", err);
  }
}

window.applyBackendPlaybackState = function applyBackendPlaybackState(playbackState) {
  if (!playbackState || typeof playbackState !== "object") return;

  const nextActive = Boolean(playbackState.active);
  if (!nextActive && backendPlaybackStarting) return;

  playbackActive = nextActive;
  playbackPaused = Boolean(playbackState.paused);
  playbackPhase = playbackState.phase || "idle";
  const cueIndex = Number(playbackState.cue_index);
  playbackCueIndex = Number.isFinite(cueIndex) ? cueIndex : -1;
  playbackWaitRemaining = Math.max(0, parseInt(playbackState.wait_remaining_ms, 10) || 0);
  liveWaitAdjust = parseInt(playbackState.wait_adjust_ms, 10) || 0;

  if (playbackActive) {
    backendPlaybackStarting = false;
    showPlaybackBar();
  } else {
    hidePlaybackBar();
    backendPlaybackPlan = [];
    backendLastCueToken = 0;
  }
  updatePlaybackUI();

  const cueToken = parseInt(playbackState.cue_token, 10) || 0;
  if (!playbackActive || playbackPhase !== "fading" || cueToken <= 0 || cueToken === backendLastCueToken) return;
  backendLastCueToken = cueToken;
  handleBackendCueStart(playbackState).catch((err) => console.warn("[BACKEND-CUE]", err));
};

function loadCueIntoUIAndRun(idx) {
  const step = cuesObj.sequence[idx];
  if (!step?.devices) return;

  const order = Array.isArray(step.device_order) && step.device_order.length
    ? step.device_order.map(String)
    : Object.keys(step.devices);

  selectedDeviceOrder = order.filter(id => rigDevices[id]);
  selectedDeviceSet = new Set(selectedDeviceOrder);

  for (const id of Object.keys(rigDevices)) {
    const dev = rigDevices[id];
    if (!dev) continue;
    deviceLocalValues[id] = localValuesFromStepForDevice(dev, step);
  }

  restoreDeviceGroupsFromStep(step);
  drawRig();
  refreshControllerFromSelection();

  playbackActive = true;
  const runId = ++uiFollowRunId;
  uiFollowStopFlag = false;

  // Démarrer la boucle d'animation des effets si besoin
  startEffectRenderLoop();

  // Lecture d'une seule cue en pilotage 100% UI
  uiFollowSequence([step], runId, idx).catch(e => console.error("[UI-FOLLOW]", e));
  toast(`Played ${step.name || "cue"}`, "info");
}

///////////////////////
// HELPERS
///////////////////////

function localValuesFromStepForDevice(dev, step) {
  const entry = (step.devices || {})[dev.id] || {};
  const ch = entry.channels || {};
  const fi = fixtures[dev.fixture] || {};
  const addrCount = fi.addr_count || 1;
  const base = dev.address || 0;
  
  const local = {};
  for (let li = 0; li < addrCount; li++) {
    const absCh = base + li;
    local[li] = ch[String(absCh)] != null ? parseInt(ch[String(absCh)], 10) || 0 : 0;
  }
  return local;
}

function restoreDeviceGroupsFromStep(step) {
  // Only clear/replace effects for devices that are EXPLICITLY in the new cue
  // Devices not in the cue keep their current effects

  const map = step.device_groups || {};
  const devicesInCue = new Set(Object.keys(step.devices || {}));

  for (const id of Object.keys(rigDevices)) {
    // Only clear if this device is in the new cue (has values or effects)
    if (devicesInCue.has(id) || map[id]) {
      deviceCurrentGroups[id] = new Set();
    }
    // Otherwise, keep existing effects
  }

  // Apply new effects from cue
  for (const [devId, groups] of Object.entries(map)) {
    if (!Array.isArray(groups)) continue;
    if (!deviceCurrentGroups[devId]) deviceCurrentGroups[devId] = new Set();
    groups.forEach(g => deviceCurrentGroups[devId].add(g));
  }

  if (typeof renderActualEffectsPanel === "function") {
    renderActualEffectsPanel();
  }
}

/**
 * Calcule le pattern de fade à partir d'un champ duration.
 * 
 * Exemples :
 *  "500"          -> baseFadeMs=500, totalMs=500, offsets=0
 *  "500 > 5000"   -> baseFadeMs=500, totalMs=5500, offsets selon ordre
 *  "500 < 5000"   -> idem mais ordre inversé
 *  "500 >< 5000"  -> extrémités -> centre
 *  "500 <> 5000"  -> centre -> extrémités
 *  "500 | 5000"   -> 1 device sur 2 (alternance)
 *  "500 || 5000"  -> moitié / moitié
 *  "500 ? 5000"   -> ordre aléatoire
 */
function computeFadePattern(fadeField, deviceIds) {
  const ids = deviceIds || [];
  const str = String(fadeField ?? "0").trim();

  let baseFadeMs = 0;
  let spreadMs = 0;
  let op = null;

  let parts = [];
  if (str.includes("><")) {
    op = "><";
    parts = str.split("><");
  } else if (str.includes("<>")) {
    op = "<>";
    parts = str.split("<>");
  } else if (str.includes("||")) {
    op = "||";
    parts = str.split("||");
  } else if (str.includes("|")) {
    op = "|";
    parts = str.split("|");
  } else if (str.includes(">")) {
    op = ">";
    parts = str.split(">");
  } else if (str.includes("<")) {
    op = "<";
    parts = str.split("<");
  } else if (str.includes("?")) {
    op = "?";
    parts = str.split("?");
  }

  if (op && parts.length >= 2) {
    baseFadeMs = parseInt(String(parts[0]).trim(), 10) || 0;
    spreadMs = parseInt(String(parts[1]).trim(), 10) || 0;
  } else if (/^\d+$/.test(str)) {
    baseFadeMs = parseInt(str, 10) || 0;
  } else {
    baseFadeMs = 0;
  }

  const offsets = {};
  const n = ids.length;

  if (n <= 1 || spreadMs <= 0 || !op) {
    for (const id of ids) offsets[id] = 0;
    return { baseFadeMs, totalMs: baseFadeMs, offsets };
  }

  const indices = [];

  if (op === ">" || op === "<") {
    for (let i = 0; i < n; i++) indices.push(i);
    if (op === "<") indices.reverse();
  } else if (op === "><") {
    // extrémités -> centre
    let left = 0, right = n - 1;
    while (left <= right) {
      if (left === right) {
        indices.push(left);
      } else {
        indices.push(left);
        indices.push(right);
      }
      left++;
      right--;
    }
  } else if (op === "<>") {
    // centre -> extrémités
    if (n % 2 === 1) {
      const mid = (n - 1) / 2;
      indices.push(mid);
      for (let step = 1; step <= mid; step++) {
        if (mid - step >= 0) indices.push(mid - step);
        if (mid + step < n) indices.push(mid + step);
      }
    } else {
      const midLeft = n / 2 - 1;
      const midRight = n / 2;
      indices.push(midLeft, midRight);
      for (let step = 1; step <= midLeft; step++) {
        if (midLeft - step >= 0) indices.push(midLeft - step);
        if (midRight + step < n) indices.push(midRight + step);
      }
    }
  } else if (op === "|") {
    // 1 device sur 2 (alternance)
    for (let i = 0; i < n; i++) {
      const devId = ids[i];
      offsets[devId] = (i % 2 === 0) ? 0 : spreadMs;
    }
    return { baseFadeMs, totalMs: baseFadeMs + spreadMs, offsets };
  } else if (op === "||") {
    // moitié / moitié
    const split = Math.ceil(n / 2);
    for (let i = 0; i < n; i++) {
      const devId = ids[i];
      offsets[devId] = i < split ? 0 : spreadMs;
    }
    return { baseFadeMs, totalMs: baseFadeMs + spreadMs, offsets };
  } else if (op === "?") {
    for (let i = 0; i < n; i++) indices.push(i);
    // shuffle Fisher-Yates
    for (let i = n - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [indices[i], indices[j]] = [indices[j], indices[i]];
    }
  } else {
    for (let i = 0; i < n; i++) indices.push(i);
  }

  const denom = Math.max(n - 1, 1);
  indices.forEach((idxInOrder, rank) => {
    const devId = ids[idxInOrder];
    const offset = (spreadMs * rank) / denom;
    offsets[devId] = offset;
  });

  return { baseFadeMs, totalMs: baseFadeMs + spreadMs, offsets };
}

///////////////////////
// UI FOLLOW SEQUENCE
///////////////////////

function isBackendRenderMode() {
  return typeof window.isBackendMode === "function" && window.isBackendMode();
}

function buildBackendEffectGroupsFromStep(step) {
  const map = step?.device_groups || {};
  const groupIds = new Set();
  for (const groups of Object.values(map)) {
    if (Array.isArray(groups)) groups.forEach(g => groupIds.add(g));
  }
  const groups = [];
  for (const gid of groupIds) {
    const g = virtualGroups[gid];
    if (!g) continue;
    const clone = { ...g };
    clone.id = String(clone.id || gid);
    clone.mode = String(clone.mode || "legacy").toLowerCase() === "intelligent" ? "intelligent" : "legacy";
    if (Array.isArray(clone.deviceIds)) {
      clone.deviceIds = clone.deviceIds.map(String);
    }
    if (Array.isArray(clone.selection_groups)) {
      clone.selection_groups = clone.selection_groups.map(sg => Array.isArray(sg) ? sg.map(String) : []);
    }
    groups.push(clone);
  }
  return groups;
}

function buildBackendCuePayload(step) {
  const devices = step?.devices || {};
  const order = Array.isArray(step?.device_order) && step.device_order.length
    ? step.device_order.map(String)
    : Object.keys(devices || {});
  const effectGroups = buildBackendEffectGroupsFromStep(step);
  return {
    devices,
    duration: step?.duration ?? "0",
    device_order: order,
    effect_groups: effectGroups
  };
}

function buildBackendCuePayloadFromCurrentState() {
  const devices = {};
  const deviceGroups = {};
  const order = [];

  for (const id of Object.keys(rigDevices)) {
    const dev = rigDevices[id];
    if (!dev) continue;

    order.push(String(id));

    const fi = fixtures[dev.fixture] || {};
    const addrCount = fi.addr_count || 1;
    const localVals = deviceLocalValues[id] || {};
    const channels = { Universe: dev.universe };

    for (let li = 0; li < addrCount; li++) {
      const absCh = dev.address + li;
      channels[String(absCh)] = localVals[li] ?? 0;
    }

    devices[id] = { channels };
    deviceGroups[id] = Array.from(deviceCurrentGroups[id] || []);
  }

  return buildBackendCuePayload({
    devices,
    device_order: order,
    device_groups: deviceGroups,
    duration: "0",
  });
}

async function sendBackendCuePayload(cue) {
  const payload = { cue, device_order: cue?.device_order || [] };
  const res = await fetch("/api/playback/go", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || "backend cue failed");
  }
}

function flattenPlaybackSequence(seq, indexOffset = 0) {
  const out = [];
  if (!Array.isArray(seq)) return out;

  let i = 0;
  while (i < seq.length) {
    const step = seq[i];
    if (!step || typeof step !== "object") {
      i++;
      continue;
    }

    if (step.loopGroup) {
      const groupId = step.loopGroup;
      const groupStart = i;
      let groupEnd = i;
      while (groupEnd + 1 < seq.length && seq[groupEnd + 1]?.loopGroup === groupId) {
        groupEnd++;
      }
      const loopCount = Math.max(1, parseInt(step.loopCount, 10) || 1);
      for (let loopIter = 0; loopIter < loopCount; loopIter++) {
        for (let j = groupStart; j <= groupEnd; j++) {
          const loopStep = seq[j];
          if (!loopStep || typeof loopStep !== "object") continue;
          out.push({
            ...loopStep,
            playback_index: j + indexOffset,
          });
        }
      }
      i = groupEnd + 1;
      continue;
    }

    out.push({
      ...step,
      playback_index: i + indexOffset,
    });
    i++;
  }

  return out;
}

async function sendCueToBackend(step) {
  const cue = buildBackendCuePayload(step);
  await sendBackendCuePayload(cue);
}

async function runBackendSequence(seq, indexOffset = 0) {
  backendPlaybackPlan = flattenPlaybackSequence(seq, indexOffset);
  backendLastCueToken = 0;
  backendPlaybackStarting = true;
  if (backendPlaybackPlan.length) {
    const firstCueIndex = Number(backendPlaybackPlan[0]?.playback_index);
    if (Number.isFinite(firstCueIndex)) {
      playbackCueIndex = firstCueIndex;
    }
    if (playbackPhase === "waiting") {
      playbackWaitRemaining = Math.max(0, parseInt(backendPlaybackPlan[0]?.sleep, 10) || 0);
    }
    updatePlaybackUI();
  }

  const payload = {
    sequence: backendPlaybackPlan.map((step) => {
      const cue = buildBackendCuePayload(step);
      return {
        ...cue,
        sleep: step?.sleep ?? "0",
        playback_index: step?.playback_index ?? -1,
        sync_video: step?.sync_video || null,
      };
    }),
  };

  const res = await fetch("/api/playback/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    backendPlaybackStarting = false;
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || "backend playback failed");
  }
}

async function controlBackendPlayback(action, deltaMs = 0, extraPayload = null) {
  const res = await fetch("/api/playback/control", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      action,
      delta_ms: deltaMs,
      ...(extraPayload && typeof extraPayload === "object" ? extraPayload : {}),
    }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || "backend playback control failed");
  }
}

async function uiFollowSequence(seq, runId, indexOffset = 0) {
  uiFollowStopFlag = false;
  skipToNextCue = false;
  if (!Array.isArray(seq)) return;
  const plan = flattenPlaybackSequence(seq, indexOffset);
  
  for (let planIndex = 0; planIndex < plan.length; planIndex++) {
    const step = plan[planIndex];
    if (uiFollowStopFlag || runId !== uiFollowRunId) break;
    playbackCueIndex = Number.isFinite(Number(step?.playback_index))
      ? Number(step.playback_index)
      : -1;
    updatePlaybackUI();
    await uiFollowStep(step, runId, {
      hasNext: planIndex < plan.length - 1,
    });
  }

  if (runId === uiFollowRunId) {
    playbackActive = false;
    playbackCueIndex = -1;
    playbackPhase = "idle";
    hidePlaybackBar();
    updatePlaybackUI();
  }
}

// ============================================================================
// DOUBLE FADE avec VERROU DMX + presets de transition + mix d'effets
// ============================================================================

async function uiFollowStep(step, runId, timeline = null) {
  if (isBackendRenderMode()) {
    return await uiFollowStepBackend(step, runId);
  }
  if (uiFollowStopFlag || runId !== uiFollowRunId) return;

  const hasTimeline = Boolean(
    timeline &&
    Number.isFinite(timeline.anchorMs) &&
    Number.isFinite(timeline.cursorMs)
  );
  const sleepMs = Math.max(0, parseInt(step.sleep, 10) || 0);

  // 1. SLEEP (SANS verrou) avec support pause/skip/adjust
  let pauseStartedMs = null;
  let waitBaseTargetMs = hasTimeline
    ? timeline.anchorMs + timeline.cursorMs + sleepMs
    : performance.now() + sleepMs;
  const totalSleepMs = Math.max(0, sleepMs + liveWaitAdjust);

  if (totalSleepMs > 0) {
    playbackPhase = "waiting";
    updatePlaybackUI();

    while (true) {
      if (uiFollowStopFlag || runId !== uiFollowRunId) return;
      if (skipToNextCue) {
        skipToNextCue = false;
        if (hasTimeline) {
          timeline.cursorMs = Math.max(0, performance.now() - timeline.anchorMs);
        }
        break;
      }

      const now = performance.now();
      if (playbackPaused) {
        if (pauseStartedMs == null) pauseStartedMs = now;
        updatePlaybackUI();
        await new Promise(r => setTimeout(r, 50));
        continue;
      }

      if (pauseStartedMs != null) {
        const pausedForMs = now - pauseStartedMs;
        if (hasTimeline) {
          timeline.anchorMs += pausedForMs;
        }
        waitBaseTargetMs += pausedForMs;
        pauseStartedMs = null;
      }

      const waitTargetMs = waitBaseTargetMs + liveWaitAdjust;
      playbackWaitRemaining = Math.max(0, waitTargetMs - now);
      updatePlaybackUI();

      if (now >= waitTargetMs) {
        if (hasTimeline) {
          timeline.cursorMs = Math.max(0, waitTargetMs - timeline.anchorMs);
        }
        break;
      }
      await new Promise(r => setTimeout(r, 20));
    }
  }

  playbackPhase = "fading";
  playbackWaitRemaining = 0;
  updatePlaybackUI();

  await runSyncVideoCue(step);

  if (!step.devices) return;
  
  // === Snapshot des groupes AVANT la cue (état A) ===
  const prevGroupsSnapshot = {};
  for (const devId of Object.keys(rigDevices)) {
    const set = deviceCurrentGroups[devId] || new Set();
    prevGroupsSnapshot[devId] = new Set(set);
  }

  // 2. PRÉPARER LES DONNÉES
  const order = Array.isArray(step.device_order) && step.device_order.length
    ? step.device_order.map(String)
    : Object.keys(step.devices);
  
  const targets = {};
  const startVals = {};
  
  for (const id of order) {
    const dev = rigDevices[id];
    const entry = step.devices[id];
    if (!dev || !entry) continue;
    
    const fi = fixtures[dev.fixture] || {};
    const addrCount = fi.addr_count || 1;
    
    targets[id] = {};
    startVals[id] = {};
    
    for (let li = 0; li < addrCount; li++) {
      const absCh = dev.address + li;
      const vEnd = parseInt(entry.channels?.[String(absCh)], 10) || 0;
      targets[id][li] = vEnd;
      startVals[id][li] = deviceLocalValues[id]?.[li] ?? 0;
    }
  }
  
  // Appliquer les groupes de la cue (état B)
  restoreDeviceGroupsFromStep(step);
  ensureVirtualGroupsRoot?.();

  // === Snapshot des groupes APRÈS la cue (état B) ===
  const nextGroupsSnapshot = {};
  for (const devId of Object.keys(rigDevices)) {
    const set = deviceCurrentGroups[devId] || new Set();
    nextGroupsSnapshot[devId] = new Set(set);
  }

  // Union A ∪ B pour avoir tous les effets actifs pendant le fade
  const unionGroups = {};
  for (const devId of Object.keys(rigDevices)) {
    const sA = prevGroupsSnapshot[devId] || new Set();
    const sB = nextGroupsSnapshot[devId] || new Set();
    unionGroups[devId] = new Set([...sA, ...sB]);
  }

  // Pendant le fade, on se met sur UNION(A,B)
  deviceCurrentGroups = {};
  for (const [devId, setU] of Object.entries(unionGroups)) {
    deviceCurrentGroups[devId] = new Set(setU);
  }

  if (typeof renderActualEffectsPanel === "function") {
    renderActualEffectsPanel();
  }

  // === Calcul du pattern de fade (simple ou preset) ===
  const { baseFadeMs, totalMs: fadeTotalMs, offsets } =
    computeFadePattern(step.duration || "0", order);

  // ========================================
  // 🔒 ACTIVER LE VERROU DMX
  // ========================================
  window.dmxLocked = true;
  console.log(`[CUE] 🔒 DMX LOCKED for ${fadeTotalMs}ms transition (base=${baseFadeMs}ms)`);

  try {
    // 3. CUT (pas de fade)
    if (fadeTotalMs <= 0 || baseFadeMs <= 0) {
      for (const [id, localMap] of Object.entries(targets)) {
        deviceLocalValues[id] ||= {};
        for (const [li, v] of Object.entries(localMap)) {
          deviceLocalValues[id][parseInt(li, 10)] = v;
        }
      }

      // On termine en état B (groupes de la cue)
      deviceCurrentGroups = {};
      for (const [devId, setB] of Object.entries(nextGroupsSnapshot)) {
        deviceCurrentGroups[devId] = new Set(setB);
      }
      
      await sendToEngineWithEffects(1.0);
      
      drawRig();
      syncRgbWidgetFromFirstDevice();
      syncPosWidgetFromFirstDevice();
      return;
    }
    
    // 4. FADE PROGRESSIF avec offsets par device
  let fadeStartMs = hasTimeline
    ? timeline.anchorMs + timeline.cursorMs
    : performance.now();
  let fadeEndMs = fadeStartMs + fadeTotalMs;
  pauseStartedMs = null;
    
  while (!uiFollowStopFlag && runId === uiFollowRunId) {
      // Check for skip during fade
      if (skipToNextCue) {
        skipToNextCue = false;
        // Jump to final state
        for (const [id, localMap] of Object.entries(targets)) {
          deviceLocalValues[id] ||= {};
          for (const [liStr, vEnd] of Object.entries(localMap)) {
            deviceLocalValues[id][parseInt(liStr, 10)] = vEnd;
          }
        }
        if (hasTimeline) {
          timeline.cursorMs = Math.max(0, performance.now() - timeline.anchorMs);
        }
        break;
      }

      const now = performance.now();
      if (playbackPaused) {
        if (pauseStartedMs == null) pauseStartedMs = now;
        updatePlaybackUI();
        await new Promise(r => setTimeout(r, 50));
        continue;
      }

      if (pauseStartedMs != null) {
        const pausedForMs = now - pauseStartedMs;
        if (hasTimeline) {
          timeline.anchorMs += pausedForMs;
        }
        fadeStartMs += pausedForMs;
        fadeEndMs += pausedForMs;
        pauseStartedMs = null;
      }

      const elapsed = now - fadeStartMs;

      // A. Fade des valeurs de base par device
      for (const [id, localMap] of Object.entries(targets)) {
        const offset = offsets[id] || 0;
        const tDev = elapsed - offset;

        let devProgress;
        if (tDev <= 0) devProgress = 0;
        else if (tDev >= baseFadeMs) devProgress = 1;
        else devProgress = tDev / baseFadeMs;

        deviceLocalValues[id] ||= {};
        for (const [liStr, vEnd] of Object.entries(localMap)) {
          const li = parseInt(liStr, 10);
          const v0 = startVals[id][li] ?? 0;
          deviceLocalValues[id][li] = Math.round(v0 + (vEnd - v0) * devProgress);
        }
      }

      // B. Mix d’effets entre A et B, par device
      const groupMix = {};
      for (const devId of Object.keys(rigDevices)) {
        const offset = offsets[devId] || 0;
        const tDev = elapsed - offset;

        let devProgress;
        if (tDev <= 0) devProgress = 0;
        else if (tDev >= baseFadeMs) devProgress = 1;
        else devProgress = tDev / baseFadeMs;

        const sA = prevGroupsSnapshot[devId] || new Set();
        const sB = nextGroupsSnapshot[devId] || new Set();
        const sU = unionGroups[devId] || new Set();

        if (!sU.size) continue;

        const gmForDev = {};
        for (const gId of sU) {
          if (sA.has(gId) && sB.has(gId)) {
            // Même effet dans A et B -> amplitude constante
            gmForDev[gId] = 1;
          } else if (sA.has(gId) && !sB.has(gId)) {
            // Effet seulement dans A -> fade-out
            gmForDev[gId] = 1 - devProgress;
          } else if (!sA.has(gId) && sB.has(gId)) {
            // Effet seulement dans B -> fade-in
            gmForDev[gId] = devProgress;
          }
        }

        if (Object.keys(gmForDev).length) {
          groupMix[devId] = gmForDev;
        }
      }
      
      // C. Appliquer avec effets mixés (phase continue)
      await sendToEngineWithEffects(1.0, groupMix);
      
      drawRig();
      syncRgbWidgetFromFirstDevice();
      syncPosWidgetFromFirstDevice();
      
      if (now >= fadeEndMs) {
        if (hasTimeline) {
          timeline.cursorMs = Math.max(0, fadeEndMs - timeline.anchorMs);
        }
        break;
      }
      await new Promise(r => setTimeout(r, 20));
    }
    
    // 5. FINALISER
    for (const [id, localMap] of Object.entries(targets)) {
      deviceLocalValues[id] ||= {};
      for (const [li, v] of Object.entries(localMap)) {
        deviceLocalValues[id][parseInt(li, 10)] = v;
      }
    }

    // On termine en état B pur (groupes de la cue)
    deviceCurrentGroups = {};
    for (const [devId, setB] of Object.entries(nextGroupsSnapshot)) {
      deviceCurrentGroups[devId] = new Set(setB);
    }

    if (typeof renderActualEffectsPanel === "function") {
      renderActualEffectsPanel();
    }

    await sendToEngineWithEffects(1.0);
    
    drawRig();
    syncRgbWidgetFromFirstDevice();
    syncPosWidgetFromFirstDevice();
    
  } finally {
    // ========================================
    // 🔓 LIBÉRER LE VERROU DMX
    // ========================================
    window.dmxLocked = false;
    console.log('[CUE] 🔓 DMX UNLOCKED');
  }
}

async function uiFollowStep(step, runId, options = {}) {
  if (isBackendRenderMode()) {
    return await uiFollowStepBackend(step, runId);
  }
  if (uiFollowStopFlag || runId !== uiFollowRunId) return;

  const hasNext = options?.hasNext !== false;
  const sleepMs = Math.max(0, parseInt(step.sleep, 10) || 0);
  let pauseStartedMs = null;
  let skipPostWait = false;

  playbackPhase = "fading";
  playbackWaitRemaining = 0;
  updatePlaybackUI();

  await runSyncVideoCue(step);

  if (step.devices) {
    const prevGroupsSnapshot = {};
    for (const devId of Object.keys(rigDevices)) {
      const set = deviceCurrentGroups[devId] || new Set();
      prevGroupsSnapshot[devId] = new Set(set);
    }

    const order = Array.isArray(step.device_order) && step.device_order.length
      ? step.device_order.map(String)
      : Object.keys(step.devices);

    const targets = {};
    const startVals = {};

    for (const id of order) {
      const dev = rigDevices[id];
      const entry = step.devices[id];
      if (!dev || !entry) continue;

      const fi = fixtures[dev.fixture] || {};
      const addrCount = fi.addr_count || 1;

      targets[id] = {};
      startVals[id] = {};

      for (let li = 0; li < addrCount; li++) {
        const absCh = dev.address + li;
        const vEnd = parseInt(entry.channels?.[String(absCh)], 10) || 0;
        targets[id][li] = vEnd;
        startVals[id][li] = deviceLocalValues[id]?.[li] ?? 0;
      }
    }

    restoreDeviceGroupsFromStep(step);
    ensureVirtualGroupsRoot?.();

    const nextGroupsSnapshot = {};
    for (const devId of Object.keys(rigDevices)) {
      const set = deviceCurrentGroups[devId] || new Set();
      nextGroupsSnapshot[devId] = new Set(set);
    }

    const unionGroups = {};
    for (const devId of Object.keys(rigDevices)) {
      const sA = prevGroupsSnapshot[devId] || new Set();
      const sB = nextGroupsSnapshot[devId] || new Set();
      unionGroups[devId] = new Set([...sA, ...sB]);
    }

    deviceCurrentGroups = {};
    for (const [devId, setU] of Object.entries(unionGroups)) {
      deviceCurrentGroups[devId] = new Set(setU);
    }

    if (typeof renderActualEffectsPanel === "function") {
      renderActualEffectsPanel();
    }

    const { baseFadeMs, totalMs: fadeTotalMs, offsets } =
      computeFadePattern(step.duration || "0", order);

    window.dmxLocked = true;
    console.log(`[CUE] DMX LOCKED for ${fadeTotalMs}ms transition (base=${baseFadeMs}ms)`);

    try {
      if (fadeTotalMs <= 0 || baseFadeMs <= 0) {
        for (const [id, localMap] of Object.entries(targets)) {
          deviceLocalValues[id] ||= {};
          for (const [li, v] of Object.entries(localMap)) {
            deviceLocalValues[id][parseInt(li, 10)] = v;
          }
        }

        deviceCurrentGroups = {};
        for (const [devId, setB] of Object.entries(nextGroupsSnapshot)) {
          deviceCurrentGroups[devId] = new Set(setB);
        }

        await sendToEngineWithEffects(1.0);
        drawRig();
        syncRgbWidgetFromFirstDevice();
        syncPosWidgetFromFirstDevice();
      } else {
        let fadeStartMs = performance.now();
        let fadeEndMs = fadeStartMs + fadeTotalMs;
        pauseStartedMs = null;

        while (!uiFollowStopFlag && runId === uiFollowRunId) {
          if (skipToNextCue) {
            skipToNextCue = false;
            skipPostWait = true;
            for (const [id, localMap] of Object.entries(targets)) {
              deviceLocalValues[id] ||= {};
              for (const [liStr, vEnd] of Object.entries(localMap)) {
                deviceLocalValues[id][parseInt(liStr, 10)] = vEnd;
              }
            }
            break;
          }

          const now = performance.now();
          if (playbackPaused) {
            if (pauseStartedMs == null) pauseStartedMs = now;
            updatePlaybackUI();
            await new Promise(r => setTimeout(r, 50));
            continue;
          }

          if (pauseStartedMs != null) {
            const pausedForMs = now - pauseStartedMs;
            fadeStartMs += pausedForMs;
            fadeEndMs += pausedForMs;
            pauseStartedMs = null;
          }

          const elapsed = now - fadeStartMs;

          for (const [id, localMap] of Object.entries(targets)) {
            const offset = offsets[id] || 0;
            const tDev = elapsed - offset;

            let devProgress;
            if (tDev <= 0) devProgress = 0;
            else if (tDev >= baseFadeMs) devProgress = 1;
            else devProgress = tDev / baseFadeMs;

            deviceLocalValues[id] ||= {};
            for (const [liStr, vEnd] of Object.entries(localMap)) {
              const li = parseInt(liStr, 10);
              const v0 = startVals[id][li] ?? 0;
              deviceLocalValues[id][li] = Math.round(v0 + (vEnd - v0) * devProgress);
            }
          }

          const groupMix = {};
          for (const devId of Object.keys(rigDevices)) {
            const offset = offsets[devId] || 0;
            const tDev = elapsed - offset;

            let devProgress;
            if (tDev <= 0) devProgress = 0;
            else if (tDev >= baseFadeMs) devProgress = 1;
            else devProgress = tDev / baseFadeMs;

            const sA = prevGroupsSnapshot[devId] || new Set();
            const sB = nextGroupsSnapshot[devId] || new Set();
            const sU = unionGroups[devId] || new Set();
            if (!sU.size) continue;

            const gmForDev = {};
            for (const gId of sU) {
              if (sA.has(gId) && sB.has(gId)) {
                gmForDev[gId] = 1;
              } else if (sA.has(gId) && !sB.has(gId)) {
                gmForDev[gId] = 1 - devProgress;
              } else if (!sA.has(gId) && sB.has(gId)) {
                gmForDev[gId] = devProgress;
              }
            }

            if (Object.keys(gmForDev).length) {
              groupMix[devId] = gmForDev;
            }
          }

          await sendToEngineWithEffects(1.0, groupMix);
          drawRig();
          syncRgbWidgetFromFirstDevice();
          syncPosWidgetFromFirstDevice();

          if (now >= fadeEndMs) break;
          await new Promise(r => setTimeout(r, 20));
        }

        for (const [id, localMap] of Object.entries(targets)) {
          deviceLocalValues[id] ||= {};
          for (const [li, v] of Object.entries(localMap)) {
            deviceLocalValues[id][parseInt(li, 10)] = v;
          }
        }

        deviceCurrentGroups = {};
        for (const [devId, setB] of Object.entries(nextGroupsSnapshot)) {
          deviceCurrentGroups[devId] = new Set(setB);
        }

        if (typeof renderActualEffectsPanel === "function") {
          renderActualEffectsPanel();
        }

        await sendToEngineWithEffects(1.0);
        drawRig();
        syncRgbWidgetFromFirstDevice();
        syncPosWidgetFromFirstDevice();
      }
    } finally {
      window.dmxLocked = false;
      console.log("[CUE] DMX UNLOCKED");
    }
  }

  if (uiFollowStopFlag || runId !== uiFollowRunId || !hasNext || skipPostWait) return;

  const totalSleepMs = Math.max(0, sleepMs + liveWaitAdjust);
  if (totalSleepMs <= 0) return;

  playbackPhase = "waiting";
  playbackWaitRemaining = totalSleepMs;
  updatePlaybackUI();

  let waitBaseTargetMs = performance.now() + sleepMs;
  pauseStartedMs = null;

  while (true) {
    if (uiFollowStopFlag || runId !== uiFollowRunId) return;
    if (skipToNextCue) {
      skipToNextCue = false;
      break;
    }

    const now = performance.now();
    if (playbackPaused) {
      if (pauseStartedMs == null) pauseStartedMs = now;
      updatePlaybackUI();
      await new Promise(r => setTimeout(r, 50));
      continue;
    }

    if (pauseStartedMs != null) {
      const pausedForMs = now - pauseStartedMs;
      waitBaseTargetMs += pausedForMs;
      pauseStartedMs = null;
    }

    const waitTargetMs = waitBaseTargetMs + liveWaitAdjust;
    playbackWaitRemaining = Math.max(0, waitTargetMs - now);
    updatePlaybackUI();

    if (now >= waitTargetMs) break;
    await new Promise(r => setTimeout(r, 20));
  }
}

async function uiFollowStepBackend(step, runId) {
  if (uiFollowStopFlag || runId !== uiFollowRunId) return;

  const baseSleepMs = parseInt(step.sleep, 10) || 0;
  const totalSleepMs = Math.max(0, baseSleepMs + liveWaitAdjust);

  if (totalSleepMs > 0) {
    playbackPhase = "waiting";
    const start = performance.now();

    while (true) {
      if (uiFollowStopFlag || runId !== uiFollowRunId) return;
      if (skipToNextCue) {
        skipToNextCue = false;
        break;
      }

      while (playbackPaused && !uiFollowStopFlag && runId === uiFollowRunId) {
        updatePlaybackUI();
        await new Promise(r => setTimeout(r, 50));
      }

      const elapsed = performance.now() - start;
      const currentTotal = Math.max(0, baseSleepMs + liveWaitAdjust);
      playbackWaitRemaining = Math.max(0, currentTotal - elapsed);
      updatePlaybackUI();

      if (elapsed >= currentTotal) break;
      await new Promise(r => setTimeout(r, 20));
    }
  }

  playbackPhase = "fading";
  playbackWaitRemaining = 0;
  updatePlaybackUI();

  await runSyncVideoCue(step);

  if (!step.devices) return;

  const order = Array.isArray(step.device_order) && step.device_order.length
    ? step.device_order.map(String)
    : Object.keys(step.devices);

  const { totalMs: fadeTotalMs } = computeFadePattern(step.duration || "0", order);

  window.dmxLocked = true;
  try {
    await sendCueToBackend(step);
  } catch (err) {
    console.warn("[BACKEND] cue send failed:", err);
    if (typeof window.fallbackToUiMode === "function") {
      window.fallbackToUiMode("Backend cue failed, fallback to UI render mode.");
    }
  }

  for (const id of Object.keys(rigDevices)) {
    const dev = rigDevices[id];
    if (!dev) continue;
    deviceLocalValues[id] = localValuesFromStepForDevice(dev, step);
  }
  restoreDeviceGroupsFromStep(step);
  drawRig();
  refreshControllerFromSelection();

  const fadeStart = performance.now();
  while (!uiFollowStopFlag && runId === uiFollowRunId) {
    if (skipToNextCue) {
      skipToNextCue = false;
      break;
    }
    while (playbackPaused && !uiFollowStopFlag && runId === uiFollowRunId) {
      updatePlaybackUI();
      await new Promise(r => setTimeout(r, 50));
    }
    const elapsed = performance.now() - fadeStart;
    playbackWaitRemaining = Math.max(0, fadeTotalMs - elapsed);
    updatePlaybackUI();
    if (elapsed >= fadeTotalMs) break;
    await new Promise(r => setTimeout(r, 20));
  }

  window.dmxLocked = false;
}

// ============================================================================
// FONCTION CENTRALE (seule autorisée pendant verrou)
// ============================================================================

async function sendToEngineWithEffects(effectScale, groupMix) {
  // Sécurité : effetScale dans [0,1]
  const scale = clamp(effectScale ?? 1, 0, 1);

  const tMs = performance.now() - window.effectStartEpoch;
  const perUniverseMap = {};

  devicePreviewRGB = {};
  devicePreviewDimmer = {};

  for (const dev of Object.values(rigDevices)) {
    const fi = fixtures[dev.fixture] || {};
    const absMap = getDeviceAttrAbsChannels(dev);
    const previewChannels = getDevicePrimaryPreviewChannels(dev);
    const lv = deviceLocalValues[dev.id] || {};
    const devGroups = Array.from(deviceCurrentGroups[dev.id] || []);

    const u = dev.universe || 0;
    perUniverseMap[u] ||= {};

    // Base brute (valeurs locales interpolées par uiFollowStep)
    const addrCount = fi.addr_count || 1;
    for (let li = 0; li < addrCount; li++) {
      const absCh = dev.address + li;
      perUniverseMap[u][absCh] = lv[li] ?? 0;
    }

    const gmForDev = groupMix?.[dev.id] || null;

    // Effets : on scale amplitude = amplitude * scale * groupMix[gId] (si présent)
    for (const gId of devGroups) {
      const group = virtualGroups[gId];
      if (!group) continue;

      if (group.mode === "intelligent") {
        const def = window.getIntelligentEffectDefinition
          ? window.getIntelligentEffectDefinition(group.type)
          : null;
        if (def && typeof applyIntelligentGroupToDevice === "function") {
          applyIntelligentGroupToDevice(group, def, dev, tMs, perUniverseMap, {
            scale,
            groupMix: gmForDev
          });
        }
        continue;
      }
      if (typeof applyLegacyGroupToDevice === "function") {
        applyLegacyGroupToDevice(group, dev, tMs, perUniverseMap, {
          scale,
          groupMix: gmForDev
        });
      }
    }

    // Previews
    if (previewChannels.r != null && previewChannels.g != null && previewChannels.b != null) {
      const rAbs = previewChannels.r, gAbs = previewChannels.g, bAbs = previewChannels.b;
      devicePreviewRGB[dev.id] = {
        r: rAbs != null ? (perUniverseMap[u][rAbs] ?? 0) : 0,
        g: gAbs != null ? (perUniverseMap[u][gAbs] ?? 0) : 0,
        b: bAbs != null ? (perUniverseMap[u][bAbs] ?? 0) : 0,
      };
    }

    if (previewChannels.dimmer != null) {
      const dAbs = previewChannels.dimmer;
      devicePreviewDimmer[dev.id] = dAbs != null ? (perUniverseMap[u][dAbs] ?? 0) : 0;
    }
  }
  // NOTE: Identify mode is now handled by Python engine (see /api/identify/*)
  // Pendant un playback de cues, l'UI est la source unique de DMX
  // On utilise bypassLock=true car cette fonction EST la source autorisée pendant lock
  if (!isBackendRenderMode()) {
    for (const [uStr, chMap] of Object.entries(perUniverseMap)) {
      const u = parseInt(uStr, 10) || 0;
      await applyUniverseState(u, chMap, true, "ui_cue"); // bypassLock=true
    }
  }
}

function swapCues(i, j) {
  const seq = cuesObj.sequence || [];
  if (i < 0 || j < 0 || i >= seq.length || j >= seq.length) return;
  const tmp = seq[i];
  seq[i] = seq[j];
  seq[j] = tmp;
}

function moveSelectedCues(delta) {
  const count = selectedCueIndices.size;
  if (count === 0) {
    toast("Select a cue first.", "error");
    return;
  }

  const seq = cuesObj.sequence || [];
  const sortedIndices = [...selectedCueIndices].sort((a, b) => a - b);

  if (delta < 0) {
    // Moving up: check if first selected can move up
    if (sortedIndices[0] + delta < 0) return;

    // Move each cue up, starting from the top
    for (const idx of sortedIndices) {
      swapCues(idx, idx + delta);
    }
  } else {
    // Moving down: check if last selected can move down
    if (sortedIndices[sortedIndices.length - 1] + delta >= seq.length) return;

    // Move each cue down, starting from the bottom
    for (let i = sortedIndices.length - 1; i >= 0; i--) {
      const idx = sortedIndices[i];
      swapCues(idx, idx + delta);
    }
  }

  // Update selection indices
  const newIndices = sortedIndices.map((idx) => idx + delta);
  selectedCueIndices.clear();
  for (const idx of newIndices) selectedCueIndices.add(idx);
  selectedCueIndex = selectedCueIndex != null ? selectedCueIndex + delta : null;

  renderCueTable();
  fillCuePropsFromSelected();
}

// Keep old function name for compatibility
function moveSelectedCue(delta) {
  moveSelectedCues(delta);
}

// Recalcule cuesObj.sequence en fonction de l'ordre visuel (drag & drop)
function applyCueOrderFromDOM() {
  const tbody = $id("cue-table-body");
  if (!tbody) return;

  const seq = cuesObj.sequence || [];
  const oldSeq = [...seq];

  // ordre des indices après drag & drop
  const newOrder = Array.from(tbody.children)
    .map(tr => parseInt(tr.dataset.index, 10))
    .filter(i => !Number.isNaN(i));

  // reconstruire la séquence dans le nouvel ordre
  cuesObj.sequence = newOrder.map(i => oldSeq[i]).filter(Boolean);

  // recaler le selectedCueIndex si besoin
  if (selectedCueIndex != null) {
    const oldSelected = selectedCueIndex;
    const newIndex = newOrder.indexOf(oldSelected);
    selectedCueIndex = newIndex >= 0 ? newIndex : null;
  }

  renderCueTable();
  fillCuePropsFromSelected();
}

function applyMultiCueDragFromDOM(draggedIndex) {
  const tbody = $id("cue-table-body");
  const seq = cuesObj.sequence || [];
  if (!tbody || !cueDragContext || !cueDragContext.selectedSet.has(draggedIndex)) {
    applyCueOrderFromDOM();
    cueDragContext = null;
    return;
  }

  const domOrder = Array.from(tbody.children)
    .map((tr) => parseInt(tr.dataset.index, 10))
    .filter((idx) => Number.isFinite(idx));
  const anchorPos = domOrder.indexOf(draggedIndex);
  if (anchorPos < 0) {
    applyCueOrderFromDOM();
    cueDragContext = null;
    return;
  }

  const movingIndices = cueDragContext.selected;
  const movingItems = movingIndices.map((idx) => seq[idx]).filter(Boolean);
  const remainingItems = seq.filter((_, idx) => !cueDragContext.selectedSet.has(idx));
  const remainingBeforeAnchor = domOrder
    .slice(0, anchorPos)
    .filter((idx) => !cueDragContext.selectedSet.has(idx)).length;
  const insertAt = Math.max(0, Math.min(remainingItems.length, remainingBeforeAnchor));

  remainingItems.splice(insertAt, 0, ...movingItems);
  cuesObj.sequence = remainingItems;

  selectedCueIndices.clear();
  for (let i = 0; i < movingItems.length; i++) {
    selectedCueIndices.add(insertAt + i);
  }

  const primaryOffset = movingIndices.indexOf(cueDragContext.primaryIndex);
  selectedCueIndex = primaryOffset >= 0 ? insertAt + primaryOffset : insertAt;
  cueDragContext = null;

  renderCueTable();
  fillCuePropsFromSelected();
}

// ============================================================================
// BACKEND-OWNED PLAYBACK OVERRIDES
// ============================================================================

function isBackendRenderMode() {
  return window.backendPlaybackOwned || (typeof window.isBackendMode === "function" && window.isBackendMode());
}

function getPlaybackStepByPlanIndex(planIndex) {
  if (!Number.isFinite(planIndex) || planIndex < 0 || planIndex >= backendPlaybackPlan.length) return null;
  return backendPlaybackPlan[planIndex] || null;
}

function getPlaybackCueLabel() {
  if (playbackCueName) return playbackCueName;
  const step = (cuesObj.sequence || [])[playbackCueIndex];
  if (step?.name) return step.name;
  if (playbackCueIndex != null && playbackCueIndex >= 0) return `Cue ${playbackCueIndex + 1}`;
  return "--";
}

function resetRigStateForPlaybackUi() {
  deviceLocalValues = {};
  deviceCurrentGroups = {};

  for (const [devId, dev] of Object.entries(rigDevices)) {
    const fi = fixtures[dev.fixture] || {};
    const addrCount = getFixtureFootprint(fi);
    const local = {};
    for (let li = 0; li < addrCount; li++) local[li] = 0;
    deviceLocalValues[devId] = local;
    deviceCurrentGroups[devId] = new Set();
  }
}

function applyPlaybackUiStep(step) {
  if (!step || typeof step !== "object") return;

  for (const devId of Object.keys(step.devices || {})) {
    const dev = rigDevices[devId];
    if (!dev) continue;
    deviceLocalValues[devId] = localValuesFromStepForDevice(dev, step);
  }
  restoreDeviceGroupsFromStep(step);
}

function rebuildPlaybackUiState(planIndex) {
  if (!Number.isFinite(planIndex) || planIndex < 0) return;
  backendAppliedPlanIndex = Math.min(planIndex, backendPlaybackPlan.length - 1);
}

async function handleBackendCueStart(playbackState) {
  const planIndex = parseInt(playbackState?.plan_index, 10);
  const step = getPlaybackStepByPlanIndex(planIndex);
  if (!step) return;

  if (ctcActive) {
    resetCtcSplitBase(performance.now());
  }

  rebuildPlaybackUiState(planIndex);

  try {
    await runSyncVideoCue(step);
  } catch (err) {
    console.warn("[SYNC-VIDEO] cue action failed:", err);
  }
}

function updatePlaybackButtons() {
  const runBtn = $id("run-cues");
  const stopBtn = $id("stop-cues");
  const skipBtn = $id("skip-cue");
  const playFromBtn = $id("cue-play-from");
  const speedSelect = $id("playback-speed-select");

  if (runBtn) {
    runBtn.disabled = !!backendPlaybackStarting;
    runBtn.classList.remove("playback-state-idle", "playback-state-playing", "playback-state-paused");
    if (!playbackActive) {
      runBtn.textContent = backendPlaybackStarting ? "Starting..." : "Play";
      runBtn.classList.add("playback-state-idle");
    } else if (playbackPaused) {
      runBtn.textContent = "Resume";
      runBtn.classList.add("playback-state-paused");
    } else {
      runBtn.textContent = "Pause";
      runBtn.classList.add("playback-state-playing");
    }
  }
  if (stopBtn) {
    stopBtn.disabled = !playbackActive;
  }
  if (skipBtn) {
    skipBtn.disabled = !playbackActive;
  }
  if (playFromBtn) {
    playFromBtn.disabled = selectedCueIndex == null || playbackActive || backendPlaybackStarting;
  }
  if (speedSelect) {
    speedSelect.disabled = !!(playbackActive || backendPlaybackStarting);
  }
  updatePlayFromButtonState();
}

// Cache for last DOM-written values; skip writes if unchanged. Saves ~5 DOM
// writes per call during 30-60Hz playback ticks when nothing actually moves.
const _playbackUiCache = { cueName: null, phaseText: null, phaseColor: null, countdown: null, adjust: null, speed: null };
function _setIfChanged(el, prop, value, cacheKey) {
  if (!el) return;
  if (_playbackUiCache[cacheKey] === value) return;
  el[prop] = value;
  _playbackUiCache[cacheKey] = value;
}
function _setStyleIfChanged(el, value, cacheKey) {
  if (!el) return;
  if (_playbackUiCache[cacheKey] === value) return;
  el.style.color = value;
  _playbackUiCache[cacheKey] = value;
}

function updatePlaybackUI() {
  const cueNameEl = $id("playback-cue-name");
  const phaseEl = $id("playback-phase");
  const countdownEl = $id("playback-countdown");
  const adjustEl = $id("wait-adjust-display");
  const speedEl = $id("playback-speed-display");
  const liveRemaining = (
    playbackActive &&
    !playbackPaused &&
    playbackPhaseEndHostMs > 0 &&
    (playbackPhase === "waiting" || playbackPhase === "fading" || playbackPhase === "active")
  ) ? Math.max(0, playbackPhaseEndHostMs - Date.now()) : playbackWaitRemaining;

  const cueText = (playbackCueIndex != null && playbackCueIndex >= 0)
    ? `Cue ${playbackCueIndex + 1}: ${getPlaybackCueLabel()}`
    : "--";
  _setIfChanged(cueNameEl, "textContent", cueText, "cueName");

  let phaseText, phaseColor;
  if (playbackPaused)              { phaseText = "PAUSED";  phaseColor = "#fbbf24"; }
  else if (playbackPhase === "waiting") { phaseText = "Waiting"; phaseColor = "#60a5fa"; }
  else if (playbackPhase === "fading")  { phaseText = "Fading";  phaseColor = "#22c55e"; }
  else if (playbackPhase === "active")  { phaseText = "Active";  phaseColor = "#93c5fd"; }
  else                                  { phaseText = "--";      phaseColor = "#94a3b8"; }
  _setIfChanged(phaseEl, "textContent", phaseText, "phaseText");
  _setStyleIfChanged(phaseEl, phaseColor, "phaseColor");

  const countdownText = (playbackActive && liveRemaining > 0 &&
    (playbackPhase === "waiting" || playbackPhase === "fading" || playbackPhase === "active"))
    ? `${Math.ceil(liveRemaining)}ms`
    : "--";
  _setIfChanged(countdownEl, "textContent", countdownText, "countdown");

  const adjustText = `${liveWaitAdjust >= 0 ? "+" : ""}${liveWaitAdjust}ms`;
  _setIfChanged(adjustEl, "textContent", adjustText, "adjust");

  const speedText = `${playbackSpeed}x`;
  _setIfChanged(speedEl, "textContent", speedText, "speed");

  updatePlaybackButtons();
  updatePlayingHighlight();
}

async function runBackendSequence(seq, startIndex = 0) {
  const cleanSequence = Array.isArray(seq) ? seq.filter((step) => step && typeof step === "object") : [];
  if (!cleanSequence.length) {
    throw new Error("empty playback sequence");
  }
  playbackSpeed = getSelectedPlaybackSpeed();
  const ctcStartedForRun = maybeStartCtcForPlayback();

  try {
    if (typeof window.isTimelineEditorMode === "function" && window.isTimelineEditorMode()) {
      const timelineRequest = typeof window.buildTimelinePlaybackRequest === "function"
        ? window.buildTimelinePlaybackRequest(startIndex)
        : null;
      if (!timelineRequest) {
        throw new Error("timeline playback unavailable");
      }

      backendPlaybackPlan = timelineRequest.ui_plan || [];
      backendAppliedPlanIndex = -1;
      backendLastCueToken = 0;
      backendPlaybackStarting = true;
      window.backendPlaybackOwned = true;
      playbackActive = true;
      playbackPaused = false;
      playbackPhase = "idle";
      playbackCueIndex = timelineRequest.start_occurrence?.source_index ?? startIndex;
      playbackCueName = timelineRequest.start_occurrence?.cue_name || cleanSequence[startIndex]?.name || "";
      playbackWaitRemaining = 0;
      playbackPhaseEndHostMs = 0;
      liveWaitAdjust = 0;
      devicePreviewRGB = {};
      devicePreviewDimmer = {};
      if (typeof window.setTimelineCursorMs === "function") {
        window.setTimelineCursorMs(timelineRequest.payload.start_ms || 0, { render: false, ensure_visible: "center" });
      }
      showPlaybackBar();
      updatePlaybackUI();

      const res = await fetch("/api/playback/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...timelineRequest.payload,
          speed: playbackSpeed,
        }),
      });

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || "backend timeline playback failed");
      }
      return;
    }

    if (typeof syncRigToBackend === "function") {
      await syncRigToBackend(true);
    }
    if (typeof buildBackendCuePayloadFromCurrentState === "function" && typeof sendBackendCuePayload === "function") {
      await sendBackendCuePayload(buildBackendCuePayloadFromCurrentState());
    }

    const resolvedStartIndex = findLoopGroupStart(cleanSequence, Math.max(0, Math.min(startIndex, cleanSequence.length - 1)));
    backendPlaybackPlan = flattenPlaybackSequence(cleanSequence, 0);
    backendAppliedPlanIndex = -1;
    backendLastCueToken = 0;
    backendPlaybackStarting = true;
    window.backendPlaybackOwned = true;
    playbackActive = true;
    playbackPaused = false;
    playbackPhase = "idle";
    playbackCueIndex = resolvedStartIndex;
    playbackCueName = cleanSequence[resolvedStartIndex]?.name || `Cue ${resolvedStartIndex + 1}`;
    playbackWaitRemaining = 0;
    playbackPhaseEndHostMs = 0;
    liveWaitAdjust = 0;
    devicePreviewRGB = {};
    devicePreviewDimmer = {};
    showPlaybackBar();
    updatePlaybackUI();

    const res = await fetch("/api/playback/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sequence: cleanSequence,
        start_index: resolvedStartIndex,
        virtual_groups: virtualGroups || {},
        speed: playbackSpeed,
        mode: "classic",
      }),
    });

    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || "backend playback failed");
    }
  } catch (err) {
    if (ctcStartedForRun) {
      stopCtcCapture(true);
    }
    backendPlaybackStarting = false;
    window.backendPlaybackOwned = false;
    backendPlaybackPlan = [];
    playbackActive = false;
    playbackPaused = false;
    playbackPhase = "idle";
    playbackCueIndex = -1;
    playbackCueName = "";
    playbackWaitRemaining = 0;
    playbackPhaseEndHostMs = 0;
    hidePlaybackBar();
    updatePlaybackUI();
    throw err;
  }
}

async function runCuesFromUI() {
  const seq = cuesObj.sequence || [];
  if (!seq.length) return toast("No cues to play.", "error");

  if (playbackActive) {
    if (isBackendRenderMode()) {
      const nextPaused = !playbackPaused;
      try {
        await controlBackendPlayback(nextPaused ? "pause" : "resume");
        toast(nextPaused ? "Playback paused" : "Playback resumed", "info");
      } catch (err) {
        console.warn("[BACKEND-PLAYBACK] pause/resume failed:", err);
        toast("Playback control failed", "error");
      }
      return;
    }
    playbackPaused = !playbackPaused;
    updatePlaybackUI();
    toast(playbackPaused ? "Playback paused" : "Playback resumed", "info");
    return;
  }
  if (backendPlaybackStarting) return toast("Playback already active.", "warning");

  try {
    startEffectRenderLoop();
    const timelineMode = typeof window.isTimelineEditorMode === "function" && window.isTimelineEditorMode();
    await runBackendSequence(seq, timelineMode ? null : 0);
    toast("Playback started", "info");
  } catch (err) {
    console.error("[BACKEND-PLAYBACK]", err);
    toast("Backend playback failed", "error");
  }
}

async function stopRun(silent = false) {
  uiFollowStopFlag = true;
  uiFollowRunId++;
  skipToNextCue = false;
  backendPlaybackStarting = false;
  backendLastCueToken = 0;
  backendPlaybackPlan = [];
  backendAppliedPlanIndex = -1;
  window.backendPlaybackOwned = false;
  playbackActive = false;
  playbackPaused = false;
  playbackCueIndex = -1;
  playbackCueName = "";
  playbackPhase = "idle";
  playbackWaitRemaining = 0;
  playbackPhaseEndHostMs = 0;
  liveWaitAdjust = 0;
  window.dmxLocked = false;
  devicePreviewRGB = {};
  devicePreviewDimmer = {};

  hidePlaybackBar();
  updatePlaybackUI();

  try {
    await fetch("/api/playback/stop", { method: "POST" });
  } catch (err) {
    console.warn("[BACKEND-PLAYBACK] stop failed:", err);
  }

  if (typeof window.resetTimelineInteractionState === "function") {
    window.resetTimelineInteractionState({ stop_scrub_session: false });
  }

  if (!silent) {
    toast("Stopped", "info");
  }
}

async function playFromSelectedCue() {
  const seq = cuesObj.sequence || [];
  if (!seq.length) return toast("No cues to play.", "error");
  if (selectedCueIndex == null) return toast("Select a cue first.", "error");

  const startIdx = findLoopGroupStart(seq, Math.min(selectedCueIndex, seq.length - 1));
  if (playbackActive || backendPlaybackStarting) {
    await stopRun(true);
  }

  try {
    startEffectRenderLoop();
    await runBackendSequence(seq, startIdx);
    toast(`Playback from cue ${startIdx + 1}`, "info");
  } catch (err) {
    console.error("[BACKEND-PLAYBACK]", err);
    toast("Backend playback failed", "error");
  }
}

async function loadCueIntoUIAndRun(idx) {
  const step = (cuesObj.sequence || [])[idx];
  if (!step?.devices) return;

  if (playbackActive || backendPlaybackStarting) {
    await stopRun(true);
  }

  const order = Array.isArray(step.device_order) && step.device_order.length
    ? step.device_order.map(String)
    : Object.keys(step.devices);

  selectedDeviceOrder = order.filter(id => rigDevices[id]);
  selectedDeviceSet = new Set(selectedDeviceOrder);

  for (const devId of Object.keys(step.devices || {})) {
    const dev = rigDevices[devId];
    if (!dev) continue;
    deviceLocalValues[devId] = localValuesFromStepForDevice(dev, step);
  }
  restoreDeviceGroupsFromStep(step);
  drawRig();
  refreshControllerFromSelection();

  try {
    if (typeof window.isBackendMode === "function" && window.isBackendMode()) {
      await sendCueToBackend(step);
    } else {
      await sendToEngineWithEffects(1.0);
    }
    toast(`Played ${step.name || "cue"}`, "info");
  } catch (err) {
    console.error("[CUE-PREVIEW]", err);
    toast("Cue playback failed", "error");
  }
}

window.applyBackendPlaybackState = function applyBackendPlaybackState(playbackState) {
  if (!playbackState || typeof playbackState !== "object") return;

  const nextActive = Boolean(playbackState.active);
  if (!nextActive && backendPlaybackStarting) return;
  const prevPhase = playbackPhase;
  const prevPaused = playbackPaused;

  playbackActive = nextActive;
  playbackPaused = Boolean(playbackState.paused);
  playbackPhase = nextActive ? (playbackState.phase || "idle") : "idle";

  const cueIndex = Number(playbackState.cue_index);
  if (Number.isFinite(cueIndex) && cueIndex >= 0) {
    playbackCueIndex = cueIndex;
  } else if (!nextActive) {
    playbackCueIndex = -1;
  }

  const cueName = String(playbackState.cue_name || "").trim();
  if (cueName) {
    playbackCueName = cueName;
  } else if (!nextActive) {
    playbackCueName = "";
  } else if (!playbackCueName && playbackCueIndex >= 0) {
    playbackCueName = (cuesObj.sequence || [])[playbackCueIndex]?.name || "";
  }
  playbackWaitRemaining = Math.max(0, parseInt(playbackState.phase_remaining_ms, 10) || 0);
  playbackPhaseEndHostMs = Math.max(0, parseInt(playbackState.phase_end_host_ms, 10) || 0);
  liveWaitAdjust = parseInt(playbackState.wait_adjust_ms, 10) || 0;
  if (nextActive) {
    playbackSpeed = normalizePlaybackSpeedValue(playbackState.speed);
  }

  if (typeof window.syncTimelinePlaybackCursor === "function") {
    window.syncTimelinePlaybackCursor(playbackState);
  }

  if (playbackActive) {
    backendPlaybackStarting = false;
    window.backendPlaybackOwned = true;
    showPlaybackBar();
  } else {
    backendPlaybackStarting = false;
    window.backendPlaybackOwned = false;
    playbackCueIndex = -1;
    playbackCueName = "";
    playbackWaitRemaining = 0;
    playbackPhaseEndHostMs = 0;
    liveWaitAdjust = 0;
    backendLastCueToken = 0;
    backendPlaybackPlan = [];
    backendAppliedPlanIndex = -1;
    devicePreviewRGB = {};
    devicePreviewDimmer = {};
    hidePlaybackBar();
  }

  if (
    ctcActive &&
    prevPhase === "fading" &&
    !prevPaused &&
    !playbackPaused &&
    playbackPhase !== "fading"
  ) {
    resetCtcSplitBase(performance.now());
  }
  playbackPrevPhase = playbackPhase;

  updatePlaybackUI();

  const cueToken = parseInt(playbackState.cue_token, 10) || 0;
  if (!playbackActive || playbackPhase !== "fading" || cueToken <= 0 || cueToken === backendLastCueToken) return;
  backendLastCueToken = cueToken;
  handleBackendCueStart(playbackState).catch((err) => console.warn("[BACKEND-CUE]", err));
}


document.addEventListener("DOMContentLoaded", () => {
  normalizeCuePanelPlaybackLayout();
  ensurePlaybackUiTimer();
  const savedSpeed = window.localStorage?.getItem("dmx_playback_speed");
  if (savedSpeed) {
    const parsed = Number.parseFloat(savedSpeed);
    if (Number.isFinite(parsed)) playbackSpeed = normalizePlaybackSpeedValue(parsed);
  }

  updateCtcUI();

  const playbackSpeedSelect = $id("playback-speed-select");
  if (playbackSpeedSelect) {
    const syncPlaybackSpeedFromSelect = () => {
      playbackSpeed = getSelectedPlaybackSpeed();
      try {
        window.localStorage?.setItem("dmx_playback_speed", String(playbackSpeed));
      } catch (err) {
        console.warn("[PLAYBACK] speed persist failed:", err);
      }
      updatePlaybackUI();
};

window.stopCuePlayback = stopRun;
window.isCuePlaybackActive = function isCuePlaybackActive() {
  return Boolean(playbackActive || backendPlaybackStarting);
};
    playbackSpeedSelect.value = playbackSpeedToOptionValue(playbackSpeed);
    playbackSpeedSelect.addEventListener("change", syncPlaybackSpeedFromSelect);
    playbackSpeedSelect.addEventListener("input", syncPlaybackSpeedFromSelect);
  }

  // Boutons Up / Down
  const btnUp = $id("cue-move-up");
  const btnDown = $id("cue-move-down");

  if (btnUp) {
    btnUp.addEventListener("click", () => {
      moveSelectedCue(-1);
    });
  }

  if (btnDown) {
    btnDown.addEventListener("click", () => {
      moveSelectedCue(1);
    });
  }

  const playFromBtn = $id("cue-play-from");
  if (playFromBtn) {
    playFromBtn.addEventListener("click", playFromSelectedCue);
  }

  const ctcStopBtn = $id("ctc-stop");
  if (ctcStopBtn) {
    ctcStopBtn.addEventListener("click", () => {
      stopCtcCapture();
    });
  }

  document.addEventListener("keydown", (ev) => {
    if (!ctcActive) return;
    if (ev.repeat) return;
    if (shouldIgnoreCtcShortcutTarget(ev.target)) return;

    const pressedCode = normalizeCtcKeybindValue(ev.code || ev.key || "");
    if (pressedCode !== ctcKeybind) return;

    ev.preventDefault();
    ev.stopPropagation();
    if (ctcKeyHeld) return;
    ctcKeyHeld = true;
    appendCtcCue(performance.now());
  }, true);

  document.addEventListener("keyup", (ev) => {
    if (!ctcActive) return;
    const releasedCode = normalizeCtcKeybindValue(ev.code || ev.key || "");
    if (releasedCode !== ctcKeybind) return;

    ev.preventDefault();
    ev.stopPropagation();

    const wasHeld = ctcKeyHeld;
    ctcKeyHeld = false;
    if (!ctcCaptureRelease || !wasHeld) return;
    appendCtcCue(performance.now());
  }, true);

  window.addEventListener("blur", () => {
    ctcKeyHeld = false;
  });

  // Drag & drop avec SortableJS
  const tbody = $id("cue-table-body");
  if (tbody && window.Sortable) {
    window.setTimeout(() => {
      if (tbody.dataset.multiCueDragPatched === "1") return;
      const sortable = typeof window.Sortable.get === "function" ? window.Sortable.get(tbody) : null;
      if (!sortable) return;
      tbody.dataset.multiCueDragPatched = "1";
      sortable.option("onStart", (evt) => {
        const draggedIndex = parseInt(evt?.item?.dataset?.index, 10);
        if (!Number.isFinite(draggedIndex) || selectedCueIndices.size <= 1 || !selectedCueIndices.has(draggedIndex)) {
          cueDragContext = null;
          return;
        }
        cueDragContext = {
          draggedIndex,
          primaryIndex: selectedCueIndex,
          selected: [...selectedCueIndices].sort((a, b) => a - b),
          selectedSet: new Set(selectedCueIndices),
        };
      });
      sortable.option("onEnd", (evt) => {
        const draggedIndex = parseInt(evt?.item?.dataset?.index, 10);
        if (cueDragContext && Number.isFinite(draggedIndex) && cueDragContext.selectedSet.has(draggedIndex)) {
          applyMultiCueDragFromDOM(draggedIndex);
          return;
        }
        applyCueOrderFromDOM();
      });
    }, 0);
  }
  if (tbody && window.Sortable) {
    Sortable.create(tbody, {
      animation: 150,
      handle: undefined,  // toute la ligne est draggable
      onEnd: () => {
        // quand on lâche la ligne, on recalcule l'ordre des cues
        applyCueOrderFromDOM();
      }
    });
  }

  // Live wait adjustment buttons (+100ms / -100ms)
  const waitPlusBtn = $id("wait-plus");
  const waitMinusBtn = $id("wait-minus");

  if (waitPlusBtn) {
    waitPlusBtn.addEventListener("click", async () => {
      if (isBackendRenderMode()) {
        try {
          await controlBackendPlayback("adjust_wait", 100);
        } catch (err) {
          console.warn("[BACKEND-PLAYBACK] adjust +100 failed:", err);
        }
        return;
      }
      liveWaitAdjust += 100;
      updatePlaybackUI();
    });
  }

  if (waitMinusBtn) {
    waitMinusBtn.addEventListener("click", async () => {
      if (isBackendRenderMode()) {
        try {
          await controlBackendPlayback("adjust_wait", -100);
        } catch (err) {
          console.warn("[BACKEND-PLAYBACK] adjust -100 failed:", err);
        }
        return;
      }
      liveWaitAdjust -= 100;
      updatePlaybackUI();
    });
  }

  // Pause/Resume button
  const pauseBtn = $id("pause-cues");
  if (pauseBtn) {
    pauseBtn.addEventListener("click", async () => {
      if (!playbackActive) return;
      if (isBackendRenderMode()) {
        const nextPaused = !playbackPaused;
        try {
          await controlBackendPlayback(nextPaused ? "pause" : "resume");
          toast(nextPaused ? "Playback paused" : "Playback resumed", "info");
        } catch (err) {
          console.warn("[BACKEND-PLAYBACK] pause/resume failed:", err);
        }
        return;
      }
      playbackPaused = !playbackPaused;
      updatePlaybackUI();
      toast(playbackPaused ? "Playback paused" : "Playback resumed", "info");
    });
  }

  // Skip to next cue button
  const skipCueBtn = $id("skip-cue");
  if (skipCueBtn) {
    skipCueBtn.addEventListener("click", async () => {
      if (!playbackActive) return;
      if (isBackendRenderMode()) {
        try {
          await controlBackendPlayback("skip");
          toast("Skipping to next cue...", "info");
        } catch (err) {
          console.warn("[BACKEND-PLAYBACK] skip failed:", err);
        }
        return;
      }
      skipToNextCue = true;
      toast("Skipping to next cue...", "info");
    });
  }

  // Loop group buttons
  const createLoopBtn = $id("cue-create-loop");
  const removeLoopBtn = $id("cue-remove-loop");

  if (createLoopBtn) {
    createLoopBtn.addEventListener("click", createLoopGroup);
  }

  if (removeLoopBtn) {
    removeLoopBtn.addEventListener("click", removeLoopGroup);
  }

  updatePlaybackUI();
});

document.addEventListener("DOMContentLoaded", () => {
  // Stop Effects button - clears all live effects from devices
  const stopFxBtn = $id("stop-effects");
  if (stopFxBtn) {
    stopFxBtn.addEventListener("click", async () => {
      // Clear all device effect groups
      for (const devId of Object.keys(rigDevices)) {
        deviceCurrentGroups[devId] = new Set();
      }
      // Re-apply current values without effects
      if (typeof renderActualEffectsPanel === "function") {
        renderActualEffectsPanel();
      }
      try {
        if (
          typeof window.isBackendMode === "function" &&
          window.isBackendMode() &&
          typeof buildBackendCuePayloadFromCurrentState === "function" &&
          typeof sendBackendCuePayload === "function"
        ) {
          await sendBackendCuePayload(buildBackendCuePayloadFromCurrentState());
          if (typeof syncBackendLiveGroups === "function") {
            await syncBackendLiveGroups();
          }
        } else if (typeof sendToEngineWithEffects === "function") {
          await sendToEngineWithEffects(1.0);
        }
      } catch (err) {
        console.warn("[FX] stop effects failed:", err);
      }
      drawRig();
      toast("Effects stopped", "info");
    });
  }

  const identBtn = $id("ident-toggle");
  if (identBtn) {
    identBtn.addEventListener("click", async () => {
      window.identMode = !window.identMode;

      if (window.identMode) {
        // ON: Start identify mode via Python engine
        identBtn.textContent = (typeof t === "function") ? t("header.identOn", "Identify: ON") : "Identify: ON";
        identBtn.classList.add("active");
        toast("Identification mode ON", "info");

        // Stop any playback
        uiFollowStopFlag = true;
        playbackActive = false;

        // Build device list for identify
        const devices = [];
        for (const devId of selectedDeviceOrder) {
          const dev = rigDevices[devId];
          if (!dev) continue;

          const fix = fixtures[dev.fixture];
          if (!fix) continue;

          const universe = dev.universe || 0;
          const absMap = getDeviceAttrAbsChannels(dev);

          // Find dimmer channel
          const dimmerCh = absMap.dimmer ?? null;

          devices.push({
            device_id: devId,
            universe: universe,
            dimmer_channel: dimmerCh
          });
        }

        // Call Python engine to start identify
        try {
          await fetch("/api/identify/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ devices })
          });
          console.log("[IDENT] Started via Python engine");
        } catch (e) {
          console.error("[IDENT] Failed to start:", e);
          toast("Identify start failed", "error");
        }

      } else {
        // OFF: Stop identify mode
        identBtn.textContent = (typeof t === "function") ? t("header.identOff", "Identify: OFF") : "Identify: OFF";
        identBtn.classList.remove("active");
        toast("Identification mode OFF", "info");

        // Call Python engine to stop identify
        try {
          await fetch("/api/identify/stop", {
            method: "POST",
            headers: { "Content-Type": "application/json" }
          });
          console.log("[IDENT] Stopped via Python engine");
        } catch (e) {
          console.error("[IDENT] Failed to stop:", e);
        }
      }
    });
  }
});
