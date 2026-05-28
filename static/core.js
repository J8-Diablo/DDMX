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
let rgbWidgetRefs = [];
let posWidgetRefs = [];

// Préview calculée par les effets
let devicePreviewRGB = {};
let devicePreviewDimmer = {};

const FIXTURE_FAMILY_ORDER = {
  dimmer: 0,
  color: 1,
  position: 2,
  other: 3,
};

const FIXTURE_SHARED_TARGET_SPECS = [
  { key: "family.dimmer.level", label: "All Dimmers", family: "dimmer", role: "level", aliases: ["dimmer"] },
  { key: "family.color.red", label: "All Reds", family: "color", role: "red", aliases: ["r"] },
  { key: "family.color.green", label: "All Greens", family: "color", role: "green", aliases: ["g"] },
  { key: "family.color.blue", label: "All Blues", family: "color", role: "blue", aliases: ["b"] },
  { key: "family.position.pan", label: "All Pans", family: "position", role: "pan", aliases: ["pan"] },
  { key: "family.position.tilt", label: "All Tilts", family: "position", role: "tilt", aliases: ["tilt"] },
];

const FIXTURE_SHARED_TARGET_BY_KEY = Object.fromEntries(
  FIXTURE_SHARED_TARGET_SPECS.map(spec => [spec.key, spec])
);

const FIXTURE_SHARED_ROLE_TO_SPEC = Object.fromEntries(
  FIXTURE_SHARED_TARGET_SPECS.map(spec => [`${spec.family}:${spec.role}`, spec])
);

const fixtureElementDefsCache = new WeakMap();

