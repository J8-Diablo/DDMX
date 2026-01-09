// static/core.js
// État global avec SYSTÈME DE VERROU DMX

///////////////////////
// ÉTAT GLOBAL
///////////////////////

// Fixtures et rig
let fixtures = {};
let rigDevices = {};
let nextDeviceId = 1;

// Cues
let cuesObj = {
  loop: false,
  loop_count: null,
  devices_def: {},
  virtual_groups: {},
  sequence: []
};
let currentCueFilename = null;
let selectedCueIndex = null;           // Primary selection (for single operations)
let selectedCueIndices = new Set();    // Multi-selection (for batch operations)

// Valeurs DMX locales par device
let deviceLocalValues = {};

// Groupes virtuels d'effets
let virtualGroups = {};
let nextVirtualGroupId = 1;

// Groupes actifs sur les devices
let deviceCurrentGroups = {};

// Sélection de devices dans le rig
let selectedDeviceOrder = [];
let selectedDeviceSet = new Set();

// Rig glow overlay (halo) for spotlighting devices
let rigGlows = {};      // { deviceId: { start: number, until: number } }
let rigGlowAnim = null; // requestAnimationFrame handle

// Canvas du rig
let rigCanvas = null;
let rigCtx = null;

// Vue du rig (pan/zoom + grid)
const RIG_GRID_SIZE = 40;
const RIG_MIN_SCALE = 0.03;
const RIG_MAX_SCALE = 30.0;
let rigView = { offsetX: 0, offsetY: 0, scale: 1 };

// Widgets controller
let rgbWidgetRef = null;
let posWidgetRef = null;

// Préview calculée par les effets
let devicePreviewRGB = {};
let devicePreviewDimmer = {};

// ========================================
// SYSTÈME DE VERROU DMX
// ========================================
window.playbackActive = false;
window.uiFollowStopFlag = false;
window.uiFollowRunId = 0;
window.effectStartEpoch = performance.now();

// Quand dmxLocked = true, SEULE la cue peut envoyer des données
// L'UI est BLOQUÉE
window.dmxLocked = false;

function canUISendDMX() {
  return !window.dmxLocked;
}

///////////////////////
// HELPERS DOM / MATH
///////////////////////

const $id = (id) => document.getElementById(id);

function bindClick(ids, fn, label = "") {
  const list = Array.isArray(ids) ? ids : [ids];
  let bound = 0;
  for (const id of list) {
    const el = $id(id);
    if (el) {
      el.onclick = fn;
      bound++;
    }
  }
  if (!bound) console.warn(`[UI] click target not found for ${label || list.join(",")}`);
  return bound > 0;
}

function clamp(v, min, max) {
  return v < min ? min : v > max ? max : v;
}

function elementCoords(e, el) {
  const r = el.getBoundingClientRect();
  const scaleX = el.clientWidth / r.width;
  const scaleY = el.clientHeight / r.height;
  return {
    x: (e.clientX - r.left) * scaleX,
    y: (e.clientY - r.top) * scaleY
  };
}

function canvasCoords(e, canvas) {
  const r = canvas.getBoundingClientRect();
  const scaleX = canvas.width / r.width;
  const scaleY = canvas.height / r.height;
  return {
    x: (e.clientX - r.left) * scaleX,
    y: (e.clientY - r.top) * scaleY
  };
}

function screenToWorld(px, py) {
  return {
    x: (px - rigView.offsetX) / rigView.scale,
    y: (py - rigView.offsetY) / rigView.scale
  };
}

function worldToScreen(wx, wy) {
  return {
    x: wx * rigView.scale + rigView.offsetX,
    y: wy * rigView.scale + rigView.offsetY
  };
}

function eventToWorld(e) {
  const { x: px, y: py } = canvasCoords(e, rigCanvas);
  const { x, y } = screenToWorld(px, py);
  return { px, py, wx: x, wy: y };
}

function snapToGrid(v) {
  return Math.round(v / RIG_GRID_SIZE) * RIG_GRID_SIZE;
}

///////////////////////
// LAYOUT : split horizontal Rig/Cues seulement
///////////////////////

