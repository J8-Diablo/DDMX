// static/rig.js
// Gestion du rig : devices, canvas pan/zoom, multi-select, controller (sliders, color, position)


// Initialize default values for a device (RGB=white, dimmer=255)
function initDeviceDefaults(deviceId, fixtureName) {
  const fi = fixtures[fixtureName];
  if (!fi) return;

  const funcs = fi.functions || {};
  deviceLocalValues[deviceId] = deviceLocalValues[deviceId] || {};

  // Default RGB to white (255, 255, 255)
  if (funcs.rgb) {
    if (funcs.rgb.red != null) deviceLocalValues[deviceId][funcs.rgb.red] = 255;
    if (funcs.rgb.green != null) deviceLocalValues[deviceId][funcs.rgb.green] = 255;
    if (funcs.rgb.blue != null) deviceLocalValues[deviceId][funcs.rgb.blue] = 255;
  }

  // Default dimmer to full (255)
  if (funcs.dimmer && funcs.dimmer.channel != null) {
    deviceLocalValues[deviceId][funcs.dimmer.channel] = 0;
  }
}

///////////////////////
// MOVEMENT CHANNELS SYNC (PAN/TILT)
///////////////////////

let movementSyncTimer = null;
let lastMovementChannelsPayload = "";
let dummySyncTimer = null;
let lastDummyChannelsPayload = "";

const DUMMY_MIN_CHANNELS = 13;

function buildMovementChannelsByUniverse() {
  const map = {};

  for (const dev of Object.values(rigDevices)) {
    if (!dev) continue;
    const fi = fixtures[dev.fixture] || {};
    const pos = fi.functions?.position;
    if (!pos) continue;

    const u = parseInt(dev.universe, 10) || 0;
    map[u] ||= new Set();

    if (pos.pan?.channel != null) {
      const abs = dev.address + parseInt(pos.pan.channel, 10);
      if (Number.isFinite(abs)) map[u].add(abs);
    }
    if (pos.tilt?.channel != null) {
      const abs = dev.address + parseInt(pos.tilt.channel, 10);
      if (Number.isFinite(abs)) map[u].add(abs);
    }
  }

  const out = {};
  for (const [u, set] of Object.entries(map)) {
    out[u] = Array.from(set).sort((a, b) => a - b);
  }
  return out;
}

async function syncMovementChannelsToEngine() {
  const universes = buildMovementChannelsByUniverse();
  const payload = JSON.stringify({ universes });
  if (payload === lastMovementChannelsPayload) return;
  lastMovementChannelsPayload = payload;

  try {
    await fetch("/api/movement_channels", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: payload,
    });
  } catch (e) {
    console.warn("[DMX] movement channel sync failed:", e);
  }
}

function scheduleMovementSync() {
  if (movementSyncTimer) return;
  movementSyncTimer = setTimeout(() => {
    movementSyncTimer = null;
    syncMovementChannelsToEngine();
  }, 200);
}

function buildDummyChannelsByUniverse() {
  const used = {};

  for (const dev of Object.values(rigDevices)) {
    if (!dev) continue;
    const fi = fixtures[dev.fixture] || {};
    const addrCount = fi.addr_count || 1;
    const u = parseInt(dev.universe, 10) || 0;
    used[u] ||= new Set();
    for (let li = 0; li < addrCount; li++) {
      const abs = dev.address + li;
      if (Number.isFinite(abs) && abs >= 0 && abs < 512) {
        used[u].add(abs);
      }
    }
  }

  const out = {};
  for (const [uStr, set] of Object.entries(used)) {
    const free = [];
    for (let ch = 0; ch < 512 && free.length < DUMMY_MIN_CHANNELS; ch++) {
      if (!set.has(ch)) free.push(ch);
    }
    if (free.length) out[uStr] = free;
  }
  return out;
}

async function syncDummyChannelsToEngine() {
  const universes = buildDummyChannelsByUniverse();
  const payload = JSON.stringify({ universes });
  if (payload === lastDummyChannelsPayload) return;
  lastDummyChannelsPayload = payload;

  try {
    await fetch("/api/dummy_channels", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: payload,
    });
  } catch (e) {
    console.warn("[DMX] dummy channel sync failed:", e);
  }
}

function scheduleDummySync() {
  if (dummySyncTimer) return;
  dummySyncTimer = setTimeout(() => {
    dummySyncTimer = null;
    syncDummyChannelsToEngine();
  }, 200);
}


function snapToGridCenter(v) {
  const step = RIG_GRID_SIZE;
  return Math.round((v - step / 2) / step) * step + step / 2;
}

///////////////////////
// RIG GLOW OVERLAY
///////////////////////

function triggerRigGlow(deviceIds, durationMs = 5000) {
  const now = performance.now();
  const ms = Math.max(0, durationMs || 0);

  (deviceIds || []).forEach(id => {
    const key = String(id);
    rigGlows[key] = { start: now, until: now + ms };
  });

  ensureRigGlowLoop();
  drawRig();
}

function ensureRigGlowLoop() {
  if (rigGlowAnim) return;

  const tick = () => {
    const now = performance.now();
    let hasActive = false;
    for (const [id, glow] of Object.entries(rigGlows)) {
      if (!glow || now >= glow.until) {
        delete rigGlows[id];
      } else {
        hasActive = true;
      }
    }

    if (hasActive) {
      drawRig();
      rigGlowAnim = requestAnimationFrame(tick);
    } else {
      rigGlowAnim = null;
    }
  };

  rigGlowAnim = requestAnimationFrame(tick);
}


///////////////////////
// FIND NEXT FREE ADDRESS
///////////////////////

function findNextFreeAddress(universe, addrCount) {
  const ranges = [];
  for (const d of Object.values(rigDevices)) {
    if (!d) continue;
    if (parseInt(d.universe, 10) !== universe) continue;
    const fi = fixtures[d.fixture] || {};
    const c = fi.addr_count || 1;
    ranges.push([d.address, d.address + c - 1]);
  }
  ranges.sort((a, b) => a[0] - b[0]);

  for (let start = 0; start <= 512 - addrCount; start++) {
    let ok = true;
    for (const [r0, r1] of ranges) {
      const end = start + addrCount - 1;
      if (!(end < r0 || start > r1)) {
        ok = false;
        start = r1;
        break;
      }
    }
    if (ok) return start;
  }
  return null;
}

///////////////////////
// DEVICE CRUD
///////////////////////