function humanizeFixtureToken(value) {
  return String(value || "")
    .replace(/[._-]+/g, " ")
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .map(part => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function getFixtureFootprint(fi) {
  return Math.max(1, parseInt(fi?.footprint ?? fi?.addr_count ?? 1, 10) || 1);
}

function getFixtureGroups(fi, family = null) {
  const groups = Array.isArray(fi?.groups) ? fi.groups : [];
  if (!family) return groups;
  const wanted = String(family || "").trim().toLowerCase();
  return groups.filter(group => String(group?.family || "").trim().toLowerCase() === wanted);
}

function getFixtureGroupLabel(group) {
  return String(group?.label || "").trim() || humanizeFixtureToken(group?.id || group?.kind || group?.family || "Group");
}

function getFixturePrimaryGroupId(fi, family) {
  const primary = fi?.primary;
  const wanted = String(family || "").trim().toLowerCase();
  if (primary && typeof primary === "object" && primary[wanted]) {
    return String(primary[wanted]);
  }
  const match = getFixtureGroups(fi, wanted)[0];
  return match ? String(match.id || "") : "";
}

function getFixturePrimaryGroup(fi, family) {
  const wantedId = getFixturePrimaryGroupId(fi, family).toLowerCase();
  return getFixtureGroups(fi, family).find(group => String(group?.id || "").toLowerCase() === wantedId) || null;
}

function getGroupChannels(group) {
  return Array.isArray(group?.channels) ? group.channels : [];
}

function getGroupChannel(group, role) {
  const wanted = String(role || "").trim().toLowerCase();
  return getGroupChannels(group).find(channel => String(channel?.role || "").trim().toLowerCase() === wanted) || null;
}

function getGroupSelectionScope(group) {
  return String(group?.selectionScope || "devices").trim().toLowerCase() === "fixture_elements"
    ? "fixture_elements"
    : "devices";
}

function getGroupTargetKey(group) {
  return String(group?.targetKey || group?.attrKey || group?.attr || "").trim();
}

function getSharedFixtureTargetSpec(key) {
  return FIXTURE_SHARED_TARGET_BY_KEY[String(key || "").trim().toLowerCase()] || null;
}

function getFixtureElementDefs(fi) {
  if (!fi || typeof fi !== "object") return [];
  if (fixtureElementDefsCache.has(fi)) {
    return fixtureElementDefsCache.get(fi) || [];
  }

  const counts = {};
  const elements = [];

  getFixtureGroups(fi).forEach(group => {
    const family = String(group?.family || "").trim().toLowerCase();
    if (!family) return;

    const mapped = [];
    getGroupChannels(group).forEach(channel => {
      const role = String(channel?.role || "").trim().toLowerCase();
      const spec = FIXTURE_SHARED_ROLE_TO_SPEC[`${family}:${role}`];
      if (!spec) return;
      mapped.push({
        spec,
        offset: parseInt(channel?.offset ?? 0, 10) || 0,
        attrKey: `${String(group?.id || "").trim().toLowerCase()}.${role}`,
      });
    });
    if (!mapped.length) return;

    counts[family] = (counts[family] || 0) + 1;
    const idx = counts[family] - 1;
    const element = elements[idx] || { index: idx, targets: {} };

    mapped.forEach(entry => {
      element.targets[entry.spec.key] = {
        offset: entry.offset,
        attrKey: entry.attrKey,
      };
      entry.spec.aliases.forEach(alias => {
        element.targets[alias] = {
          offset: entry.offset,
          attrKey: entry.attrKey,
        };
      });
    });

    elements[idx] = element;
  });

  fixtureElementDefsCache.set(fi, elements);
  return elements;
}

function resolveFixtureElementsForDevice(dev) {
  const deviceId = String(dev?.id || "");
  if (!deviceId) return [];

  const fi = fixtures?.[dev?.fixture] || {};
  const defs = getFixtureElementDefs(fi);
  const base = parseInt(dev?.address ?? 0, 10) || 0;

  if (!defs.length) {
    return [{
      memberId: `${deviceId}::1`,
      deviceId,
      elementIndex: 0,
      targets: buildDeviceAttrAbsChannels(dev),
    }];
  }

  return defs.map((def, idx) => {
    const targets = {};
    Object.entries(def.targets || {}).forEach(([key, meta]) => {
      targets[key] = base + (parseInt(meta?.offset ?? 0, 10) || 0);
    });
    return {
      memberId: `${deviceId}::${idx + 1}`,
      deviceId,
      elementIndex: idx,
      targets,
    };
  });
}

function resolveEffectMembers(group) {
  const scope = getGroupSelectionScope(group);
  const deviceIds = Array.isArray(group?.deviceIds) ? group.deviceIds.map(String) : [];
  const order = [];
  const byDevice = {};

  for (const deviceId of deviceIds) {
    const dev = rigDevices?.[deviceId];
    if (!dev) continue;
    const members = scope === "fixture_elements"
      ? resolveFixtureElementsForDevice(dev)
      : [{
          memberId: String(deviceId),
          deviceId: String(deviceId),
          elementIndex: 0,
          targets: buildDeviceAttrAbsChannels(dev),
        }];

    byDevice[deviceId] = [];
    members.forEach(member => {
      const resolved = {
        ...member,
        index: order.length,
      };
      order.push(resolved);
      byDevice[deviceId].push(resolved);
    });
  }

  return {
    scope,
    order,
    count: Math.max(1, order.length),
    byDevice,
    effectMemberIds: order.map(member => String(member.memberId)),
  };
}

function getFixtureAttrDefinitions(fi, options = {}) {
  const includeLegacy = options.includeLegacy !== false;
  const defs = {};
  const groups = getFixtureGroups(fi);
  const primary = fi?.primary && typeof fi.primary === "object" ? fi.primary : {};

  groups.forEach(group => {
    const family = String(group?.family || "").trim().toLowerCase();
    const groupId = String(group?.id || "").trim().toLowerCase();
    const groupLabel = getFixtureGroupLabel(group);
    const isPrimaryGroup = String(primary[family] || "").trim().toLowerCase() === groupId;
    getGroupChannels(group).forEach(channel => {
      const role = String(channel?.role || "").trim().toLowerCase();
      const key = `${groupId}.${role}`;
      defs[key] = {
        key,
        label: `${groupLabel} - ${humanizeFixtureToken(role)}`,
        family,
        kind: String(group?.kind || "").trim().toLowerCase(),
        groupId,
        groupLabel,
        role,
        offset: parseInt(channel?.offset ?? 0, 10) || 0,
        presets: Array.isArray(channel?.presets) ? channel.presets : [],
        ui: String(channel?.ui || "").trim().toLowerCase(),
        isPrimary: isPrimaryGroup,
        legacy: false,
      };
    });
  });

  if (!includeLegacy) return defs;

  const primaryDimmer = getFixturePrimaryGroup(fi, "dimmer");
  const primaryColor = getFixturePrimaryGroup(fi, "color");
  const primaryPosition = getFixturePrimaryGroup(fi, "position");
  const dimmerLevel = getGroupChannel(primaryDimmer, "level") || getGroupChannels(primaryDimmer)[0];
  const colorRoles = { r: "red", g: "green", b: "blue" };

  if (primaryDimmer && dimmerLevel) {
    defs.dimmer = {
      key: "dimmer",
      label: "Primary Dimmer",
      family: "dimmer",
      kind: "",
      groupId: String(primaryDimmer.id || "").toLowerCase(),
      groupLabel: getFixtureGroupLabel(primaryDimmer),
      role: String(dimmerLevel.role || "level").toLowerCase(),
      offset: parseInt(dimmerLevel.offset ?? 0, 10) || 0,
      presets: Array.isArray(dimmerLevel.presets) ? dimmerLevel.presets : [],
      ui: String(dimmerLevel.ui || "").trim().toLowerCase(),
      isPrimary: true,
      legacy: true,
    };
  }

  Object.entries(colorRoles).forEach(([alias, role]) => {
    const channel = getGroupChannel(primaryColor, role);
    if (!primaryColor || !channel) return;
    defs[alias] = {
      key: alias,
      label: `Primary Color ${alias.toUpperCase()}`,
      family: "color",
      kind: "",
      groupId: String(primaryColor.id || "").toLowerCase(),
      groupLabel: getFixtureGroupLabel(primaryColor),
      role,
      offset: parseInt(channel.offset ?? 0, 10) || 0,
      presets: Array.isArray(channel.presets) ? channel.presets : [],
      ui: String(channel.ui || "").trim().toLowerCase(),
      isPrimary: true,
      legacy: true,
    };
  });

  ["pan", "tilt"].forEach(role => {
    const channel = getGroupChannel(primaryPosition, role);
    if (!primaryPosition || !channel) return;
    defs[role] = {
      key: role,
      label: `Primary ${humanizeFixtureToken(role)}`,
      family: "position",
      kind: "",
      groupId: String(primaryPosition.id || "").toLowerCase(),
      groupLabel: getFixtureGroupLabel(primaryPosition),
      role,
      offset: parseInt(channel.offset ?? 0, 10) || 0,
      presets: Array.isArray(channel.presets) ? channel.presets : [],
      ui: String(channel.ui || "").trim().toLowerCase(),
      isPrimary: true,
      legacy: true,
    };
  });

  return defs;
}

function buildDeviceAttrAbsChannels(dev, options = {}) {
  const fi = fixtures[dev?.fixture] || {};
  const defs = getFixtureAttrDefinitions(fi, options);
  const base = parseInt(dev?.address ?? 0, 10) || 0;
  const out = {};
  for (const [key, def] of Object.entries(defs)) {
    out[key] = base + (parseInt(def?.offset ?? 0, 10) || 0);
  }
  return out;
}

function getDeviceChannelInfo(dev, absoluteChannel) {
  const fi = fixtures[dev?.fixture] || {};
  const defs = Object.values(getFixtureAttrDefinitions(fi, { includeLegacy: false }));
  const base = parseInt(dev?.address ?? 0, 10) || 0;
  const target = parseInt(absoluteChannel, 10);
  for (const def of defs) {
    const absCh = base + (parseInt(def?.offset ?? 0, 10) || 0);
    if (absCh === target) return def;
  }
  return null;
}

const _devicePreviewChannelsCache = new Map();
function getDevicePrimaryPreviewChannels(dev) {
  if (!dev) return { dimmer: null, r: null, g: null, b: null };
  const id = dev.id;
  const fixtureKey = dev.fixture;
  const addr = dev.address;
  const cached = _devicePreviewChannelsCache.get(id);
  if (cached && cached.fixture === fixtureKey && cached.address === addr) {
    return cached.result;
  }
  const absMap = buildDeviceAttrAbsChannels(dev, { includeLegacy: true });
  const result = {
    dimmer: Number.isFinite(absMap.dimmer) ? absMap.dimmer : null,
    r: Number.isFinite(absMap.r) ? absMap.r : null,
    g: Number.isFinite(absMap.g) ? absMap.g : null,
    b: Number.isFinite(absMap.b) ? absMap.b : null,
  };
  _devicePreviewChannelsCache.set(id, { fixture: fixtureKey, address: addr, result });
  return result;
}
function invalidateDevicePreviewCache(deviceId) {
  if (deviceId == null) _devicePreviewChannelsCache.clear();
  else _devicePreviewChannelsCache.delete(deviceId);
}
window.invalidateDevicePreviewCache = invalidateDevicePreviewCache;

function listSelectionEffectAttrs() {
  const merged = {};
  const orderFor = (def) => {
    const family = String(def?.family || "other").toLowerCase();
    const familyOrder = FIXTURE_FAMILY_ORDER[family] ?? 99;
    const bucket = def?.shared ? "1" : (def?.legacy ? "2" : "0");
    return `${bucket}-${familyOrder}-${String(def?.groupLabel || "")}-${String(def?.label || "")}`;
  };

  if (!selectedDeviceOrder.length) return [];

  const availableShared = new Set();

  for (const deviceId of selectedDeviceOrder) {
    const dev = rigDevices?.[deviceId];
    if (!dev) continue;
    const fi = fixtures?.[dev.fixture];
    if (!fi) continue;
    const defs = getFixtureAttrDefinitions(fi, { includeLegacy: true });
    for (const [key, def] of Object.entries(defs)) {
      if (!merged[key]) merged[key] = def;
    }

    const counts = {};
    const availableKeys = new Set();
    getFixtureGroups(fi).forEach(group => {
      const family = String(group?.family || "").trim().toLowerCase();
      if (!family) return;
      counts[family] = (counts[family] || 0) + 1;
    });
    getFixtureElementDefs(fi).forEach(element => {
      Object.keys(element?.targets || {}).forEach(key => {
        if (getSharedFixtureTargetSpec(key)) {
          availableKeys.add(String(key));
        }
      });
    });

    if (Object.values(counts).some(count => count > 1)) {
      FIXTURE_SHARED_TARGET_SPECS.forEach(spec => {
        if ((counts[spec.family] || 0) > 1 && availableKeys.has(spec.key)) {
          availableShared.add(spec.key);
        }
      });
    }
  }

  availableShared.forEach(key => {
    const spec = getSharedFixtureTargetSpec(key);
    if (!spec || merged[key]) return;
    merged[key] = {
      key: spec.key,
      targetKey: spec.key,
      label: spec.label,
      family: spec.family,
      role: spec.role,
      legacy: false,
      shared: true,
      selectionScope: "fixture_elements",
      groupLabel: "Shared",
    };
  });

  return Object.values(merged).sort((a, b) => orderFor(a).localeCompare(orderFor(b)));
}

// ========================================
// SYSTÈME DE VERROU DMX
// ========================================
window.playbackActive = false;
window.backendPlaybackOwned = false;
window.uiFollowStopFlag = false;
window.uiFollowRunId = 0;
window.effectStartEpoch = performance.now();

// Render mode: "ui" (default) or "backend"
window.renderMode = "ui";
window._renderModeListeners = [];
window.addRenderModeListener = (fn) => {
  if (typeof fn === "function") window._renderModeListeners.push(fn);
};
window.setRenderMode = (mode) => {
  const next = String(mode || "ui").toLowerCase() === "backend" ? "backend" : "ui";
  const prev = window.renderMode;
  window.renderMode = next;
  // At a mode switch, drop any cached preview state to avoid the virtual rig
  // showing stale frames from the previous owner (UI math vs Python state).
  if (prev && prev !== next) {
    for (const k of Object.keys(lastDmxFrames)) delete lastDmxFrames[k];
    devicePreviewRGB = {};
    devicePreviewDimmer = {};
    if (typeof scheduleEnginePreviewRefresh === "function") {
      scheduleEnginePreviewRefresh(true);
    } else if (typeof drawRig === "function") {
      drawRig();
    }
  }
  if (typeof window.onRenderModeChanged === "function") {
    window.onRenderModeChanged(next);
  }
  if (Array.isArray(window._renderModeListeners)) {
    window._renderModeListeners.forEach((fn) => {
      try {
        fn(next);
      } catch (err) {
        console.warn("[RenderMode] listener error:", err);
      }
    });
  }
};

window.DMX_PLAYBACK_UI_FPS = Number(window.DMX_PLAYBACK_UI_FPS || 30);
window.setPlaybackUiFps = (fps) => {
  const raw = Number.parseFloat(String(fps ?? "30"));
  if (!Number.isFinite(raw)) return;
  window.DMX_PLAYBACK_UI_FPS = Math.max(1, Math.min(60, raw));
};

let enginePreviewFrameScheduled = false;
let enginePreviewLastDrawTs = 0;

function refreshEnginePreviewFrame() {
  enginePreviewFrameScheduled = false;
  enginePreviewLastDrawTs = performance.now();

  if (window.backendPlaybackOwned) {
    devicePreviewRGB = {};
    devicePreviewDimmer = {};

    for (const dev of Object.values(rigDevices || {})) {
      if (!dev) continue;
      const universe = parseInt(dev.universe, 10) || 0;
      const frame = lastDmxFrames[universe];
      if (!Array.isArray(frame)) continue;
      const previewChannels = getDevicePrimaryPreviewChannels(dev);

      if (previewChannels.r != null && previewChannels.g != null && previewChannels.b != null) {
        const rAbs = previewChannels.r;
        const gAbs = previewChannels.g;
        const bAbs = previewChannels.b;
        devicePreviewRGB[dev.id] = {
          r: (rAbs >= 0 && rAbs < 512) ? (frame[rAbs] ?? 0) : 0,
          g: (gAbs >= 0 && gAbs < 512) ? (frame[gAbs] ?? 0) : 0,
          b: (bAbs >= 0 && bAbs < 512) ? (frame[bAbs] ?? 0) : 0,
        };
      }

      if (previewChannels.dimmer != null) {
        const dAbs = previewChannels.dimmer;
        if (dAbs >= 0 && dAbs < 512) {
          devicePreviewDimmer[dev.id] = frame[dAbs] ?? 0;
        }
      }
    }
  }

  if (typeof drawRig === "function") {
    drawRig();
  }
}

function scheduleEnginePreviewRefresh(force = false) {
  if (enginePreviewFrameScheduled) return;
  const fps = Math.max(1, Number(window.DMX_PLAYBACK_UI_FPS || 30));
  const minIntervalMs = 1000 / fps;
  const now = performance.now();
  const delay = force ? 0 : Math.max(0, minIntervalMs - (now - enginePreviewLastDrawTs));
  enginePreviewFrameScheduled = true;
  if (delay <= 0) {
    window.requestAnimationFrame(refreshEnginePreviewFrame);
  } else {
    window.setTimeout(() => {
      window.requestAnimationFrame(refreshEnginePreviewFrame);
    }, delay);
  }
}
window.isBackendMode = () => window.renderMode === "backend";

window.fallbackToUiMode = (reason) => {
  if (window.renderMode !== "ui") {
    window.renderMode = "ui";
    if (typeof window.onRenderModeChanged === "function") {
      window.onRenderModeChanged("ui");
    }
    if (reason && typeof window.toast === "function") {
      window.toast(reason, "warning");
    }
  }
};

// Global JS error reporting (helps diagnose silent crashes)
(function initJsErrorReporting() {
  if (window.__jsErrorReporting) return;
  window.__jsErrorReporting = true;

  let lastReportTs = 0;
  const REPORT_THROTTLE_MS = 1000;

  function reportJsError(payload) {
    const now = Date.now();
    if (now - lastReportTs < REPORT_THROTTLE_MS) return;
    lastReportTs = now;
    try {
      fetch("/api/js_log", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {})
      }).catch(() => {});
    } catch (_) {}
  }

  window.addEventListener("error", (event) => {
    const err = event.error || {};
    const payload = {
      level: "error",
      message: String(event.message || err.message || "JS error"),
      source: event.filename || "",
      line: event.lineno || 0,
      column: event.colno || 0,
      stack: String(err.stack || "")
    };
    reportJsError(payload);
    if (typeof window.toast === "function") {
      window.toast("JS error: " + payload.message, "error");
    }
  });

  window.addEventListener("unhandledrejection", (event) => {
    const reason = event.reason || {};
    const payload = {
      level: "error",
      message: String(reason.message || reason || "Unhandled rejection"),
      source: "unhandledrejection",
      line: 0,
      column: 0,
      stack: String(reason.stack || "")
    };
    reportJsError(payload);
    if (typeof window.toast === "function") {
      window.toast("Unhandled error: " + payload.message, "error");
    }
  });
})();

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