let rigCuesSplit = 0.6; // ~ 1.4fr / 1fr

function updateRigCanvasSize() {
  if (!rigCanvas) return;
  const rect = rigCanvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;

  const newW = Math.max(200, Math.round(rect.width));
  const newH = Math.max(200, Math.round(rect.height));

  if (rigCanvas.width !== newW || rigCanvas.height !== newH) {
    rigCanvas.width = newW;
    rigCanvas.height = newH;
  }
}

function applyLayoutSplit() {
  const rigPanel = document.querySelector(".rig-panel");
  const cuesPanel = document.querySelector(".cues-panel");
  if (rigPanel && cuesPanel) {
    rigPanel.style.flexGrow = rigCuesSplit;
    cuesPanel.style.flexGrow = 1 - rigCuesSplit;
    rigPanel.style.flexBasis = "0";
    cuesPanel.style.flexBasis = "0";
  }
  updateRigCanvasSize();
  if (typeof drawRig === "function") drawRig();
}

function initSplitLayout() {
  applyLayoutSplit();

  const vSplit = document.getElementById("split-rig-cues");
  if (vSplit) {
    vSplit.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const main = document.querySelector(".main-grid");
      if (!main) return;
      const rect = main.getBoundingClientRect();

      function onMove(ev) {
        const x = ev.clientX - rect.left;
        let frac = x / rect.width;
        frac = clamp(frac, 0.2, 0.8); // éviter 0% / 100%
        rigCuesSplit = frac;
        applyLayoutSplit();
      }

      function onUp() {
        window.removeEventListener("mousemove", onMove);
        window.removeEventListener("mouseup", onUp);
      }

      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    });
  }

  window.addEventListener("resize", () => {
    applyLayoutSplit();
  });
}

///////////////////////
// UI HELPERS
///////////////////////

const t = (key, fallback) =>
  (typeof window.t === "function" ? window.t(key, fallback) : (fallback || key));
const tfmt = (key, fallback, params) => {
  if (typeof window.tfmt === "function") return window.tfmt(key, fallback, params);
  const template = t(key, fallback);
  if (!params) return template;
  return template.replace(/\{(\w+)\}/g, (_, k) => (params[k] == null ? "" : String(params[k])));
};

const toast = (m, t = "success") => {
  if (!m) return;
  if (window.ui && typeof window.ui.toast === "function") window.ui.toast(m, t);
  else console.log(`[${t}] ${m}`);
};

const confirmModal = async (title, text) => {
  if (window.ui && typeof window.ui.confirmModal === "function") {
    return await window.ui.confirmModal(title, text);
  }
  toast(
    tfmt("ui.confirmUnavailable", "(confirm unavailable) {title}: {text}", { title, text }),
    "warning"
  );
  return true;
};

const promptModal = async (title, val = "", ph = "") => {
  if (window.ui && typeof window.ui.promptModal === "function") {
    return await window.ui.promptModal(title, val, ph);
  }
  toast(
    tfmt("ui.inputUnavailable", "(input unavailable) {title} -> action canceled", { title }),
    "warning"
  );
  return null;
};

const deviceEditModal = async (dev) => {
  if (window.ui && typeof window.ui.deviceEditModal === "function") {
    return await window.ui.deviceEditModal(dev);
  }
  toast(t("ui.deviceEditUnavailable", "(device edit unavailable) SweetAlert2 not loaded."), "error");
  return null;
};

///////////////////////
// Couleur HSV/RGB
///////////////////////

function hsvToRgb(h, s, v) {
  h = ((h % 360) + 360) % 360;
  s = clamp(s, 0, 1);
  v = clamp(v, 0, 1);

  const c = v * s;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = v - c;

  let r1 = 0, g1 = 0, b1 = 0;
  if (h < 60) { r1 = c; g1 = x; b1 = 0; }
  else if (h < 120) { r1 = x; g1 = c; b1 = 0; }
  else if (h < 180) { r1 = 0; g1 = c; b1 = x; }
  else if (h < 240) { r1 = 0; g1 = x; b1 = c; }
  else if (h < 300) { r1 = x; g1 = 0; b1 = c; }
  else { r1 = c; g1 = 0; b1 = x; }

  return {
    r: Math.round((r1 + m) * 255),
    g: Math.round((g1 + m) * 255),
    b: Math.round((b1 + m) * 255),
  };
}