function addDeviceFromUI() {
  const fixtureName = $id("fixture-type-select")?.value;
  if (!fixtureName) return toast("Select a fixture first.", "error");

  const fi = fixtures[fixtureName] || {};
  const addrCount = fi.addr_count || 1;

  const cname = $id("fixture-name-input")?.value || `Device ${nextDeviceId}`;
  const universe = parseInt($id("fixture-universe-input")?.value || "0", 10);

  const addrInput = $id("fixture-address-input");
  const manualMode = addrInput && addrInput.value.trim() !== "";
  let address;

  if (!manualMode) {
    const free = findNextFreeAddress(universe, addrCount);
    if (free == null) return toast("No free DMX address in this universe.", "error");
    address = free;
  } else {
    address = clamp(parseInt(addrInput.value, 10) || 0, 0, 511);
  }

  const id = String(nextDeviceId++);
  rigDevices[id] = {
    id,
    fixture: fixtureName,
    cname,
    universe,
    address,
    x: 100,
    y: 100,
  };
  deviceLocalValues[id] = {};
  deviceCurrentGroups[id] = new Set();
  initDeviceDefaults(id, fixtureName);
  scheduleMovementSync();
  scheduleDummySync();

  if (addrInput) addrInput.value = "";

  selectedDeviceOrder = [id];
  selectedDeviceSet = new Set(selectedDeviceOrder);

  refreshControllerFromSelection();
  drawRig();
  toast(`Device ${id} ajouté`);
}

async function editDeviceDialog(id) {
  const dev = rigDevices[id];
  if (!dev) return;

  const res = await deviceEditModal(dev);
  if (!res) return;

  rigDevices[id].cname = res.cname;
  rigDevices[id].universe = clamp(parseInt(res.universe, 10) || 0, 0, 9999);
  rigDevices[id].address = clamp(parseInt(res.address, 10) || 0, 0, 511);
  scheduleMovementSync();
  scheduleDummySync();

  drawRig();
  refreshControllerFromSelection();
}

async function deleteSelectedDevices() {
  if (selectedDeviceOrder.length === 0) return;

  const ok = await confirmModal(
    "Delete devices",
    `Delete ${selectedDeviceOrder.length} selected device(s)?`
  );
  if (!ok) return;

  for (const id of selectedDeviceOrder) {
    delete rigDevices[id];
    delete deviceLocalValues[id];
    delete deviceCurrentGroups[id];
  }
  scheduleMovementSync();
  scheduleDummySync();

  // Nettoie les steps de la cue list
  for (const step of (cuesObj.sequence || [])) {
    if (!step.devices) continue;
    for (const id of selectedDeviceOrder) {
      delete step.devices[id];
      if (step.device_groups) delete step.device_groups[id];
    }
    if (step.device_order) {
      step.device_order = step.device_order.filter(d => !selectedDeviceSet.has(String(d)));
    }
  }

  selectedDeviceOrder = [];
  selectedDeviceSet = new Set();

  renderCueTable();
  refreshControllerFromSelection();
  drawRig();
  if (typeof renderActualEffectsPanel === "function") {
    renderActualEffectsPanel();
  }
  toast("Devices deleted", "info");
}

///////////////////////
// BUILD devices_def POUR SAUVEGARDE
///////////////////////

function buildDevicesDefFromRig() {
  const defs = {};
  for (const [id, dev] of Object.entries(rigDevices)) {
    defs[id] = {
      id,
      fixture: dev.fixture,
      cname: dev.cname,
      universe: dev.universe,
      address: dev.address,
      x: dev.x,
      y: dev.y,
    };
  }
  return defs;
}

// Reconstruit le rig depuis cuesObj.devices_def (appelé au load d'un fichier)
function rebuildRigFromCueFile() {
  const defs = cuesObj.devices_def;
  if (!defs || typeof defs !== "object" || !Object.keys(defs).length) {
    scheduleMovementSync();
    scheduleDummySync();
    drawRig();
    refreshControllerFromSelection();
    if (typeof renderActualEffectsPanel === "function") {
      renderActualEffectsPanel();
    }
    return;
  }

  rigDevices = {};
  deviceLocalValues = {};
  deviceCurrentGroups = {};
  selectedDeviceOrder = [];
  selectedDeviceSet = new Set();
  let maxId = 0;

  for (const [rawId, dev] of Object.entries(defs)) {
    const parsedId = parseInt(dev.id ?? rawId, 10);
    const id = String(Number.isFinite(parsedId) ? parsedId : rawId);
    if (Number.isFinite(parsedId)) {
      maxId = Math.max(maxId, parsedId);
    }

    rigDevices[id] = {
      id,
      fixture: dev.fixture,
      cname: dev.cname ?? `Device ${id}`,
      universe: dev.universe ?? 0,
      address: dev.address ?? 0,
      x: dev.x ?? 100,
      y: dev.y ?? 100,
    };

    deviceLocalValues[id] = {};
    deviceCurrentGroups[id] = new Set();
    initDeviceDefaults(id, dev.fixture);
  }

  nextDeviceId = maxId + 1;

  selectedDeviceOrder = [];
  selectedDeviceSet = new Set();

  scheduleMovementSync();
  scheduleDummySync();
  drawRig();
  refreshControllerFromSelection();
  if (typeof renderActualEffectsPanel === "function") {
    renderActualEffectsPanel();
  }
}

///////////////////////
// CONTROLLER REBUILD
///////////////////////

// --- Tools: ordering of selected devices in rig ---
// Buttons are enabled only when 2+ devices are selected.
function updateRigSortButtonsState() {
  const disabled = !selectedDeviceOrder || selectedDeviceOrder.length < 2;
  const ids = [
    "rig-sort-vert",
    "rig-sort-horiz",
    "rig-sort-vert-one",
    "rig-sort-horiz-one",
    "rig-sort-id",
    "rig-sort-random",
    "rig-sort-reverse"
  ];
  ids.forEach((bid) => {
    const btn = (typeof $id === "function") ? $id(bid) : null;
    if (btn) btn.disabled = disabled;
  });
}

function sortSelectionVertical() {
  if (!selectedDeviceOrder || selectedDeviceOrder.length < 2) return;
  selectedDeviceOrder.sort((a, b) => {
    const da = rigDevices[a], db = rigDevices[b];
    if (!da || !db) return 0;
    // tri du haut vers le bas, puis de gauche à droite
    if (da.y !== db.y) return da.y - db.y;
    return da.x - db.x;
  });
  selectedDeviceSet = new Set(selectedDeviceOrder);
  window.selectionGroups = null; // Clear groups - each device is individual
  refreshControllerFromSelection();
  drawRig();
}