const toast = (m, t = "success") => {
  if (!m) return;
  if (window.ui && typeof window.ui.toast === "function") window.ui.toast(m, t);
  else console.log(`[${t}] ${m}`);
};
window.toast = toast;

const confirmModal = async (title, text) => {
  if (window.ui && typeof window.ui.confirmModal === "function") {
    return await window.ui.confirmModal(title, text);
  }
  toast(`(no confirm modal) ${title}: ${text}`, "warning");
  return true;
};

const alertModal = async (title, text, icon = "warning") => {
  if (window.ui && typeof window.ui.alertModal === "function") {
    return await window.ui.alertModal(title, text, icon);
  }
  toast(`${title}: ${text}`, icon === "error" ? "error" : "warning");
  return true;
};

const promptModal = async (title, val = "", ph = "") => {
  if (window.ui && typeof window.ui.promptModal === "function") {
    return await window.ui.promptModal(title, val, ph);
  }
  toast(`(input indisponible) ${title} → action annulée`, "warning");
  return null;
};

const deviceEditModal = async (dev) => {
  if (window.ui && typeof window.ui.deviceEditModal === "function") {
    return await window.ui.deviceEditModal(dev);
  }
  toast("Device edit modal indisponible.", "error");
  return null;
};

const fixtureRemapModal = async (config) => {
  if (window.ui && typeof window.ui.fixtureRemapModal === "function") {
    return await window.ui.fixtureRemapModal(config);
  }
  toast("Fixture remap modal indisponible.", "error");
  return null;
};

