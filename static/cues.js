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
let playbackWaitRemaining = 0;    // Remaining wait time (ms)
let skipToNextCue = false;        // Flag to skip current wait/fade

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
      // Normal playback with effects - send DMX
      await sendToEngineWithEffects(1.0);
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

async function refreshCueFileList(retryCount = 0) {
  const sel = $id("cue-file-select");
  if (!sel) return;
  sel.innerHTML = "";
  
  try {
    const r = await fetch("/api/cue_files", { cache: "no-store" });
    if (!r.ok) throw new Error(`cue_files: ${r.status}`);
    const data = await r.json();
    const files = data.files || [];
    
    for (const f of files) {
      const opt = document.createElement("option");
      opt.value = f;
      opt.textContent = f;
      sel.appendChild(opt);
    }
    
    if (files.length && !currentCueFilename) {
      currentCueFilename = files[0];
      sel.value = currentCueFilename;
      await loadCueFile(currentCueFilename);
    }
    if (!files.length && retryCount < 5) {
      setTimeout(() => refreshCueFileList(retryCount + 1), 1000);
    }
  } catch (e) {
    console.error(e);
    if (retryCount < 5) {
      setTimeout(() => refreshCueFileList(retryCount + 1), 1000);
    }
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
    const r = await fetch(`/api/cues/${encodeURIComponent(filename)}`, { cache: "no-store" });
    if (!r.ok) {
      throw new Error(`load cue failed (${r.status})`);
    }
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
    rebuildRigFromCueFile();
    renderCueTable();
    fillCuePropsFromSelected();
    toast(tfmt("cues.toast.loaded", "Loaded {filename}", { filename }), "success");
  } catch (e) {
    console.error(e);
    toast(t("cues.toast.loadFailed", "Failed to load cue file"), "error");
  }
}

async function saveCurrentCueFile() {
  if (!currentCueFilename) {
    return toast(t("cues.toast.noFileSelected", "No cue file selected."), "error");
  }
  
  cuesObj.devices_def = buildDevicesDefFromRig();
  cuesObj.virtual_groups = virtualGroups;
  
  try {
    await fetch(`/api/cues/${encodeURIComponent(currentCueFilename)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cuesObj),
    });
    toast(t("cues.toast.saved", "Saved"), "success");
  } catch (e) {
    console.error(e);
    toast(t("cues.toast.saveFailed", "Save failed"), "error");
  }
}

async function saveCueFileAs() {
  let name = await promptModal(
    t("cues.prompt.newFilenameTitle", "New cue filename"),
    t("cues.prompt.newFilenameDefault", "New.json"),
    t("cues.prompt.newFilenamePlaceholder", "ex: myshow.json")
  );
  if (!name) return;
  
  name = makeUniqueCueName(name);
  cuesObj.devices_def = buildDevicesDefFromRig();
  cuesObj.virtual_groups = virtualGroups;
  
  try {
    await fetch(`/api/cues/${encodeURIComponent(name)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cuesObj),
    });
    currentCueFilename = name;
    await refreshCueFileList();
    
    const sel = $id("cue-file-select");
    if (sel) sel.value = name;
    toast(tfmt("cues.toast.savedAs", "Saved as {name}", { name }), "success");
  } catch (e) {
    console.error(e);
    toast(t("cues.toast.saveAsFailed", "Save as failed"), "error");
  }
}

///////////////////////
// BUILD DEVICES BLOCK
///////////////////////