function rgbToHsv(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const d = max - min;
  let h = 0;
  if (d !== 0) {
    if (max === r) h = 60 * (((g - b) / d) % 6);
    else if (max === g) h = 60 * (((b - r) / d) + 2);
    else h = 60 * (((r - g) / d) + 4);
  }
  if (h < 0) h += 360;
  const s = max === 0 ? 0 : d / max;
  const v = max;
  return { h, s, v };
}

///////////////////////
// ATTRIBUTS FIXTURE -> ABS CHANNELS
///////////////////////

function getDeviceAttrAbsChannels(dev) {
  const fi = fixtures[dev.fixture] || {};
  const funcs = fi.functions || {};
  const base = dev.address || 0;
  const out = {};

  if (funcs.dimmer && funcs.dimmer.channel != null)
    out.dimmer = base + parseInt(funcs.dimmer.channel, 10);

  if (funcs.rgb) {
    if (funcs.rgb.red != null) out.r = base + parseInt(funcs.rgb.red, 10);
    if (funcs.rgb.green != null) out.g = base + parseInt(funcs.rgb.green, 10);
    if (funcs.rgb.blue != null) out.b = base + parseInt(funcs.rgb.blue, 10);
  }

  if (funcs.position) {
    if (funcs.position.pan && funcs.position.pan.channel != null)
      out.pan = base + parseInt(funcs.position.pan.channel, 10);
    if (funcs.position.tilt && funcs.position.tilt.channel != null)
      out.tilt = base + parseInt(funcs.position.tilt.channel, 10);
  }
  return out;
}

///////////////////////
// API DMX (buffer + pump réseau)
///////////////////////

// Cache des derniers frames envoyés par univers pour éviter les envois identiques
const lastDmxFrames = {}; // { [universe]: { ch: val, ... } }

// Buffer côté front : 1 pipeline par univers
const dmxUniverseBuffers = {}; 
// structure : {
//   [universe]: {
//     pending: { ch: val, ... },   // dernières valeurs à envoyer
//     inFlight: false              // requête HTTP en cours
//   }
// };

// ========================================
// NEW ARCHITECTURE: Python handles all DMX
// JS only sends high-level commands
// ========================================

// Send channel values to Python engine (for live controller edits)
async function applyUniverseState(universe, channels, bypassLock = false) {
  // UI lock check (for visual feedback only now)
  if (window.dmxLocked && !bypassLock) return;

  if (!channels || typeof channels !== "object") return;
  const keys = Object.keys(channels);
  if (!keys.length) return;

  const u = Number.isFinite(universe) ? universe : 0;

  // Buffer locally for UI preview
  let state = dmxUniverseBuffers[u];
  if (!state) {
    state = { pending: {}, inFlight: false };
    dmxUniverseBuffers[u] = state;
  }

  for (const [k, v] of Object.entries(channels)) {
    const ch = parseInt(k, 10);
    if (!Number.isFinite(ch) || ch < 0 || ch >= 512) continue;
    const val = Math.max(0, Math.min(255, v | 0));
    state.pending[ch] = val;
  }
}

// Send buffered values to Python engine
async function dmxNetworkPump() {
  for (const [uStr, state] of Object.entries(dmxUniverseBuffers)) {
    const u = parseInt(uStr, 10) || 0;

    if (state.inFlight) continue;

    const pendingKeys = Object.keys(state.pending);
    if (!pendingKeys.length) continue;

    const frame = {};
    for (const k of pendingKeys) {
      frame[k] = state.pending[k];
    }
    state.pending = {};

    // Check if changed
    const prev = lastDmxFrames[u];
    let changed = false;
    if (!prev) {
      changed = true;
    } else if (Object.keys(prev).length !== pendingKeys.length) {
      changed = true;
    } else {
      for (const k of pendingKeys) {
        if (prev[k] !== frame[k]) {
          changed = true;
          break;
        }
      }
    }
    if (!changed) continue;

    state.inFlight = true;
    try {
      // Use new API endpoint
      await fetch("/api/live/channels", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ universe: u, channels: frame, device_id: "ui_live" }),
      });
      lastDmxFrames[u] = frame;
    } catch (err) {
      console.warn("[DMX] pump send failed for universe", u, err);
    } finally {
      state.inFlight = false;
    }
  }
}