const fixtureChangeDecisionModal = async (config) => {
  if (window.ui && typeof window.ui.fixtureChangeDecisionModal === "function") {
    return await window.ui.fixtureChangeDecisionModal(config);
  }
  toast("Fixture change decision modal indisponible.", "error");
  return null;
};

const operationStatusModal = async (config) => {
  if (window.ui && typeof window.ui.operationStatusModal === "function") {
    return await window.ui.operationStatusModal(config);
  }
  toast(config?.hero || config?.message || "Operation completed.", config?.status === "error" ? "error" : "success");
  return true;
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

const _deviceAttrAbsCache = new Map();
function getDeviceAttrAbsChannels(dev) {
  if (!dev) return {};
  const id = dev.id;
  const fixtureKey = dev.fixture;
  const addr = dev.address;
  const cached = _deviceAttrAbsCache.get(id);
  if (cached && cached.fixture === fixtureKey && cached.address === addr) {
    return cached.result;
  }
  const result = buildDeviceAttrAbsChannels(dev, { includeLegacy: true });
  _deviceAttrAbsCache.set(id, { fixture: fixtureKey, address: addr, result });
  return result;
}
function invalidateDeviceAttrCache(deviceId) {
  if (deviceId == null) _deviceAttrAbsCache.clear();
  else _deviceAttrAbsCache.delete(deviceId);
}
window.invalidateDeviceAttrCache = invalidateDeviceAttrCache;

///////////////////////
// API DMX (buffer + pump réseau)
///////////////////////

// Cache des derniers frames reçus de l'engine (visualisation UNIQUEMENT)
const lastDmxFrames = {}; // { [universe]: [512 values] }

// État "shadow" local pour filtrer les envois répétés
const dmxShadowState = {}; // { [key]: Uint8Array(512) }

function getShadowState(key) {
  if (!dmxShadowState[key]) dmxShadowState[key] = new Uint8Array(512);
  return dmxShadowState[key];
}

// Buffer côté front : 1 pipeline par (device_id + universe)
const dmxUniverseBuffers = {}; 
// structure : {
//   [key]: {
//     universe: number,
//     deviceId: string,
//     pending: { ch: val, ... },   // dernières valeurs à envoyer
//     inFlight: false              // requête HTTP en cours
//   }
// };

// ========================================
// NEW ARCHITECTURE: Python handles all DMX
// JS only sends high-level commands
// ========================================

// Send channel values to Python engine (for live controller edits)
async function applyUniverseState(universe, channels, bypassLock = false, deviceId = "ui_live") {
  // UI lock check (for visual feedback only now)
  if (window.dmxLocked && !bypassLock) return;
  if (window.backendPlaybackOwned) return;
  if (window.renderMode === "backend" && (deviceId === "ui_effect" || deviceId === "ui_cue")) return;

  if (!channels || typeof channels !== "object") return;
  const keys = Object.keys(channels);
  if (!keys.length) return;

  const u = Number.isFinite(universe) ? universe : 0;
  const devId = deviceId || "ui_live";
  const bufKey = `${devId}:${u}`;

  // Buffer locally for UI preview
  let state = dmxUniverseBuffers[bufKey];
  if (!state) {
    state = { universe: u, deviceId: devId, pending: {}, inFlight: false };
    dmxUniverseBuffers[bufKey] = state;
  }

  const forceFull = window.DMX_FORCE_FULL_SEND === true;
  const shadow = getShadowState(bufKey);
  let changedCount = 0;

  for (const [k, v] of Object.entries(channels)) {
    const ch = parseInt(k, 10);
    if (!Number.isFinite(ch) || ch < 0 || ch >= 512) continue;
    const val = Math.max(0, Math.min(255, v | 0));
    if (!forceFull) {
      if (shadow[ch] === val) continue;
    }
    shadow[ch] = val;
    state.pending[ch] = val;
    changedCount++;
  }

  if (!changedCount) return;
}

// Send buffered values to Python engine.
// All pending buffers across (deviceId, universe) tuples are coalesced into
// one HTTP POST per pump cycle, grouped by deviceId. The /api/live/channels/bulk
// endpoint commits every universe's channels under a single engine lock so the
// next render frame sees a consistent multi-universe snapshot — no inter-universe
// drift on simultaneous edits.
let _dmxPumpInFlight = false;
async function dmxNetworkPump() {
  if (_dmxPumpInFlight) return;

  // Group pending channels by deviceId, then by universe.
  const byDevice = {}; // {devId: {uni: {ch: val}}}
  const drained = []; // [[state, snapshot], ...] for rollback on failure
  for (const [_key, state] of Object.entries(dmxUniverseBuffers)) {
    const pendingKeys = Object.keys(state.pending);
    if (!pendingKeys.length) continue;
    const u = Number.isFinite(state.universe) ? state.universe : 0;
    const devId = state.deviceId || "ui_live";

    const frame = {};
    for (const k of pendingKeys) frame[k] = state.pending[k];

    let devBucket = byDevice[devId];
    if (!devBucket) {
      devBucket = {};
      byDevice[devId] = devBucket;
    }
    // If two buffers somehow map to the same (devId, universe) (shouldn't),
    // last one wins after merge.
    devBucket[u] = Object.assign(devBucket[u] || {}, frame);

    drained.push([state, frame]);
    state.pending = {};
  }

  if (!drained.length) return;

  _dmxPumpInFlight = true;
  try {
    // One bulk request per device_id (almost always just "ui_live").
    await Promise.all(Object.entries(byDevice).map(([devId, universes]) =>
      fetch("/api/live/channels/bulk", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device_id: devId, universes }),
      })
    ));
  } catch (err) {
    console.warn("[DMX] bulk pump send failed", err);
    // Re-queue (preserve newer pending writes that may have arrived since drain)
    for (const [state, frame] of drained) {
      state.pending = Object.assign({}, frame, state.pending);
    }
  } finally {
    _dmxPumpInFlight = false;
  }
}