function sortSelectionHorizontal() {
  if (!selectedDeviceOrder || selectedDeviceOrder.length < 2) return;
  selectedDeviceOrder.sort((a, b) => {
    const da = rigDevices[a], db = rigDevices[b];
    if (!da || !db) return 0;
    // tri de gauche à droite, puis du haut vers le bas
    if (da.x !== db.x) return da.x - db.x;
    return da.y - db.y;
  });
  selectedDeviceSet = new Set(selectedDeviceOrder);
  window.selectionGroups = null; // Clear groups - each device is individual
  refreshControllerFromSelection();
  drawRig();
}

// Vertical ONE: devices in same column (same X) get same index
// Grouped by column, columns sorted left to right
function sortSelectionVerticalOne() {
  if (!selectedDeviceOrder || selectedDeviceOrder.length < 2) return;

  // Group devices by X position (with tolerance for "same column")
  const tolerance = 20; // pixels tolerance for "same column"
  const groups = [];

  // Sort by X first to group columns
  const sorted = [...selectedDeviceOrder].sort((a, b) => {
    const da = rigDevices[a], db = rigDevices[b];
    if (!da || !db) return 0;
    return da.x - db.x;
  });

  // Group into columns
  let currentGroup = [];
  let currentX = null;

  for (const id of sorted) {
    const dev = rigDevices[id];
    if (!dev) continue;

    if (currentX === null || Math.abs(dev.x - currentX) <= tolerance) {
      currentGroup.push(id);
      if (currentX === null) currentX = dev.x;
    } else {
      if (currentGroup.length) groups.push(currentGroup);
      currentGroup = [id];
      currentX = dev.x;
    }
  }
  if (currentGroup.length) groups.push(currentGroup);

  // Sort devices within each column by Y (top to bottom)
  for (const group of groups) {
    group.sort((a, b) => {
      const da = rigDevices[a], db = rigDevices[b];
      if (!da || !db) return 0;
      return da.y - db.y;
    });
  }

  // Flatten: all devices in same column are consecutive
  selectedDeviceOrder = groups.flat();
  selectedDeviceSet = new Set(selectedDeviceOrder);

  // Store group info for effects (devices in same column = same phase)
  window.selectionGroups = groups;

  refreshControllerFromSelection();
  drawRig();
  toast(`${groups.length} columns`, "info");
}

// Horizontal ONE: devices in same row (same Y) get same index
// Grouped by row, rows sorted top to bottom
function sortSelectionHorizontalOne() {
  if (!selectedDeviceOrder || selectedDeviceOrder.length < 2) return;

  // Group devices by Y position (with tolerance for "same row")
  const tolerance = 20; // pixels tolerance for "same row"
  const groups = [];

  // Sort by Y first to group rows
  const sorted = [...selectedDeviceOrder].sort((a, b) => {
    const da = rigDevices[a], db = rigDevices[b];
    if (!da || !db) return 0;
    return da.y - db.y;
  });

  // Group into rows
  let currentGroup = [];
  let currentY = null;

  for (const id of sorted) {
    const dev = rigDevices[id];
    if (!dev) continue;

    if (currentY === null || Math.abs(dev.y - currentY) <= tolerance) {
      currentGroup.push(id);
      if (currentY === null) currentY = dev.y;
    } else {
      if (currentGroup.length) groups.push(currentGroup);
      currentGroup = [id];
      currentY = dev.y;
    }
  }
  if (currentGroup.length) groups.push(currentGroup);

  // Sort devices within each row by X (left to right)
  for (const group of groups) {
    group.sort((a, b) => {
      const da = rigDevices[a], db = rigDevices[b];
      if (!da || !db) return 0;
      return da.x - db.x;
    });
  }

  // Flatten: all devices in same row are consecutive
  selectedDeviceOrder = groups.flat();
  selectedDeviceSet = new Set(selectedDeviceOrder);

  // Store group info for effects (devices in same row = same phase)
  window.selectionGroups = groups;

  refreshControllerFromSelection();
  drawRig();
  toast(`${groups.length} rows`, "info");
}

function sortSelectionById() {
  if (!selectedDeviceOrder || selectedDeviceOrder.length < 2) return;
  selectedDeviceOrder.sort((a, b) => {
    const na = parseInt(a, 10);
    const nb = parseInt(b, 10);
    const fa = Number.isFinite(na);
    const fb = Number.isFinite(nb);
    if (fa && fb && na !== nb) return na - nb;  // tri numérique si possible
    return String(a).localeCompare(String(b));   // sinon lexicographique
  });
  selectedDeviceSet = new Set(selectedDeviceOrder);
  window.selectionGroups = null; // Clear groups
  refreshControllerFromSelection();
  drawRig();
}

function shuffleSelection() {
  if (!selectedDeviceOrder || selectedDeviceOrder.length < 2) return;
  // Fisher–Yates
  for (let i = selectedDeviceOrder.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [selectedDeviceOrder[i], selectedDeviceOrder[j]] = [selectedDeviceOrder[j], selectedDeviceOrder[i]];
  }
  selectedDeviceSet = new Set(selectedDeviceOrder);
  window.selectionGroups = null; // Clear groups
  refreshControllerFromSelection();
  drawRig();
}

function reverseSelectionOrder() {
  if (!selectedDeviceOrder || selectedDeviceOrder.length < 2) return;
  selectedDeviceOrder.reverse();
  selectedDeviceSet = new Set(selectedDeviceOrder);
  // Keep selectionGroups - just reverse the group order
  if (window.selectionGroups) {
    window.selectionGroups = window.selectionGroups.reverse();
  }
  refreshControllerFromSelection();
  drawRig();
}