// DMX pump interval
setInterval(dmxNetworkPump, 30);

// ========================================
// SSE: Receive state from Python engine
// ========================================
let sseConnection = null;
let sseReconnectTimeout = null;

function connectSSE() {
  if (sseConnection) {
    sseConnection.close();
  }

  sseConnection = new EventSource("/api/state/stream");

  sseConnection.onmessage = (event) => {
    try {
      const state = JSON.parse(event.data);
      handleEngineState(state);
    } catch (e) {
      console.warn("[SSE] parse error:", e);
    }
  };

  sseConnection.onerror = () => {
    console.warn("[SSE] connection error, reconnecting...");
    sseConnection.close();
    if (sseReconnectTimeout) clearTimeout(sseReconnectTimeout);
    sseReconnectTimeout = setTimeout(connectSSE, 2000);
  };

  sseConnection.onopen = () => {
    console.log("[SSE] connected to engine state stream");
  };
}

function handleEngineState(state) {
  // Update local state from engine for visualization ONLY
  // DO NOT update dmxLocked - that's controlled by JS fade logic
  if (state.universes) {
    for (const [uStr, values] of Object.entries(state.universes)) {
      const u = parseInt(uStr, 10);
      if (!dmxUniverseBuffers[u]) {
        dmxUniverseBuffers[u] = { pending: {}, inFlight: false };
      }
      // Store for visualization only (don't feed back into pending!)
      lastDmxFrames[u] = {};
      for (let i = 0; i < values.length; i++) {
        if (values[i] !== 0) {
          lastDmxFrames[u][i] = values[i];
        }
      }
    }
  }

  // Update identify indicator from Python (Python controls identify)
  if (state.identify_active !== undefined) {
    window.identMode = state.identify_active;
  }

  // NOTE: DO NOT update window.dmxLocked from Python!
  // JS controls dmxLocked during cue transitions.
  // Python's fade_active is for server-side fades which we don't use.

  // Trigger rig redraw if needed (for visualization)
  if (typeof drawRig === "function") {
    drawRig();
  }
}

// Connect SSE on page load
document.addEventListener("DOMContentLoaded", () => {
  setTimeout(connectSSE, 500);
});

///////////////////////
// FIXTURES
///////////////////////

async function loadFixtures(retryCount = 0) {
  try {
    const r = await fetch("/api/fixtures", { cache: "no-store" });
    if (!r.ok) throw new Error(`fixtures: ${r.status}`);
    fixtures = await r.json();
  } catch (e) {
    fixtures = {};
    console.error(e);
  }

  const sel = $id("fixture-type-select");
  if (!sel) return;
  sel.innerHTML = "";
  for (const [name, fx] of Object.entries(fixtures)) {
    if (fx.error) continue;
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = `${fx.info?.model || name} (${name})`;
    sel.appendChild(opt);
  }

  if (!Object.keys(fixtures).length && retryCount < 5) {
    setTimeout(() => loadFixtures(retryCount + 1), 1000);
  }
}

///////////////////////
// TABS Controller
///////////////////////

function bindTabs() {
  const btns = document.querySelectorAll(".controller-tabs .tab-btn");
  if (!btns.length) return;
  btns.forEach(btn => {
    btn.onclick = async () => {
      btns.forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      const tab = btn.dataset.tab;

      $id("tab-rig")?.classList.toggle("active", tab === "rig");
      $id("tab-effects")?.classList.toggle("active", tab === "effects");

      if (tab === "effects") {
        await ensureEffectsLoaded();
        renderEffectsLibrary();
        renderEffectsTargets();
      }
    };
  });
}

///////////////////////
// BOUTONS TOP + CUES
///////////////////////