// DMX pump interval (20 ms ≈ 50 Hz, comfortably faster than the 120 Hz
// engine tick can consume but still throttled by the engine's global send gate).
setInterval(dmxNetworkPump, 20);

// ========================================
// UI: Packet meter (ArtNet packets/sec)
// ========================================
let packetMeterTimer = null;
const packetMeterState = {
  lastCount: null,
  lastTime: null,
};

async function pollPacketStats() {
  const rateEl = $id("dmx-packet-rate");
  const vuFillEl = $id("dmx-packet-vu-fill");
  if (!rateEl || !vuFillEl) return;

  try {
    const res = await fetch("/api/stats");
    if (!res.ok) return;
    const data = await res.json();
    if (!data || data.ok === false) return;

    const count = Number(data.artnet_packets || 0);
    const now = Number(data.server_time || (Date.now() / 1000));

    let rate = 0;
    if (packetMeterState.lastCount !== null && packetMeterState.lastTime !== null) {
      const dt = now - packetMeterState.lastTime;
      const delta = count - packetMeterState.lastCount;
      if (dt > 0 && delta > 0) {
        rate = delta / dt;
      }
    }

    packetMeterState.lastCount = count;
    packetMeterState.lastTime = now;

    rateEl.textContent = `${rate.toFixed(1)} pkt/s`;
    const maxRate = 60; // target scale for the VU meter
    const pct = Math.max(0, Math.min(100, (rate / maxRate) * 100));
    vuFillEl.style.width = `${pct.toFixed(1)}%`;
  } catch (e) {
    // ignore poll errors to avoid log spam
  }
}