// Brancher les boutons de tri (scripts chargés après le DOM dans index.html)
(function initRigSortButtons() {
  const btnVert     = (typeof $id === "function") ? $id("rig-sort-vert") : null;
  const btnHoriz    = (typeof $id === "function") ? $id("rig-sort-horiz") : null;
  const btnVertOne  = (typeof $id === "function") ? $id("rig-sort-vert-one") : null;
  const btnHorizOne = (typeof $id === "function") ? $id("rig-sort-horiz-one") : null;
  const btnId       = (typeof $id === "function") ? $id("rig-sort-id") : null;
  const btnRnd      = (typeof $id === "function") ? $id("rig-sort-random") : null;
  const btnRev      = (typeof $id === "function") ? $id("rig-sort-reverse") : null;

  if (btnVert)     btnVert.addEventListener("click",     (e) => { e.preventDefault(); sortSelectionVertical(); });
  if (btnHoriz)    btnHoriz.addEventListener("click",    (e) => { e.preventDefault(); sortSelectionHorizontal(); });
  if (btnVertOne)  btnVertOne.addEventListener("click",  (e) => { e.preventDefault(); sortSelectionVerticalOne(); });
  if (btnHorizOne) btnHorizOne.addEventListener("click", (e) => { e.preventDefault(); sortSelectionHorizontalOne(); });
  if (btnId)       btnId.addEventListener("click",       (e) => { e.preventDefault(); sortSelectionById(); });
  if (btnRnd)      btnRnd.addEventListener("click",      (e) => { e.preventDefault(); shuffleSelection(); });
  if (btnRev)      btnRev.addEventListener("click",      (e) => { e.preventDefault(); reverseSelectionOrder(); });

  updateRigSortButtonsState();
})();


function refreshControllerFromSelection() {
  const info = $id("controller-info");
  rgbWidgetRef = null;
  posWidgetRef = null;
  updateRigSortButtonsState();

  if (!selectedDeviceOrder.length) {
    if (info) info.textContent = "Select device(s) in rig.";
    $id("intensity-body") && ($id("intensity-body").innerHTML = "");
    $id("color-body") && ($id("color-body").innerHTML = "");
    $id("position-body") && ($id("position-body").innerHTML = "");
    $id("beam-body") && ($id("beam-body").innerHTML = "");
    if ($id('tab-effects')?.classList.contains('active')) renderEffectsTargets();
    return;
  }

  if (info) info.textContent = `${selectedDeviceOrder.length} device(s) selected.`;

  const firstId = selectedDeviceOrder[0];
  const first = rigDevices[firstId];
  if (!first) {
    $id("intensity-body") && ($id("intensity-body").innerHTML = "");
    $id("color-body") && ($id("color-body").innerHTML = "");
    $id("position-body") && ($id("position-body").innerHTML = "");
    $id("beam-body") && ($id("beam-body").innerHTML = "");
    if ($id('tab-effects')?.classList.contains('active')) renderEffectsTargets();
    return;
  }

  const fi = fixtures[first.fixture] || {};
  const funcs = fi.functions || {};

  const ib = $id("intensity-body");
  if (ib) {
    ib.innerHTML = "";
    if (funcs.dimmer) addLocalSlider(ib, "Dimmer", funcs.dimmer.channel);
    else ib.innerHTML = "<span class='muted'>No dimmer defined.</span>";
  }

  const cb = $id("color-body");
  if (cb) {
    cb.innerHTML = "";
    if (funcs.rgb) addRgbControls(cb, funcs.rgb);
    else cb.innerHTML = "<span class='muted'>No RGB function defined.</span>";
  }

  const pb = $id("position-body");
  if (pb) {
    pb.innerHTML = "";
    if (funcs.position) addPositionControls(pb, funcs.position);
    else pb.innerHTML = "<span class='muted'>No position function defined.</span>";
  }

  const bb = $id("beam-body");
  if (bb) {
    bb.innerHTML = "";
    if (funcs.beam || funcs.focus) addBeamControls(bb, funcs);
    else bb.innerHTML = "<span class='muted'>No beam controls yet.</span>";
  }

  // Si l’onglet "Effects" est actif, on rafraîchit l’affichage des groupes
  if ($id('tab-effects')?.classList.contains('active')) {
    renderEffectsTargets();
  }
}


///////////////////////
// SLIDERS GÉNÉRIQUES
///////////////////////

function addLocalSlider(container, label, localIndex, opts = {}) {
  const row = document.createElement("div");
  row.className = "ctrl-row";

  const lab = document.createElement("label");
  lab.textContent = `${label} (${localIndex})`;

  const slider = document.createElement("input");
  slider.type = "range";
  slider.min = "0";
  slider.max = "255";
  slider.className = "ctrl-slider " + (opts.className || "");

  const valSpan = document.createElement("div");
  valSpan.className = "slider-value";

  const getCommonValue = () => {
    const vals = [];
    for (const id of selectedDeviceOrder) {
      vals.push(deviceLocalValues[id]?.[localIndex] ?? 0);
    }
    return vals[0] ?? 0;
  };

  slider.value = getCommonValue();
  valSpan.textContent = slider.value;

  slider.oninput = () => {
    const v = parseInt(slider.value, 10) || 0;
    valSpan.textContent = v;

    for (const id of selectedDeviceOrder) {
      deviceLocalValues[id] ||= {};
      deviceLocalValues[id][localIndex] = v;
    }

    applySelectionToEngine(true);
    drawRig();

    syncRgbWidgetFromFirstDevice();
    syncPosWidgetFromFirstDevice();
  };

  slider.ondblclick = () => {
    slider.value = "128";
    slider.dispatchEvent(new Event("input"));
  };

  row.appendChild(lab);
  row.appendChild(slider);
  row.appendChild(valSpan);
  container.appendChild(row);

  return slider;
}

///////////////////////
// RGB + Color Wheel
///////////////////////