function buildDevicesBlockFromSelection() {
  if (selectedDeviceOrder.length === 0) {
    toast(t("cues.toast.selectDevice", "Select at least one device in the rig."), "error");
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
    name: tfmt("cues.defaultIndexedName", "Cue {index}", { index: seq.length + 1 }),
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
  toast(t("cues.toast.added", "Cue added"));
}

function cueUpdateFromSelection() {
  if (selectedCueIndex == null) {
    return toast(t("cues.toast.selectCueFirst", "Select a cue first."), "error");
  }

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
  toast(t("cues.toast.updated", "Cue updated"), "info");
}

async function cueDelete() {
  const count = selectedCueIndices.size;
  if (count === 0) {
    toast(t("cues.toast.selectCueFirst", "Select a cue first."), "error");
    return;
  }

  const msg = count > 1
    ? tfmt("cues.confirm.deleteBodyMulti", "Delete {count} selected cues?", { count })
    : t("cues.confirm.deleteBodySingle", "Delete selected cue?");
  const ok = await confirmModal(t("cues.confirm.deleteTitle", "Delete cue(s)"), msg);
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
  toast(tfmt("cues.toast.deleted", "{count} cue(s) deleted", { count }), "info");
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
    toast(t("cues.toast.selectCueFirst", "Select a cue first."), "error");
    return;
  }

  // Sort indices and duplicate in order, inserting after the last selected
  const sortedIndices = [...selectedCueIndices].sort((a, b) => a - b);
  const insertAfter = sortedIndices[sortedIndices.length - 1];

  const newCues = [];
  for (const idx of sortedIndices) {
    const original = cuesObj.sequence[idx];
    const copy = deepCopyCue(original);
    const baseName = original.name || t("cues.defaultName", "Cue");
    copy.name = tfmt("cues.copyName", "{name} (copy)", { name: baseName });
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
  toast(tfmt("cues.toast.duplicated", "{count} cue(s) duplicated", { count }), "success");
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
  
  renderCueTable();
  toast(t("cues.toast.propsUpdated", "Cue props updated"), "info");
}

let lastClickedCueIndex = null; // For shift+click range selection

function renderCueTable() {
  const tbody = $id("cue-table-body");
  if (!tbody) return;
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
      loopBadge.title = tfmt(
        "cues.loopBadgeTitle",
        "Loop group: {group}, {count}x",
        { group: step.loopGroup, count: step.loopCount || 1 }
      );
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
    countEl.textContent = count > 1
      ? tfmt("cues.selectionCount", "({count} selected)", { count })
      : "";
  }
}

///////////////////////
// LOOP GROUPS
///////////////////////

let nextLoopGroupId = 1;