function startPacketMeter() {
  if (packetMeterTimer) return;
  pollPacketStats();
  packetMeterTimer = setInterval(pollPacketStats, 500);
}

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
  let needsPreviewRefresh = false;

  // Update local state from engine for visualization ONLY
  // DO NOT update dmxLocked - that's controlled by JS fade logic
  if (state.universes) {
    let universesChanged = false;
    for (const [uStr, values] of Object.entries(state.universes)) {
      const u = parseInt(uStr, 10);
      const newFrame = Array.isArray(values) ? values.slice(0, 512) : [];
      // Compare against previous frame to avoid redundant preview redraws when
      // the engine broadcasts state but nothing actually moved (idle backend).
      if (!universesChanged) {
        const old = lastDmxFrames[u];
        if (!old || old.length !== newFrame.length) {
          universesChanged = true;
        } else {
          for (let i = 0; i < newFrame.length; i++) {
            if (old[i] !== newFrame[i]) { universesChanged = true; break; }
          }
        }
      }
      lastDmxFrames[u] = newFrame;
    }
    if (universesChanged) {
      needsPreviewRefresh = Boolean(
        window.backendPlaybackOwned ||
        window.identMode ||
        (typeof window.isBackendMode === "function" && window.isBackendMode())
      );
    }
  }

  // Update identify indicator from Python (Python controls identify)
  if (state.identify_active !== undefined) {
    window.identMode = state.identify_active;
    needsPreviewRefresh = true;
  }

  // NOTE: DO NOT update window.dmxLocked from Python!
  // JS controls dmxLocked during cue transitions.
  // Python's fade_active is for server-side fades which we don't use.

  if (state.playback && typeof window.applyBackendPlaybackState === "function") {
    window.applyBackendPlaybackState(state.playback);
  }
  if (needsPreviewRefresh) {
    scheduleEnginePreviewRefresh(false);
  }
}