function addRgbControls(container, rgbMap) {
  const preview = document.createElement("div");
  preview.className = "color-preview";
  container.appendChild(preview);

  const wheelWrap = document.createElement("div");
  wheelWrap.style.display = "flex";
  wheelWrap.style.alignItems = "center";
  wheelWrap.style.gap = "10px";

  const wheel = document.createElement("div");
  wheel.className = "color-wheel";
  const cursor = document.createElement("div");
  cursor.className = "wheel-cursor";
  wheel.appendChild(cursor);

  wheelWrap.appendChild(wheel);
  container.appendChild(wheelWrap);

  const sliders = {};

  const comps = [
    { name: "Red", key: "red" },
    { name: "Green", key: "green" },
    { name: "Blue", key: "blue" },
  ];

  comps.forEach(c => {
    const li = rgbMap[c.key];
    sliders[c.key] = addLocalSlider(container, c.name, li);
  });

  rgbWidgetRef = { wheelEl: wheel, cursorEl: cursor, sliders, rgbMap, previewEl: preview };

  function setFromWheelEvent(e) {
    const { x, y } = elementCoords(e, wheel);
    const w = wheel.clientWidth, h = wheel.clientHeight;
    const cx = w / 2, cy = h / 2;
    const dx = x - cx, dy = y - cy;
    const r = Math.sqrt(dx * dx + dy * dy);
    const maxR = Math.min(w, h) / 2;

    let s = clamp(r / maxR, 0, 1);
    let hDeg = (Math.atan2(dy, dx) * 180 / Math.PI + 360) % 360;

    if (r < maxR * 0.12) {
      s = 0;
      hDeg = 0;
    }

    const { r: rr, g: gg, b: bb } = hsvToRgb(hDeg, s, 1);

    sliders.red.value = rr;
    sliders.green.value = gg;
    sliders.blue.value = bb;
    sliders.red.dispatchEvent(new Event("input"));
    sliders.green.dispatchEvent(new Event("input"));
    sliders.blue.dispatchEvent(new Event("input"));

    moveWheelCursor(hDeg, s);
  }

  let dragging = false;
  wheel.onmousedown = (e) => { dragging = true; setFromWheelEvent(e); };
  window.addEventListener("mousemove", (e) => dragging && setFromWheelEvent(e));
  window.addEventListener("mouseup", () => dragging = false);

  wheel.ondblclick = () => {
    sliders.red.value = 255;
    sliders.green.value = 255;
    sliders.blue.value = 255;
    sliders.red.dispatchEvent(new Event("input"));
    sliders.green.dispatchEvent(new Event("input"));
    sliders.blue.dispatchEvent(new Event("input"));
    moveWheelCursor(0, 0);
  };

  updateColorPreview(preview, rgbMap);
  syncRgbWidgetFromFirstDevice();
}

function moveWheelCursor(hDeg, s) {
  if (!rgbWidgetRef) return;
  const wheel = rgbWidgetRef.wheelEl;
  const cursor = rgbWidgetRef.cursorEl;
  const w = wheel.clientWidth, h = wheel.clientHeight;
  const maxR = Math.min(w, h) / 2;
  const rad = hDeg * Math.PI / 180;
  const r = s * maxR;

  const x = (w / 2) + Math.cos(rad) * r;
  const y = (h / 2) + Math.sin(rad) * r;
  cursor.style.left = `${x}px`;
  cursor.style.top = `${y}px`;
}

function updateColorPreview(preview, rgbMap) {
  const firstId = selectedDeviceOrder[0];
  const vals = deviceLocalValues[firstId] || {};
  const r = vals[rgbMap.red] ?? 0;
  const g = vals[rgbMap.green] ?? 0;
  const b = vals[rgbMap.blue] ?? 0;
  preview.style.background = `rgb(${r},${g},${b})`;
}

function syncRgbWidgetFromFirstDevice() {
  if (!rgbWidgetRef || !selectedDeviceOrder.length) return;
  const id = selectedDeviceOrder[0];
  const vals = deviceLocalValues[id] || {};
  const { rgbMap, previewEl } = rgbWidgetRef;

  const r = vals[rgbMap.red] ?? 0;
  const g = vals[rgbMap.green] ?? 0;
  const b = vals[rgbMap.blue] ?? 0;

  const hsv = rgbToHsv(r, g, b);
  moveWheelCursor(hsv.h, hsv.s);
  if (previewEl) updateColorPreview(previewEl, rgbMap);
}

///////////////////////
// Position XY + Pan/Tilt
///////////////////////

function addPositionControls(container, posMap) {
  const panIdx = posMap.pan?.channel;
  const tiltIdx = posMap.tilt?.channel;

  // Layout général : XY + Tilt à droite + Pan en dessous
  const layout = document.createElement("div");
  layout.className = "position-layout";
  container.appendChild(layout);

  // --- zone XY ---
  const xyWrapper = document.createElement("div");
  xyWrapper.className = "position-xy-wrapper";
  layout.appendChild(xyWrapper);

  const xy = document.createElement("div");
  xy.className = "xy-area";
  const cursor = document.createElement("div");
  cursor.className = "xy-cursor";
  xy.appendChild(cursor);
  xyWrapper.appendChild(xy);

  // --- Pan (sous le carré) ---
  const panWrapper = document.createElement("div");
  panWrapper.className = "position-pan-wrapper";
  layout.appendChild(panWrapper);

  // --- Tilt (à droite du carré) ---
  const tiltWrapper = document.createElement("div");
  tiltWrapper.className = "position-tilt-wrapper";
  layout.appendChild(tiltWrapper);

  let panSlider = null;
  let tiltSlider = null;

  if (panIdx != null) {
    panSlider = addLocalSlider(panWrapper, "Pan", panIdx);
  }

  if (tiltIdx != null) {
    // on lui donne juste une classe en plus pour le style vertical
    tiltSlider = addLocalSlider(tiltWrapper, "Tilt", tiltIdx, {
      className: "tilt-row"
    });
  }

  posWidgetRef = { xyEl: xy, cursorEl: cursor, panSlider, tiltSlider, panIdx, tiltIdx };

  function setFromXYEvent(e) {
    const { x, y } = elementCoords(e, xy);
    const w = xy.clientWidth, h = xy.clientHeight;
    let nx = clamp(x / w, 0, 1);
    let ny = clamp(y / h, 0, 1);

    const panVal = Math.round(nx * 255);
    const tiltVal = Math.round(ny * 255);

    if (panSlider) {
      panSlider.value = panVal;
      panSlider.dispatchEvent(new Event("input"));
    }
    if (tiltSlider) {
      tiltSlider.value = tiltVal;
      tiltSlider.dispatchEvent(new Event("input"));
    }

    moveXYCursor(nx, ny);
  }

  let dragging = false;
  xy.onmousedown = (e) => { dragging = true; setFromXYEvent(e); };
  window.addEventListener("mousemove", (e) => dragging && setFromXYEvent(e));
  window.addEventListener("mouseup", () => dragging = false);

  xy.ondblclick = () => {
    if (panSlider) { panSlider.value = "128"; panSlider.dispatchEvent(new Event("input")); }
    if (tiltSlider) { tiltSlider.value = "128"; tiltSlider.dispatchEvent(new Event("input")); }
    moveXYCursor(0.5, 0.5);
  };

  syncPosWidgetFromFirstDevice();
}

///////////////////////
// Beam / Focus
///////////////////////