function bindButtons() {
  bindClick(["add-fixture-btn", "add-device-btn"], addDeviceFromUI, "add device");
  bindClick(["delete-device-btn", "remove-device-btn"], deleteSelectedDevices, "delete device");

  bindClick(["cue-load-file", "cue-load"], handleLoadCueClick, "cue load file");
  bindClick(["cue-save-file", "cue-save"], saveCurrentCueFile, "cue save");
  bindClick(["cue-save-as", "cue-saveas"], saveCueFileAs, "cue save as");

  bindClick(["cue-add", "cue-add-btn"], cueAddFromSelection, "cue add");
  bindClick(["cue-update", "cue-update-btn"], cueUpdateFromSelection, "cue update");
  bindClick(["cue-duplicate"], cueDuplicate, "cue duplicate");

  const cueDelBtn =
    $id("cue-delete") ||
    $id("cue-delete-btn") ||
    $id("cue-del");

  if (cueDelBtn) {
    cueDelBtn.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      cueDelete();
    };
  } else {
    console.warn("[UI] cue delete button not found");
  }

  bindClick(["cue-prop-apply", "cue-props-apply"], applyCueProps, "cue props apply");

  bindClick(["run-cues", "play-cues"], runCuesFromUI, "run cues");
  bindClick(["stop-cues", "stop-run"], stopRun, "stop cues");

  bindClick(["apply-selection", "apply-now"], () => applySelectionToEngine(false), "apply selection");

  bindClick(["new-json", "new-cuelist"], newJSON, "new json");
}

async function handleLoadCueClick() {
  const sel = $id("cue-file-select");
  if (!sel) return toast(t("cues.toast.selectMissing", "Cue file selector not found."), "error");

  if (!sel.value) {
    await refreshCueFileList();
  }

  const filename =
    sel.value ||
    currentCueFilename ||
    sel.options[0]?.value;

  if (!filename) {
    toast(t("cues.toast.noneAvailable", "No cue file available."), "error");
    return;
  }

  sel.value = filename;
  loadCueFile(filename);
}

///////////////////////
// BOOT
///////////////////////

window.addEventListener("load", () => {
  rigCanvas = $id("rig-canvas");
  if (!rigCanvas) {
    console.error("[BOOT] rig-canvas not found");
    return;
  }
  rigCtx = rigCanvas.getContext("2d");

  // Layout : split horizontal Rig/Cues
  try {
    initSplitLayout();
  } catch (e) {
    console.error("[BOOT] initSplitLayout failed", e);
  }

  // Events rig (pan/zoom/select dans rig.js)
  if (typeof bindRigCanvasEvents === "function") {
    try {
      bindRigCanvasEvents();
    } catch (e) {
      console.error("[BOOT] bindRigCanvasEvents failed", e);
    }
  } else {
    console.warn("[BOOT] bindRigCanvasEvents missing");
  }
  if (typeof onRigWheel === "function") {
    rigCanvas.addEventListener("wheel", onRigWheel, { passive: false });
  } else {
    console.warn("[BOOT] onRigWheel missing");
  }

  try {
    bindButtons();
  } catch (e) {
    console.error("[BOOT] bindButtons failed", e);
  }
  try {
    bindTabs();
  } catch (e) {
    console.error("[BOOT] bindTabs failed", e);
  }

  loadFixtures().then(() => {
    if (typeof refreshCueFileList === "function") {
      refreshCueFileList();
    } else {
      console.warn("[BOOT] refreshCueFileList missing");
    }
    try {
      applyLayoutSplit(); // ensure canvas size + first draw
    } catch (e) {
      console.error("[BOOT] applyLayoutSplit failed", e);
    }
  });

  if ($id("tab-effects")?.classList.contains("active") &&
      typeof ensureEffectsLoaded === "function") {
    ensureEffectsLoaded().then(() => {
      renderEffectsLibrary();
      renderEffectsTargets();
    });
  }

  if (typeof startEffectRunner === "function") {
    startEffectRunner();
  } else {
    console.warn("[BOOT] startEffectRunner missing");
  }
});