// Connect SSE on page load
document.addEventListener("DOMContentLoaded", () => {
  setTimeout(connectSSE, 500);
  startPacketMeter();
});

///////////////////////
// FIXTURES
///////////////////////

async function loadFixtures() {
  try {
    const r = await fetch("/api/fixtures");
    fixtures = await r.json();
  } catch (e) {
    fixtures = {};
    console.error(e);
  }
  invalidateDevicePreviewCache();
  invalidateDeviceAttrCache();

  const sel = $id("fixture-type-select");
  if (!sel) return;
  sel.innerHTML = "";
  for (const [name, fx] of Object.entries(fixtures)) {
    if (fx.error) continue;
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = `${fx.meta?.model || fx.info?.model || name} (${name})`;
    sel.appendChild(opt);
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
  if (!sel) return toast("cue-file-select introuvable.", "error");

  if (!sel.value) {
    await refreshCueFileList();
  }

  const filename =
    sel.value ||
    currentCueFilename ||
    sel.options[0]?.value;

  if (!filename) {
    toast("Aucun fichier de cue dispo.", "error");
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
  initSplitLayout();

  // Events rig (pan/zoom/select dans rig.js)
  bindRigCanvasEvents();
  rigCanvas.addEventListener("wheel", onRigWheel, { passive: false });

  bindButtons();
  bindTabs();

  loadFixtures().then(() => {
    refreshCueFileList();
    applyLayoutSplit(); // ensure canvas size + first draw
  });

  if ($id("tab-effects")?.classList.contains("active")) {
    ensureEffectsLoaded().then(() => {
      renderEffectsLibrary();
      renderEffectsTargets();
    });
  }

  startEffectRunner();
});