function addBeamControls(container, funcs) {
  const focusIdx = funcs?.focus?.channel ?? funcs?.beam?.focus?.channel;
  if (focusIdx != null) {
    addLocalSlider(container, "Focus", focusIdx);
    return;
  }

  container.innerHTML = "<span class='muted'>No beam controls yet.</span>";
}


function moveXYCursor(nx, ny) {
  if (!posWidgetRef) return;
  const xy = posWidgetRef.xyEl, cursor = posWidgetRef.cursorEl;
  const w = xy.clientWidth, h = xy.clientHeight;
  cursor.style.left = `${nx * w}px`;
  cursor.style.top = `${ny * h}px`;
}

function syncPosWidgetFromFirstDevice() {
  if (!posWidgetRef || !selectedDeviceOrder.length) return;
  const id = selectedDeviceOrder[0];
  const vals = deviceLocalValues[id] || {};

  const panIdx = posWidgetRef.panIdx;
  const tiltIdx = posWidgetRef.tiltIdx;

  const panVal = panIdx != null ? (vals[panIdx] ?? 128) : 128;
  const tiltVal = tiltIdx != null ? (vals[tiltIdx] ?? 128) : 128;

  moveXYCursor(panVal / 255, tiltVal / 255);
}


///////////////////////
// APPLY LIVE SELECTION -> ENGINE
///////////////////////

async function applySelectionToEngine(silent = false) {
  // ========================================
  // PROTECTION : Verrou DMX
  // ========================================
  if (window.dmxLocked) {
    console.log('[RIG] Blocked by DMX lock - transition in progress');
    return;
  }
  
  if (!selectedDeviceOrder.length) return;

  // Ensure movement channels are synced at least once (needed after backend restart)
  if (!lastMovementChannelsPayload) {
    await syncMovementChannelsToEngine();
  }

  const { devices } = buildDevicesBlockFromSelection();
  if (!devices) return;

  const perU = {};
  for (const d of Object.values(devices)) {
    const ch = d.channels || {};
    const u = parseInt(ch.Universe, 10) || 0;
    perU[u] ||= {};
    for (const [k, v] of Object.entries(ch)) {
      if (k === "Universe") continue;
      perU[u][k] = v;
    }
  }

  for (const [uStr, flat] of Object.entries(perU)) {
    const u = parseInt(uStr, 10) || 0;
    await applyUniverseState(u, flat, false, "ui_live");
  }

  if (!silent) toast("Applied", "info");
}

///////////////////////
// RIG DRAW + EVENTS
///////////////////////

// Hit test en coordonnées monde
function hitTestDeviceWorld(wx, wy) {
  const w = 60;
  const h = 36;
  for (const dev of Object.values(rigDevices)) {
    const dx = Math.abs(wx - dev.x);
    const dy = Math.abs(wy - dev.y);
    if (dx <= w / 2 && dy <= h / 2) {
      return dev;
    }
  }
  return null;
}