function createLoopGroup() {
  const count = selectedCueIndices.size;
  if (count < 2) {
    toast(t("cues.toast.loopNeedTwo", "Select at least 2 cues to create a loop group."), "error");
    return;
  }

  const loopCountInput = $id("loop-count-input");
  const loopCount = parseInt(loopCountInput?.value) || 2;

  const sortedIndices = [...selectedCueIndices].sort((a, b) => a - b);

  // Check if indices are contiguous
  for (let i = 1; i < sortedIndices.length; i++) {
    if (sortedIndices[i] !== sortedIndices[i-1] + 1) {
      toast(t("cues.toast.loopContiguous", "Selected cues must be contiguous for loop group."), "error");
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
  toast(
    tfmt(
      "cues.toast.loopCreated",
      "Loop group created ({count} cues, {loopCount}x)",
      { count, loopCount }
    ),
    "success"
  );
}

function removeLoopGroup() {
  const count = selectedCueIndices.size;
  if (count === 0) {
    toast(t("cues.toast.loopRemoveSelect", "Select cues to remove from loop group."), "error");
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
    toast(
      tfmt("cues.toast.loopRemoved", "Removed {removed} cue(s) from loop group", { removed }),
      "info"
    );
  } else {
    toast(t("cues.toast.loopNone", "No loop groups found in selection."), "warning");
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
  toast(t("cues.toast.newList", "New cue list created"), "success");
}

///////////////////////
// PLAYBACK CONTROL
///////////////////////

async function runCuesFromUI() {
  const seq = cuesObj.sequence || [];
  if (!seq.length) return toast(t("cues.toast.noCuesPlay", "No cues to play."), "error");
  if (playbackActive) return toast(t("cues.toast.playbackActive", "Playback already active."), "warning");
  
  playbackActive = true;
  uiFollowStopFlag = false;
  const runId = ++uiFollowRunId;

  // Démarrer la boucle d'animation des effets (si pas déjà démarrée)
  startEffectRenderLoop();
  
  // Pilotage 100% côté UI
  uiFollowSequence(seq, runId, 0).catch(e => console.error("[UI-FOLLOW]", e));
  toast(t("cues.toast.playbackStarted", "Playback started"), "info");
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

  // Libérer le verrou DMX au cas où
  window.dmxLocked = false;

  hidePlaybackBar();
  updatePlaybackUI();

  await fetch("/api/stop_run", { method: "POST" });
  toast(t("cues.toast.playbackStopped", "Stopped"), "info");
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
  if (!seq.length) return toast(t("cues.toast.noCuesPlay", "No cues to play."), "error");
  if (selectedCueIndex == null) return toast(t("cues.toast.selectCueFirst", "Select a cue first."), "error");

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
  await sendToEngineWithEffects(1.0);

  playbackActive = true;
  const runId = ++uiFollowRunId;
  playbackCueIndex = startIdx;

  startEffectRenderLoop();
  showPlaybackBar();
  updatePlaybackUI();

  uiFollowSequence(seq.slice(startIdx), runId, startIdx).catch(e => console.error("[UI-FOLLOW]", e));
  toast(tfmt("cues.toast.playbackFrom", "Playback from cue {index}", { index: startIdx + 1 }), "info");
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
    pauseBtn.textContent = playbackPaused
      ? `▶ ${t("cues.playback.resume", "Resume")}`
      : `⏸ ${t("cues.playback.pause", "Pause")}`;
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
  btn.disabled = !hasSelection || playbackActive;
}

function updatePlaybackUI() {
  const seq = cuesObj.sequence || [];
  const cueName = $id("playback-cue-name");
  const phaseEl = $id("playback-phase");
  const countdownEl = $id("playback-countdown");
  const adjustEl = $id("wait-adjust-display");

  if (cueName) {
    const step = seq[playbackCueIndex];
    cueName.textContent = step
      ? tfmt(
        "cues.playback.cueLabel",
        "Cue {index}: {name}",
        { index: playbackCueIndex + 1, name: step.name || "" }
      )
      : "--";
  }

  if (phaseEl) {
    if (playbackPaused) {
      phaseEl.textContent = `⏸ ${t("cues.playback.phasePaused", "PAUSED")}`;
      phaseEl.style.color = "#fbbf24";
    } else if (playbackPhase === "waiting") {
      phaseEl.textContent = `⏳ ${t("cues.playback.phaseWaiting", "Waiting")}`;
      phaseEl.style.color = "#60a5fa";
    } else if (playbackPhase === "fading") {
      phaseEl.textContent = `🎬 ${t("cues.playback.phaseFading", "Fading")}`;
      phaseEl.style.color = "#22c55e";
    } else {
      phaseEl.textContent = "--";
      phaseEl.style.color = "#94a3b8";
    }
  }

  if (countdownEl) {
    if (playbackPhase === "waiting" && playbackWaitRemaining > 0) {
      countdownEl.textContent = `${Math.ceil(playbackWaitRemaining)}ms`;
    } else {
      countdownEl.textContent = "--";
    }
  }

  if (adjustEl) {
    const sign = liveWaitAdjust >= 0 ? "+" : "";
    adjustEl.textContent = `${sign}${liveWaitAdjust}ms`;
  }

  updatePlaybackButtons();
  updatePlayingHighlight();
}

function updatePlayingHighlight() {
  const tbody = $id("cue-table-body");
  if (!tbody) return;

  const rows = Array.from(tbody.children);
  rows.forEach(tr => tr.classList.remove("playing"));

  if (playbackCueIndex == null || playbackCueIndex < 0) return;
  const row = rows.find(tr => parseInt(tr.dataset.index, 10) === playbackCueIndex);
  if (row) row.classList.add("playing");
}

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
  toast(
    tfmt("cues.toast.played", "Played {name}", { name: step.name || t("cues.toast.playedFallback", "cue") }),
    "info"
  );
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
 *  "500"         -> baseFadeMs=500, totalMs=500, offsets=0
 *  "500 > 5000"  -> baseFadeMs=500, totalMs=5500, offsets selon ordre
 *  "500 < 5000"  -> idem mais ordre inversé
 *  "500 | 5000"  -> extrémités -> centre
 *  "500 || 5000" -> centre -> extrémités
 *  "500 ? 5000"  -> ordre aléatoire
 */
function computeFadePattern(fadeField, deviceIds) {
  const ids = deviceIds || [];
  const str = String(fadeField ?? "0").trim();

  let baseFadeMs = 0;
  let spreadMs = 0;
  let op = null;

  const m = str.match(/^(\d+)\s*([><|?]{1,2})\s*(\d+)$/);
  if (m) {
    baseFadeMs = parseInt(m[1], 10) || 0;
    op = m[2];
    spreadMs = parseInt(m[3], 10) || 0;
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
  } else if (op === "|") {
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
  } else if (op === "||") {
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

async function uiFollowSequence(seq, runId, indexOffset = 0) {
  uiFollowStopFlag = false;
  skipToNextCue = false;
  if (!Array.isArray(seq)) return;

  let i = 0;
  while (i < seq.length) {
    if (uiFollowStopFlag || runId !== uiFollowRunId) break;

    const step = seq[i];
    playbackCueIndex = i + indexOffset;
    updatePlaybackUI();

    // Check if this cue starts a loop group
    if (step.loopGroup) {
      // Find all cues in this loop group
      const groupId = step.loopGroup;
      const loopCount = step.loopCount || 1;
      const groupStart = i;
      let groupEnd = i;

      // Find the end of the loop group
      while (groupEnd + 1 < seq.length && seq[groupEnd + 1].loopGroup === groupId) {
        groupEnd++;
      }

      // Execute the loop group loopCount times
      for (let loopIter = 0; loopIter < loopCount; loopIter++) {
        for (let j = groupStart; j <= groupEnd; j++) {
          if (uiFollowStopFlag || runId !== uiFollowRunId) break;

          playbackCueIndex = j + indexOffset;
          updatePlaybackUI();
          await uiFollowStep(seq[j], runId);
        }
        if (uiFollowStopFlag || runId !== uiFollowRunId) break;
      }

      // Skip to after the loop group
      i = groupEnd + 1;
    } else {
      await uiFollowStep(step, runId);
      i++;
    }
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

async function uiFollowStep(step, runId) {
  if (uiFollowStopFlag || runId !== uiFollowRunId) return;

  // 1. SLEEP (SANS verrou) avec support pause/skip/adjust
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

      // Handle pause
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
    const fadeStart = performance.now();
    
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
        break;
      }

      // Handle pause during fade
      while (playbackPaused && !uiFollowStopFlag && runId === uiFollowRunId) {
        updatePlaybackUI();
        await new Promise(r => setTimeout(r, 50));
      }

      const elapsed = performance.now() - fadeStart;

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
      
      if (elapsed >= fadeTotalMs) break;
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
    const funcs = fi.functions || {};
    const absMap = getDeviceAttrAbsChannels(dev);
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

      const attr = group.attrKey;
      const absCh = absMap[attr];
      if (absCh == null) continue;

      let baseAmp = parseFloat(group.amplitude ?? 0) || 0;
      let effScale = scale;

      if (gmForDev && gmForDev[gId] != null) {
        effScale *= gmForDev[gId];
      }

      const scaledGroup = {
        ...group,
        amplitude: baseAmp * effScale,
        frequency: parseFloat(group.frequency ?? 0) || 0,
      };

      const baseVal = perUniverseMap[u][absCh] ?? 0;
      const delta = evalGroupEffect(scaledGroup, tMs, dev.id);
      const val = clamp(Math.round(baseVal + delta), 0, 255);
      perUniverseMap[u][absCh] = val;
    }

    // Previews
    if (funcs.rgb) {
      const rAbs = absMap.r, gAbs = absMap.g, bAbs = absMap.b;
      devicePreviewRGB[dev.id] = {
        r: rAbs != null ? (perUniverseMap[u][rAbs] ?? 0) : 0,
        g: gAbs != null ? (perUniverseMap[u][gAbs] ?? 0) : 0,
        b: bAbs != null ? (perUniverseMap[u][bAbs] ?? 0) : 0,
      };
    }

    if (funcs.dimmer) {
      const dAbs = absMap.dimmer;
      devicePreviewDimmer[dev.id] = dAbs != null ? (perUniverseMap[u][dAbs] ?? 0) : 0;
    }
  }
  // NOTE: Identify mode is now handled by Python engine (see /api/identify/*)
  // Pendant un playback de cues, l'UI est la source unique de DMX
  // On utilise bypassLock=true car cette fonction EST la source autorisée pendant lock
  for (const [uStr, chMap] of Object.entries(perUniverseMap)) {
    const u = parseInt(uStr, 10) || 0;
    await applyUniverseState(u, chMap, true); // bypassLock=true
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
    toast(t("cues.toast.selectCueFirst", "Select a cue first."), "error");
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
  const newIndices = new Set();
  for (const idx of sortedIndices) {
    newIndices.add(idx + delta);
  }
  selectedCueIndices = newIndices;
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


document.addEventListener("DOMContentLoaded", () => {
  const cueSelect = $id("cue-file-select");
  if (cueSelect && cueSelect.options.length === 0) {
    refreshCueFileList();
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

  // Drag & drop avec SortableJS
  const tbody = $id("cue-table-body");
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
    waitPlusBtn.addEventListener("click", () => {
      liveWaitAdjust += 100;
      updatePlaybackUI();
    });
  }

  if (waitMinusBtn) {
    waitMinusBtn.addEventListener("click", () => {
      liveWaitAdjust -= 100;
      updatePlaybackUI();
    });
  }

  // Pause/Resume button
  const pauseBtn = $id("pause-cues");
  if (pauseBtn) {
    pauseBtn.addEventListener("click", () => {
      if (!playbackActive) return;
      playbackPaused = !playbackPaused;
      updatePlaybackUI();
      toast(
        playbackPaused
          ? t("cues.toast.playbackPaused", "Playback paused")
          : t("cues.toast.playbackResumed", "Playback resumed"),
        "info"
      );
    });
  }

  // Skip to next cue button
  const skipCueBtn = $id("skip-cue");
  if (skipCueBtn) {
    skipCueBtn.addEventListener("click", () => {
      if (!playbackActive) return;
      skipToNextCue = true;
      toast(t("cues.toast.playbackSkip", "Skipping to next cue..."), "info");
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

  updatePlayFromButtonState();
});

document.addEventListener("DOMContentLoaded", () => {
  // Stop Effects button - clears all live effects from devices
  const stopFxBtn = $id("stop-effects");
  if (stopFxBtn) {
    stopFxBtn.addEventListener("click", () => {
      // Clear all device effect groups
      for (const devId of Object.keys(rigDevices)) {
        deviceCurrentGroups[devId] = new Set();
      }
      // Re-apply current values without effects
      if (typeof renderActualEffectsPanel === "function") {
        renderActualEffectsPanel();
      }
      sendToEngineWithEffects(1.0);
      drawRig();
      toast(t("cues.toast.effectsStopped", "Effects stopped"), "info");
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
        toast(t("cues.toast.identOn", "Identification mode ON"), "info");

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

          const baseAddr = dev.address || 0;
          const universe = dev.universe || 0;

          // Find dimmer channel
          let dimmerCh = null;
          if (fix.functions?.dimmer?.channel != null) {
            dimmerCh = baseAddr + fix.functions.dimmer.channel;
          }

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
          toast(t("cues.toast.identStartFailed", "Identify start failed"), "error");
        }

      } else {
        // OFF: Stop identify mode
        identBtn.textContent = (typeof t === "function") ? t("header.identOff", "Identify: OFF") : "Identify: OFF";
        identBtn.classList.remove("active");
        toast(t("cues.toast.identOff", "Identification mode OFF"), "info");

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