function drawRig() {
  if (!rigCtx || !rigCanvas) return;
  rigCtx.clearRect(0, 0, rigCanvas.width, rigCanvas.height);

  const nowGlow = performance.now();
  for (const [gid, glow] of Object.entries(rigGlows)) {
    if (!glow || nowGlow >= glow.until) delete rigGlows[gid];
  }

  // fond
  rigCtx.fillStyle = "#0b0d12";
  rigCtx.fillRect(0, 0, rigCanvas.width, rigCanvas.height);

  // grid monde -> écran
  rigCtx.save();
  rigCtx.strokeStyle = "#1a1f2b";
  rigCtx.lineWidth = 1;

  const scale = rigView.scale;
  const offX = rigView.offsetX;
  const offY = rigView.offsetY;

  const minWorldX = (-offX) / scale;
  const minWorldY = (-offY) / scale;
  const maxWorldX = (rigCanvas.width - offX) / scale;
  const maxWorldY = (rigCanvas.height - offY) / scale;

  const startX = Math.floor(minWorldX / RIG_GRID_SIZE) * RIG_GRID_SIZE;
  const endX = Math.ceil(maxWorldX / RIG_GRID_SIZE) * RIG_GRID_SIZE;
  const startY = Math.floor(minWorldY / RIG_GRID_SIZE) * RIG_GRID_SIZE;
  const endY = Math.ceil(maxWorldY / RIG_GRID_SIZE) * RIG_GRID_SIZE;

  for (let x = startX; x <= endX; x += RIG_GRID_SIZE) {
    const sx = x * scale + offX;
    rigCtx.beginPath();
    rigCtx.moveTo(sx, 0);
    rigCtx.lineTo(sx, rigCanvas.height);
    rigCtx.stroke();
  }

  for (let y = startY; y <= endY; y += RIG_GRID_SIZE) {
    const sy = y * scale + offY;
    rigCtx.beginPath();
    rigCtx.moveTo(0, sy);
    rigCtx.lineTo(rigCanvas.width, sy);
    rigCtx.stroke();
  }
  rigCtx.restore();

  const RIG_DETAIL_MIN_SCALE = 0.8; // à ajuster selon ton goût

  // devices
  for (const dev of Object.values(rigDevices)) {
    const fi = fixtures[dev.fixture] || {};
    const funcs = fi.functions || {};
    const isSel = selectedDeviceSet.has(String(dev.id));
  
    // Dimensions en monde (cohérentes avec hitTestDeviceWorld)
    const baseW = 80;
    const baseH = 80;
    const halfW = baseW / 2;
    const halfH = baseH / 2;
  
    // Coins monde -> écran (le scale est pris en compte ici)
    const topLeft = worldToScreen(dev.x - halfW, dev.y - halfH);
    const bottomRight = worldToScreen(dev.x + halfW, dev.y + halfH);

    const x = topLeft.x;
    const y = topLeft.y;
    const w = bottomRight.x - topLeft.x;
    const h = bottomRight.y - topLeft.y;

    // Halo autour du device (glow)
    const glow = rigGlows[String(dev.id)];
    if (glow) {
      const pulse = 0.35 + 0.25 * Math.sin((nowGlow - glow.start) / 180);
      const center = worldToScreen(dev.x, dev.y);
      const rx = Math.max(w * 0.65, 24);
      const ry = Math.max(h * 0.55, 18);

      rigCtx.save();
      rigCtx.strokeStyle = `rgba(96, 199, 255, ${pulse})`;
      rigCtx.lineWidth = Math.max(4, 2 + rigView.scale * 2);
      rigCtx.shadowColor = "rgba(96, 199, 255, 0.7)";
      rigCtx.shadowBlur = 20;
      rigCtx.beginPath();
      rigCtx.ellipse(center.x, center.y, rx, ry, 0, 0, Math.PI * 2);
      rigCtx.stroke();
      rigCtx.restore();
    }
  
    // ---- FACTEUR DE SCALE + SEUIL DE DÉTAILS ----
    const scale = rigView.scale || 1;
  
    // Seuil à partir duquel on affiche texte + ID + numéro
    const showDetails = scale >= RIG_DETAIL_MIN_SCALE;
  
    // même logique qu'avant pour le texte, mais clampé
    const textScale = clamp(scale, 0.5, 3);
    const fontSize = 12 * textScale;
    const selBoxSize = 18 * textScale;
    const rgbRadius = clamp(6 * textScale, 2, 20);

    // Fond du device
    rigCtx.fillStyle = isSel ? "#4e8cff" : "#2a2f3a";
    rigCtx.strokeStyle = "#000";
  
    rigCtx.fillRect(x, y, w, h);
    rigCtx.strokeRect(x, y, w, h);
  
    // ----- DÉTAILS (ID + cname + numéro de sélection) UNIQUEMENT SI ZOOM SUFFISANT -----
    if (showDetails) {
      // Texte (ID + cname)
      rigCtx.fillStyle = "#fff";
      rigCtx.font = `${fontSize}px system-ui`;
  
      const line1Y = y + 4 + fontSize;          // première ligne
      const line2Y = line1Y + fontSize * 1.1;   // seconde ligne
  
      rigCtx.fillText(`ID ${dev.id}`, x + 4 * textScale, line1Y);
      rigCtx.fillText(`${dev.cname}`, x + 4 * textScale, line2Y);
  
      // Numéro d’ordre de sélection (ou numéro de groupe si groupé)
      if (isSel) {
        const devIdStr = String(dev.id);
        let displayNum;
        let boxColor = "#000";

        // Check if we have selection groups (Vert ONE / Horiz ONE)
        if (window.selectionGroups && Array.isArray(window.selectionGroups)) {
          // Find which group this device is in
          for (let gi = 0; gi < window.selectionGroups.length; gi++) {
            if (window.selectionGroups[gi].map(String).includes(devIdStr)) {
              displayNum = gi + 1; // Group number (1-based)
              boxColor = "#6b21a8"; // Purple for grouped devices
              break;
            }
          }
          if (displayNum === undefined) {
            displayNum = selectedDeviceOrder.indexOf(devIdStr) + 1;
          }
        } else {
          displayNum = selectedDeviceOrder.indexOf(devIdStr) + 1;
        }

        rigCtx.fillStyle = boxColor;
        rigCtx.fillRect(x + w - selBoxSize, y, selBoxSize, selBoxSize);

        rigCtx.fillStyle = "#fff";
        const numX = x + w - selBoxSize / 2 - fontSize * 0.25;
        const numY = y + fontSize * 0.85;
        rigCtx.fillText(String(displayNum), numX, numY);
      }
    }
  
    // ----- APERÇU RGB : TOUJOURS AFFICHÉ, MÊME EN GROS DÉZOOM -----
    if (funcs.rgb) {
      const pv = devicePreviewRGB[dev.id];
      let r = 0, g = 0, b = 0;
      if (pv) { r = pv.r; g = pv.g; b = pv.b; }
      else {
        const lv = deviceLocalValues[dev.id] || {};
        r = lv[funcs.rgb.red] ?? 255;
        g = lv[funcs.rgb.green] ?? 255;
        b = lv[funcs.rgb.blue] ?? 255;
      }

      // Apply dimmer to the color preview
      let dimmerFactor = 1.0;
      if (funcs.dimmer) {
        const dimmerVal = devicePreviewDimmer[dev.id] ?? (deviceLocalValues[dev.id]?.[funcs.dimmer.channel] ?? 255);
        dimmerFactor = dimmerVal / 255;
      }
      r = Math.round(r * dimmerFactor);
      g = Math.round(g * dimmerFactor);
      b = Math.round(b * dimmerFactor);

      rigCtx.fillStyle = `rgb(${r},${g},${b})`;

      if (showDetails) {
        // 🔍 Mode zoomé : petit rond en bas à droite (comme avant)
        rigCtx.beginPath();
        rigCtx.arc(
          x + w - rgbRadius - 2 * textScale,
          y + h - rgbRadius - 2 * textScale,
          rgbRadius,
          0,
          Math.PI * 2
        );
        rigCtx.fill();
      } else {
        // 🟢 Mode dézoom : gros “pixel” centré qui prend toute la place
        const bigRadius = Math.min(w, h) / 2 - 2;  // marge de 2 px
        rigCtx.beginPath();
        rigCtx.arc(
          x + w / 2,
          y + h / 2,
          bigRadius,
          0,
          Math.PI * 2
        );
        rigCtx.fill();
      }
    }
  }
  
  
  
  // rectangle de sélection (si en cours)
  if (window._rigSelectionRect) {
    const { x1, y1, x2, y2 } = window._rigSelectionRect;
    const p1 = worldToScreen(x1, y1);
    const p2 = worldToScreen(x2, y2);
    const rx = Math.min(p1.x, p2.x);
    const ry = Math.min(p1.y, p2.y);
    const rw = Math.abs(p2.x - p1.x);
    const rh = Math.abs(p2.y - p1.y);

    rigCtx.save();
    rigCtx.strokeStyle = "#4e8cff";
    rigCtx.lineWidth = 1;
    rigCtx.setLineDash([4, 3]);
    rigCtx.strokeRect(rx, ry, rw, rh);
    rigCtx.restore();
  }
}

///////////////////////
// ZOOM MOLETTE
///////////////////////

function onRigWheel(e) {
  e.preventDefault();
  if (!rigCanvas) return;

  const { x: px, y: py } = canvasCoords(e, rigCanvas);
  const worldBefore = screenToWorld(px, py);

  const zoomFactor = e.deltaY > 0 ? 0.9 : 1.1;
  const newScale = clamp(rigView.scale * zoomFactor, RIG_MIN_SCALE, RIG_MAX_SCALE);

  rigView.scale = newScale;

  rigView.offsetX = px - worldBefore.x * rigView.scale;
  rigView.offsetY = py - worldBefore.y * rigView.scale;

  drawRig();
}

///////////////////////
// EVENTS RIG (pan, multi-select, drag group)
///////////////////////

function bindRigCanvasEvents() {
  let isMouseDown = false;
  let dragMode = null; // "pan" | "devices" | "select-rect" | null

  let dragPanStart = null;          // {px,py}
  let dragPanOffsetStart = null;    // {x,y}
  let dragDevicesBaseWorld = null;  // {x,y}
  let dragDevicesInitialPositions = null; // {id:{x,y}}
  let dragSelectStartWorld = null;  // {x,y}

  let dragBaseSelectedSet = null;
  let dragBaseSelectedOrder = [];
  let dragRectOrder = [];

  rigCanvas.onmousedown = (e) => {
    if (e.button !== 0) return;
    if (!rigCanvas) return;

    isMouseDown = true;
    const { px, py, wx, wy } = eventToWorld(e);
    const hitDev = hitTestDeviceWorld(wx, wy);
    const ctrl = e.ctrlKey || e.metaKey;

    if (hitDev) {
      const sid = String(hitDev.id);

      if (ctrl) {
        if (selectedDeviceSet.has(sid)) {
          selectedDeviceSet.delete(sid);
          selectedDeviceOrder = selectedDeviceOrder.filter(d => d !== sid);
        } else {
          selectedDeviceSet.add(sid);
          selectedDeviceOrder.push(sid);
        }
      } else {
        if (!selectedDeviceSet.has(sid)) {
          selectedDeviceOrder = [sid];
          selectedDeviceSet = new Set(selectedDeviceOrder);
        }
      }

      refreshControllerFromSelection();

      if (selectedDeviceSet.has(sid)) {
        dragMode = "devices";
        dragDevicesBaseWorld = { x: wx, y: wy };
        dragDevicesInitialPositions = {};
        for (const id of selectedDeviceOrder) {
          const d = rigDevices[id];
          if (!d) continue;
          dragDevicesInitialPositions[id] = { x: d.x, y: d.y };
        }
        window._rigSelectionRect = null;
      }

      drawRig();
      return;
    }

    // clic sur fond
    if (ctrl) {
      dragMode = "select-rect";
      dragSelectStartWorld = { x: wx, y: wy };
      window._rigSelectionRect = { x1: wx, y1: wy, x2: wx, y2: wy };

      dragBaseSelectedSet = new Set(selectedDeviceSet);
      dragBaseSelectedOrder = [...selectedDeviceOrder];
      dragRectOrder = [];
    } else {
      dragMode = "pan";
      dragPanStart = { px, py };
      dragPanOffsetStart = { x: rigView.offsetX, y: rigView.offsetY };

      selectedDeviceOrder = [];
      selectedDeviceSet = new Set();
      refreshControllerFromSelection();
    }

    drawRig();
  };

  rigCanvas.onmousemove = (e) => {
    if (!isMouseDown || !dragMode) return;
    if (!rigCanvas) return;

    if (dragMode === "devices") {
      const { wx, wy } = eventToWorld(e);
      const dx = wx - dragDevicesBaseWorld.x;
      const dy = wy - dragDevicesBaseWorld.y;

      for (const id of selectedDeviceOrder) {
        const base = dragDevicesInitialPositions[id];
        if (!base) continue;
        rigDevices[id].x = snapToGridCenter(base.x + dx);
        rigDevices[id].y = snapToGridCenter(base.y + dy);
      }
      drawRig();
      return;
    }

    if (dragMode === "pan") {
      const { x: px, y: py } = canvasCoords(e, rigCanvas);
      const dx = px - dragPanStart.px;
      const dy = py - dragPanStart.py;
      rigView.offsetX = dragPanOffsetStart.x + dx;
      rigView.offsetY = dragPanOffsetStart.y + dy;
      drawRig();
      return;
    }

    if (dragMode === "select-rect") {
      const { wx, wy } = eventToWorld(e);
      window._rigSelectionRect = {
        x1: dragSelectStartWorld.x,
        y1: dragSelectStartWorld.y,
        x2: wx,
        y2: wy
      };

      const selRect = window._rigSelectionRect;
      const minX = Math.min(selRect.x1, selRect.x2);
      const maxX = Math.max(selRect.x1, selRect.x2);
      const minY = Math.min(selRect.y1, selRect.y2);
      const maxY = Math.max(selRect.y1, selRect.y2);

      const currentRectSet = new Set();
      for (const [id, dev] of Object.entries(rigDevices)) {
        if (
          dev.x >= minX && dev.x <= maxX &&
          dev.y >= minY && dev.y <= maxY
        ) {
          currentRectSet.add(id);
          if (!dragBaseSelectedSet.has(id) && !dragRectOrder.includes(id)) {
            dragRectOrder.push(id);
          }
        }
      }

      selectedDeviceSet = new Set(dragBaseSelectedSet);
      selectedDeviceOrder = [...dragBaseSelectedOrder];

      for (const id of dragRectOrder) {
        if (currentRectSet.has(id) && !selectedDeviceSet.has(id)) {
          selectedDeviceSet.add(id);
          selectedDeviceOrder.push(id);
        }
      }

      // devices qui sortent du rectangle doivent être retirés (sauf ceux de base)
      for (const id of [...selectedDeviceSet]) {
        if (dragBaseSelectedSet.has(id)) continue;
        if (!currentRectSet.has(id)) {
          selectedDeviceSet.delete(id);
          selectedDeviceOrder = selectedDeviceOrder.filter(d => d !== id);
        }
      }

      refreshControllerFromSelection();
      drawRig();
      return;
    }
  };

  rigCanvas.onmouseup = () => {
    if (!isMouseDown) return;
    isMouseDown = false;

    if (dragMode === "select-rect") {
      window._rigSelectionRect = null;
      dragBaseSelectedSet = null;
      dragBaseSelectedOrder = [];
      dragRectOrder = [];
      drawRig();
    }

    dragMode = null;
  };

  rigCanvas.onmouseleave = () => {
    if (!isMouseDown) return;
    isMouseDown = false;
    dragMode = null;
    window._rigSelectionRect = null;
    dragBaseSelectedSet = null;
    dragBaseSelectedOrder = [];
    dragRectOrder = [];
    drawRig();
  };

  rigCanvas.ondblclick = (e) => {
    const { wx, wy } = eventToWorld(e);
    const hitDev = hitTestDeviceWorld(wx, wy);
    if (hitDev) {
      editDeviceDialog(hitDev.id);
    }
  };
}
