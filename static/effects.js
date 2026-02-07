// static/effects.js
// Effets virtuels - RESPECTE LE VERROU DMX

///////////////////////
// ÉTAT
///////////////////////

let availableEffects = [];
let activeEffectAttr = "dimmer";
// NOTE: effectStartEpoch est défini dans core.js sur window.effectStartEpoch
let effectTickHandle = null;

///////////////////////
// GROUP INDEX HELPER
///////////////////////

// Get the group index for a device (for Vert ONE / Horiz ONE modes)
// Returns { idx: number, total: number } where:
//   - idx is the group index (devices in same group have same idx)
//   - total is the number of groups (not devices)
// If no groups, returns individual device index
function getDeviceGroupIndex(deviceId, deviceOrder) {
  const order = deviceOrder || [];
  const n = order.length || 1;
  const devIdStr = String(deviceId);

  // Check if there are selection groups (from Vert ONE / Horiz ONE)
  const selGroups = window.selectionGroups;

  if (selGroups && Array.isArray(selGroups) && selGroups.length > 0) {
    // Find which group this device belongs to
    for (let gi = 0; gi < selGroups.length; gi++) {
      if (selGroups[gi].map(String).includes(devIdStr)) {
        return { idx: gi, total: selGroups.length };
      }
    }
    // Device not found in any group - fallback to individual
    const idx = Math.max(0, order.map(String).indexOf(devIdStr));
    return { idx, total: n };
  }

  // No groups - each device is individual
  const idx = Math.max(0, order.map(String).indexOf(devIdStr));
  return { idx, total: n };
}

const EFFECT_ATTRS = [
  { key: "dimmer", label: "Dimmer" },
  { key: "r", label: "Color R" },
  { key: "g", label: "Color G" },
  { key: "b", label: "Color B" },
  { key: "pan", label: "Pan" },
  { key: "tilt", label: "Tilt" },
];

function getAttrLabel(key) {
  const entry = EFFECT_ATTRS.find(a => a.key === key);
  return entry?.label || key || "Attr";
}

///////////////////////
// LOAD EFFECTS
///////////////////////

async function ensureEffectsLoaded(forceReload = false) {
  if (availableEffects.length && !forceReload) return;

  try {
    const r = await fetch("/api/effects?t=" + Date.now()); // bypass cache
    const data = await r.json();
    const list = data.effects || data || [];
    availableEffects = Array.isArray(list) ? list : [];
    console.log("[FX] Loaded", availableEffects.length, "effects");
  } catch (e) {
    console.warn("Failed to load effects:", e);
    availableEffects = [];
  }
}

// Force reload effects definitions from server
async function reloadEffectsDefinitions() {
  await ensureEffectsLoaded(true);
  renderEffectsLibrary();
}

function renderEffectsLibrary() {
  const listEl = $id("effects-list");
  if (!listEl) return;
  listEl.innerHTML = "";
  
  if (!availableEffects.length) {
    listEl.innerHTML = "<div class='muted'>No effects loaded.</div>";
    return;
  }
  
  for (const eff of availableEffects) {
    const name = eff?.name || String(eff);
    const label = eff?.label || name;
    
    const item = document.createElement("div");
    item.className = "effects-item";
    item.textContent = label;
    item.title = "Double-click to apply";
    
    item.addEventListener("dblclick", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      applyEffectToSelection(name);
    });
    
    listEl.appendChild(item);
  }
}

///////////////////////
// VIRTUAL GROUPS
///////////////////////

function ensureVirtualGroupsRoot() {
  if (!cuesObj.virtual_groups || typeof cuesObj.virtual_groups !== "object") {
    cuesObj.virtual_groups = {};
  }
  virtualGroups = cuesObj.virtual_groups;
}

function allocVirtualGroupId() {
  let n = nextVirtualGroupId;
  while (virtualGroups[`vg${n}`]) n++;
  nextVirtualGroupId = n + 1;
  return `vg${n}`;
}

function rebuildVirtualGroupsFromCues() {
  ensureVirtualGroupsRoot();
  
  let maxNum = 0;
  for (const id of Object.keys(virtualGroups)) {
    const m = id.match(/^vg(\d+)$/i);
    if (m) {
      const num = parseInt(m[1], 10);
      if (Number.isFinite(num)) maxNum = Math.max(maxNum, num);
    }
  }
  nextVirtualGroupId = maxNum + 1;
  
  for (const id of Object.keys(rigDevices)) {
    if (!deviceCurrentGroups[id]) deviceCurrentGroups[id] = new Set();
  }
}

///////////////////////
// UI
///////////////////////

function isGroupSameAsSelection(group) {
  if (!selectedDeviceOrder.length) return false;
  if (!Array.isArray(group.deviceIds)) return false;
  if (group.deviceIds.length !== selectedDeviceOrder.length) return false;
  
  for (let i = 0; i < selectedDeviceOrder.length; i++) {
    if (String(selectedDeviceOrder[i]) !== String(group.deviceIds[i])) return false;
  }
  return true;
}

function renderEffectsTargets() {
  const container = $id("effects-targets");
  if (!container) return;
  container.innerHTML = "";
  
  if (!selectedDeviceOrder.length) {
    container.innerHTML = "<div class='muted'>Select devices in the rig to manage effects.</div>";
    return;
  }
  
  ensureVirtualGroupsRoot();
  
  for (const attr of EFFECT_ATTRS) {
    const row = document.createElement("div");
    row.className = "effects-attr-row";
    if (activeEffectAttr === attr.key) row.classList.add("active");
    
    const header = document.createElement("div");
    header.className = "effects-attr-header";
    header.onclick = () => {
      activeEffectAttr = attr.key;
      renderEffectsTargets();
      toast(`Attribut actif: ${attr.label}`, "info");
    };
    
    const title = document.createElement("div");
    title.textContent = attr.label;
    
    const groupsForAttr = Object.values(virtualGroups).filter(g => {
      if (g.attrKey !== attr.key) return false;
      if (!isGroupSameAsSelection(g)) return false;
      for (const devId of selectedDeviceOrder) {
        const set = deviceCurrentGroups[devId];
        if (!set || !set.has(g.id)) return false;
      }
      return true;
    });
    
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = groupsForAttr.length + " group(s)";
    
    header.appendChild(title);
    header.appendChild(badge);
    row.appendChild(header);
    
    if (activeEffectAttr === attr.key) {
      const body = document.createElement("div");
      
      if (!groupsForAttr.length) {
        const none = document.createElement("div");
        none.className = "muted";
        none.style.fontSize = "12px";
        none.textContent = "No effect group on this attribute for this selection.";
        body.appendChild(none);
      } else {
        groupsForAttr.forEach(group => {
          body.appendChild(buildEffectGroupCard(group));
        });
      }
      
      row.appendChild(body);
    }
    
    container.appendChild(row);
  }

  renderActualEffectsPanel();
}


function buildEffectGroupCard(group, options = {}) {
  const { subtitle, metaText, onRemove, removeLabel, extraActions } = options;
  const card = document.createElement("div");
  card.className = "effect-card";

  const title = document.createElement("div");
  title.textContent = group.type || "Effect";
  title.style.fontWeight = "600";
  card.appendChild(title);

  const sub = document.createElement("div");
  sub.className = "muted";
  sub.style.fontSize = "11px";
  const defaultSubtitle = `Group ${group.id} - ${getAttrLabel(group.attrKey)} - devices: ${(group.deviceIds || []).join(', ')}`;
  sub.textContent = subtitle || defaultSubtitle;
  card.appendChild(sub);

  if (metaText) {
    const meta = document.createElement("div");
    meta.className = "actual-effects-meta";
    meta.textContent = metaText;
    card.appendChild(meta);
  }

  // Get effect definition for dynamic params
  const def = getEffectDefinition(group.type);
  const params = def?.params || [];

  // Build UI for each parameter from definition
  for (const param of params) {
    const row = document.createElement("div");
    row.className = "effect-param-row";

    const lab = document.createElement("label");
    lab.textContent = param.label || param.key;
    if (param.hint) lab.title = param.hint;
    row.appendChild(lab);

    let input;

    if (param.type === "select" && Array.isArray(param.options)) {
      // Dropdown
      input = document.createElement("select");
      for (const opt of param.options) {
        const option = document.createElement("option");
        option.value = opt;
        option.textContent = opt;
        if (group[param.key] === opt) option.selected = true;
        input.appendChild(option);
      }
      input.onchange = () => { group[param.key] = input.value; };
    } else if (param.type === "range") {
      // Slider with number display
      const wrapper = document.createElement("div");
      wrapper.className = "range-wrapper";

      input = document.createElement("input");
      input.type = "range";
      input.min = param.min ?? 0;
      input.max = param.max ?? 100;
      input.step = param.step ?? 1;
      input.value = group[param.key] ?? param.default ?? 0;

      const valDisplay = document.createElement("span");
      valDisplay.className = "range-value";
      valDisplay.textContent = input.value;

      input.oninput = () => {
        const v = parseFloat(input.value);
        group[param.key] = v;
        valDisplay.textContent = v;
      };

      wrapper.appendChild(input);
      wrapper.appendChild(valDisplay);
      row.appendChild(wrapper);
      card.appendChild(row);
      continue;
    } else if (param.type === "number") {
      input = document.createElement("input");
      input.type = "number";
      input.min = param.min ?? 0;
      input.max = param.max ?? 99999;
      input.value = group[param.key] ?? param.default ?? 0;
      input.oninput = () => { group[param.key] = parseFloat(input.value) || 0; };
    } else {
      // Text input
      input = document.createElement("input");
      input.type = "text";
      input.value = group[param.key] != null ? String(group[param.key]) : "";
      input.placeholder = param.hint || "";
      input.oninput = () => { group[param.key] = input.value; };
    }

    row.appendChild(input);
    card.appendChild(row);
  }

  // Fallback: show any extra params not in definition
  const definedKeys = new Set(params.map(p => p.key));
  definedKeys.add("id"); definedKeys.add("attrKey"); definedKeys.add("type"); definedKeys.add("deviceIds");

  for (const [k, v] of Object.entries(group)) {
    if (definedKeys.has(k)) continue;
    const row = document.createElement("div");
    row.className = "effect-param-row";
    const lab = document.createElement("label");
    lab.textContent = k;
    const inp = document.createElement("input");
    inp.value = String(v);
    inp.oninput = () => { group[k] = typeof v === "number" ? (parseFloat(inp.value) || 0) : inp.value; };
    row.appendChild(lab);
    row.appendChild(inp);
    card.appendChild(row);
  }

  const actions = [];

  const rm = document.createElement("button");
  rm.className = "remove-btn";
  rm.textContent = removeLabel || "Remove";
  rm.onclick = () => {
    if (typeof onRemove === "function") onRemove(group);
    else removeEffectGroupFromCurrentSelection(group);
  };
  actions.push(rm);

  if (Array.isArray(extraActions)) {
    for (const action of extraActions) {
      if (!action || typeof action.handler !== "function") continue;
      const btn = document.createElement("button");
      btn.textContent = action.label || "Action";
      btn.className = action.className || "secondary";
      btn.onclick = () => action.handler(group);
      actions.push(btn);
    }
  }

  if (actions.length) {
    const row = document.createElement("div");
    row.className = "action-row";
    actions.forEach(btn => row.appendChild(btn));
    card.appendChild(row);
  }

  return card;
}

function removeEffectGroupFromCurrentSelection(group) {
  const ids = group.deviceIds || [];
  for (const id of ids) {
    if (!deviceCurrentGroups[id]) deviceCurrentGroups[id] = new Set();
    deviceCurrentGroups[id].delete(group.id);
  }
  toast(`Group ${group.id} removed.`, "info");
  renderEffectsTargets();
  renderActualEffectsPanel();
}

///////////////////////
// APPLY EFFECT
///////////////////////

async function applyEffectToSelection(effectName) {
  try {
    if (!selectedDeviceOrder.length) {
      toast("Sélectionne d'abord des devices.", "error");
      return;
    }

    const attrKey = activeEffectAttr || "dimmer";

    await ensureEffectsLoaded();
    ensureVirtualGroupsRoot();

    // Find effect definition
    const def = availableEffects.find(e => (e.name || e) === effectName) || {};
    const params = def.params || [];

    const groupId = allocVirtualGroupId();

    // Build group with all parameters from definition
    const group = {
      id: groupId,
      attrKey,
      type: effectName,
      deviceIds: [...selectedDeviceOrder].map(String),
    };

    // Apply default values from effect definition
    for (const param of params) {
      group[param.key] = param.default ?? 0;
    }

    virtualGroups[groupId] = group;
    cuesObj.virtual_groups = virtualGroups;

    for (const id of group.deviceIds) {
      if (!deviceCurrentGroups[id]) deviceCurrentGroups[id] = new Set();
      deviceCurrentGroups[id].add(groupId);
    }

    renderEffectsTargets();
    renderActualEffectsPanel();
    toast(`Effet ${effectName} ajouté (${groupId}) sur ${attrKey}`, "success");
  } catch (err) {
    console.error("[FX] error:", err);
    toast("Ajout d'effet échoué.", "error");
  }
}

// Get effect definition by name
function getEffectDefinition(effectName) {
  return availableEffects.find(e => (e.name || e) === effectName) || null;
}

///////////////////////
// ACTUAL EFFECTS PANEL
///////////////////////

function gatherActiveEffectGroups() {
  ensureVirtualGroupsRoot();
  const map = new Map();

  for (const [devId, groups] of Object.entries(deviceCurrentGroups || {})) {
    if (!groups) continue;
    const list = Array.isArray(groups) ? groups : Array.from(groups);
    for (const gid of list) {
      const group = virtualGroups[gid];
      if (!group) continue;
      if (!map.has(gid)) map.set(gid, { group, devices: new Set() });
      map.get(gid).devices.add(devId);
    }
  }

  return Array.from(map.values()).map(entry => ({
    group: entry.group,
    devices: Array.from(entry.devices).sort(),
  }));
}

function countGroupUsageInCues(groupId) {
  let count = 0;
  for (const step of cuesObj.sequence || []) {
    const dg = step.device_groups || {};
    for (const groups of Object.values(dg)) {
      if (Array.isArray(groups) && groups.includes(groupId)) {
        count++;
        break;
      }
    }
  }
  return count;
}

function isGroupUsedAnywhere(groupId) {
  if (gatherActiveEffectGroups().some(({ group }) => group.id === groupId)) return true;
  return countGroupUsageInCues(groupId) > 0;
}

function glowDevicesInRig(devices, durationMs = 5000) {
  const ids = (devices || []).map(String).filter(id => rigDevices[id]);
  if (!ids.length) {
    toast("Devices non trouvés dans le rig.", "warning");
    return;
  }
  if (typeof triggerRigGlow === "function") {
    triggerRigGlow(ids, durationMs);
  } else {
    toast("Glow rig indisponible.", "error");
  }
}

function disableGroupOnRig(groupId) {
  let changed = false;

  for (const [devId, setOrArr] of Object.entries(deviceCurrentGroups || {})) {
    if (!setOrArr) continue;
    if (setOrArr.delete) {
      if (setOrArr.delete(groupId)) changed = true;
    } else if (Array.isArray(setOrArr)) {
      const filtered = setOrArr.filter(g => g !== groupId);
      if (filtered.length !== setOrArr.length) {
        deviceCurrentGroups[devId] = new Set(filtered);
        changed = true;
      }
    }
  }

  if (changed) {
    renderEffectsTargets();
    renderActualEffectsPanel();
    if (typeof sendToEngineWithEffects === "function" && (window.playbackActive || window.dmxLocked)) {
      sendToEngineWithEffects(1.0);
    }
  }
}

function removeEffectGroupCompletely(groupId) {
  let removedFromCues = false;

  for (const step of cuesObj.sequence || []) {
    const dg = step.device_groups || {};
    for (const [devId, groups] of Object.entries(dg)) {
      if (!Array.isArray(groups)) continue;
      const filtered = groups.filter(g => g !== groupId);
      if (filtered.length !== groups.length) {
        removedFromCues = true;
        if (filtered.length) dg[devId] = filtered;
        else delete dg[devId];
      }
    }
  }

  let removedLive = false;
  for (const [devId, setOrArr] of Object.entries(deviceCurrentGroups || {})) {
    if (!setOrArr) continue;
    if (setOrArr.delete) {
      removedLive = setOrArr.delete(groupId) || removedLive;
    } else if (Array.isArray(setOrArr)) {
      const filtered = setOrArr.filter(g => g !== groupId);
      if (filtered.length !== setOrArr.length) {
        deviceCurrentGroups[devId] = new Set(filtered);
        removedLive = true;
      }
    }
  }

  if (!isGroupUsedAnywhere(groupId)) {
    delete virtualGroups[groupId];
    if (cuesObj.virtual_groups) delete cuesObj.virtual_groups[groupId];
  }
  cuesObj.virtual_groups = virtualGroups;

  renderEffectsTargets();
  renderActualEffectsPanel();
  if (typeof sendToEngineWithEffects === "function") {
    sendToEngineWithEffects(1.0);
  }

  if (removedFromCues || removedLive) {
    toast(`Effet ${groupId} supprimé.`, "info");
  }
}

function renderActualEffectsPanel() {
  const listEl = $id("actual-effects-list");
  if (!listEl) return;

  const active = gatherActiveEffectGroups();
  listEl.innerHTML = "";

  if (!active.length) {
    listEl.innerHTML = "<div class='muted'>Aucun effet actif.</div>";
    return;
  }

  active.sort((a, b) => {
    const aAttr = getAttrLabel(a.group.attrKey || "");
    const bAttr = getAttrLabel(b.group.attrKey || "");
    if (aAttr !== bAttr) return aAttr.localeCompare(bAttr);
    return (a.group.type || "").localeCompare(b.group.type || "");
  });

  for (const { group, devices } of active) {
    const usage = countGroupUsageInCues(group.id);
    const subtitle = `Attr: ${getAttrLabel(group.attrKey)} - Devices: ${devices.join(', ') || 'n/a'}`;
    const metaText = usage ? `Présent dans ${usage} cue(s)` : "Pas dans la cue chargée";

    const card = buildEffectGroupCard(group, {
      subtitle,
      metaText,
      removeLabel: "Glow device in Rig",
      onRemove: () => glowDevicesInRig(devices, 5000),
      extraActions: [
        {
          label: "Désactiver",
          handler: () => disableGroupOnRig(group.id),
          className: "secondary"
        }
      ],
    });

    listEl.appendChild(card);
  }
}

///////////////////////
// EFFECT CALCULATION
///////////////////////

/**
 * Parse phase field with spread patterns (same as cue fade patterns)
 * Supported formats:
 *  - "100"           -> fixed 100ms offset for all devices
 *  - "0 > 500"       -> spread 0-500ms in selection order
 *  - "0 < 500"       -> spread 0-500ms in reverse order
 *  - "0 | 500"       -> spread from edges to center
 *  - "0 || 500"      -> spread from center to edges
 *  - "0 ? 500"       -> random spread between 0-500ms
 */
function phaseOffsetForDevice(group, deviceId) {
  const ph = String(group.phase ?? "0").trim();
  const order = Array.isArray(group.deviceIds) ? group.deviceIds.map(String) : [];

  // Use group index if selection groups exist (Vert ONE / Horiz ONE)
  const groupInfo = getDeviceGroupIndex(deviceId, order);
  const idx = groupInfo.idx;
  const n = groupInfo.total;

  // Detect operator: ||, |, >, <, ?
  let op = null;
  let parts = [];

  if (ph.includes("||")) {
    op = "||";
    parts = ph.split("||").map(s => parseFloat(s.trim()) || 0);
  } else if (ph.includes("|")) {
    op = "|";
    parts = ph.split("|").map(s => parseFloat(s.trim()) || 0);
  } else if (ph.includes(">")) {
    op = ">";
    parts = ph.split(">").map(s => parseFloat(s.trim()) || 0);
  } else if (ph.includes("<")) {
    op = "<";
    parts = ph.split("<").map(s => parseFloat(s.trim()) || 0);
  } else if (ph.includes("?")) {
    op = "?";
    parts = ph.split("?").map(s => parseFloat(s.trim()) || 0);
  }

  if (!op || parts.length < 2) {
    // No pattern, just parse as number
    const x = parseFloat(ph);
    return Number.isFinite(x) ? x : 0;
  }

  const baseMs = parts[0] || 0;
  const spreadMs = parts[1] || 0;

  if (n <= 1) return baseMs;

  // Compute rank based on operator
  let rank;
  const denom = Math.max(n - 1, 1);

  switch (op) {
    case ">":
      // First to last in selection order
      rank = idx;
      break;
    case "<":
      // Last to first (reverse)
      rank = n - 1 - idx;
      break;
    case "|":
      // Edges to center
      rank = idx < n / 2 ? idx : (n - 1 - idx);
      break;
    case "||":
      // Center to edges
      rank = Math.abs(idx - Math.floor((n - 1) / 2));
      if (n % 2 === 0 && idx >= n / 2) rank = Math.abs(idx - Math.floor(n / 2));
      rank = Math.floor((n - 1) / 2) - Math.abs(idx - Math.floor((n - 1) / 2));
      rank = Math.max(0, rank);
      // Re-calculate for center-out
      const mid = (n - 1) / 2;
      const distFromCenter = Math.abs(idx - mid);
      const maxDist = mid;
      rank = maxDist > 0 ? Math.round((1 - distFromCenter / maxDist) * (n - 1)) : 0;
      break;
    case "?":
      // Random (but deterministic per device)
      // Use device index as seed for consistent random
      rank = Math.floor(((idx * 1234567) % (n * 100)) / 100 * n) % n;
      break;
    default:
      rank = idx;
  }

  return baseMs + (spreadMs * rank) / denom;
}

function triWave(x) { return x < 0.5 ? (x * 4 - 1) : (3 - x * 4); }
function sawWave(x) { return x * 2 - 1; }
function sqrWave(x) { return x < 0.5 ? 1 : -1; }

// Fade curves for chaser
function applyFadeCurve(t, curve) {
  curve = (curve || "linear").toLowerCase();
  if (curve === "linear") return t;
  if (curve === "easein") return t * t;
  if (curve === "easeout") return 1 - (1 - t) * (1 - t);
  if (curve === "easeinout") return t * t * (3 - 2 * t);
  if (curve === "snap") return t > 0.5 ? 1 : 0;
  if (curve === "smooth") return t * t * t * (t * (t * 6 - 15) + 10);
  return t;
}

// Random seeds for chaser random mode (per group)
const chaserRandomSeeds = {};

function evalChaserAdvanced(group, tMs, deviceId) {
  const order = group.deviceIds || [];
  const n = order.length;
  if (n === 0) return 0;

  // Use global group index helper (respects Vert ONE / Horiz ONE grouping)
  const groupInfo = getDeviceGroupIndex(deviceId, order);
  const idx = groupInfo.idx;
  const effectiveN = groupInfo.total;

  // Get parameters
  const fadeMs = Math.max(0, parseFloat(group.fade ?? 100));       // Fade time for IN and OUT each
  const duration = Math.max(0, parseFloat(group.duration ?? 0));   // Hold time at peak
  const fadeCurve = group.fadeCurve || "Linear";
  const size = Math.max(1, parseInt(group.size ?? 1));             // Nb groups ON at same time
  const stepSize = Math.max(1, parseInt(group.stepSize ?? 1));     // How many positions to advance per step
  const breakStep = parseInt(group.breakStep ?? 0);
  const breakSize = Math.max(0, parseFloat(group.breakSize ?? 500));
  const playMode = (group.playMode || "Normal").toLowerCase();

  // DEBUG - log every 2 seconds for first device only
  if (idx === 0 && (!window._lastChaserLog || tMs - window._lastChaserLog > 2000)) {
    window._lastChaserLog = tMs;
    console.log('[CHASER]', {
      effectiveN, size, stepSize, fadeMs, duration,
      numSteps: (Math.min(size, effectiveN) >= effectiveN) ? 1 : Math.max(1, Math.ceil((effectiveN - Math.min(size, effectiveN) + 1) / stepSize))
    });
  }

  // Step timing: fade_in + hold + fade_out
  const stepDuration = fadeMs + duration + fadeMs;
  if (stepDuration <= 0) return 0;

  // Number of steps in one full cycle
  // Without wrapping: numSteps = how many steps until the "window" has passed all devices
  // The window starts at 0 and ends when startPos + size > effectiveN
  // So numSteps = max(1, ceil((effectiveN - size + 1) / stepSize)) when size < effectiveN
  // If size >= effectiveN, all devices are always lit, so numSteps = 1
  const effectiveSize = Math.min(size, effectiveN);
  const numSteps = effectiveSize >= effectiveN
    ? 1
    : Math.max(1, Math.ceil((effectiveN - effectiveSize + 1) / stepSize));

  // Total cycle = all steps + breaks
  const breaksCount = breakStep > 0 ? Math.floor((numSteps - 1) / breakStep) : 0;
  const totalBreakTime = breaksCount * breakSize;
  const cycleDuration = (numSteps * stepDuration) + totalBreakTime;

  // Current position in cycle
  const cycleTime = tMs % cycleDuration;

  // Find current step number (accounting for breaks)
  let currentStep = 0;
  let accTime = 0;
  for (let s = 0; s < numSteps; s++) {
    const nextTime = accTime + stepDuration;
    if (cycleTime < nextTime) {
      currentStep = s;
      break;
    }
    accTime = nextTime;
    // Add break time after every breakStep steps
    if (breakStep > 0 && (s + 1) % breakStep === 0) {
      if (cycleTime < accTime + breakSize) {
        // We're in a break - nothing is lit
        return 0;
      }
      accTime += breakSize;
    }
    currentStep = s + 1;
  }
  currentStep = Math.min(currentStep, numSteps - 1);

  // Apply play mode to get effective step
  let effectiveStep = currentStep;
  if (playMode === "reverse") {
    effectiveStep = numSteps - 1 - currentStep;
  } else if (playMode === "bounce") {
    const cycleNum = Math.floor(tMs / cycleDuration) % 2;
    if (cycleNum === 1) effectiveStep = numSteps - 1 - currentStep;
  } else if (playMode === "in") {
    // From edges toward center
    const half = Math.ceil(numSteps / 2);
    if (currentStep < half) {
      effectiveStep = currentStep; // Left side
    } else {
      effectiveStep = numSteps - 1 - (currentStep - half); // Right side
    }
  } else if (playMode === "out") {
    // From center toward edges
    const half = Math.ceil(numSteps / 2);
    const center = Math.floor(numSteps / 2);
    if (currentStep < half) {
      effectiveStep = center - currentStep;
    } else {
      effectiveStep = center + (currentStep - half + 1);
    }
    effectiveStep = Math.max(0, Math.min(effectiveStep, numSteps - 1));
  } else if (playMode === "inout") {
    const quarter = Math.ceil(numSteps / 4);
    const phase = Math.floor(currentStep / quarter) % 4;
    const posInPhase = currentStep % quarter;
    if (phase === 0) effectiveStep = posInPhase;
    else if (phase === 1) effectiveStep = numSteps - 1 - posInPhase;
    else if (phase === 2) effectiveStep = numSteps - 1 - posInPhase;
    else effectiveStep = posInPhase;
  } else if (playMode === "random") {
    const gid = group.id || "default";
    if (!chaserRandomSeeds[gid] || chaserRandomSeeds[gid].length !== numSteps) {
      chaserRandomSeeds[gid] = [];
      for (let i = 0; i < numSteps; i++) chaserRandomSeeds[gid].push(i);
      for (let i = numSteps - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [chaserRandomSeeds[gid][i], chaserRandomSeeds[gid][j]] = [chaserRandomSeeds[gid][j], chaserRandomSeeds[gid][i]];
      }
    }
    effectiveStep = chaserRandomSeeds[gid][currentStep % numSteps];
  } else if (playMode === "switch") {
    effectiveStep = currentStep % 2 === 0 ? Math.floor(currentStep / 2) : numSteps - 1 - Math.floor(currentStep / 2);
  }

  // Calculate which device positions are lit for this step
  // Starting position = effectiveStep * stepSize (NO wrapping)
  // Lit devices = positions from startPos to startPos + effectiveSize - 1
  const startPos = effectiveStep * stepSize;

  // Check if this device (idx) is one of the lit devices (NO wrapping)
  // Device is lit if idx is in range [startPos, startPos + effectiveSize)
  const isLit = (idx >= startPos && idx < startPos + effectiveSize);

  if (!isLit) return 0;

  // Calculate time within current step
  let stepStartTime = 0;
  for (let s = 0; s < currentStep; s++) {
    stepStartTime += stepDuration;
    if (breakStep > 0 && (s + 1) % breakStep === 0) {
      stepStartTime += breakSize;
    }
  }
  const timeInStep = cycleTime - stepStartTime;

  // Phase 1: Fade In (0 to fadeMs) - returns 0 to 1
  if (timeInStep < fadeMs) {
    if (fadeMs <= 0) return 1;
    const t = timeInStep / fadeMs;
    return applyFadeCurve(t, fadeCurve);
  }

  // Phase 2: Hold at peak (fadeMs to fadeMs+duration) - returns 1
  if (timeInStep < fadeMs + duration) {
    return 1;
  }

  // Phase 3: Fade Out (fadeMs+duration to fadeMs+duration+fadeMs) - returns 1 to 0
  const fadeOutEnd = fadeMs + duration + fadeMs;
  if (timeInStep < fadeOutEnd) {
    if (fadeMs <= 0) return 0;
    const fadeOutTime = timeInStep - fadeMs - duration;
    const t = fadeOutTime / fadeMs;
    return 1 - applyFadeCurve(t, fadeCurve);
  }

  // Past step duration - device is off
  return 0;
}

function evalGroupEffect(group, tMs, deviceId) {
  const ampPct = clamp(parseFloat(group.amplitude ?? 0), -255, 255);
  const type = String(group.type || "").toLowerCase();

  let y = 0;

  // Chaser uses duration-based timing, not frequency
  if (type === "chaser") {
    // Chaser returns 0-1 (off to on)
    y = evalChaserAdvanced(group, tMs, deviceId);
    // For chaser: amplitude directly controls the output
    return ampPct * y;
  } else {
    // Other effects use frequency-based timing
    const freq = Math.max(0, parseFloat(group.frequency ?? 0));
    const phMs = phaseOffsetForDevice(group, deviceId);

    const w = freq <= 0 ? 0 : ((tMs + phMs) / 1000) * freq;
    const frac = w - Math.floor(w);

    if (type === "sinus" || type === "cardinalsinus") {
      y = Math.sin(2 * Math.PI * frac);
    } else if (type === "triangle") {
      y = triWave(frac);
    } else if (type === "sawtooth") {
      y = sawWave(frac);
    } else if (type === "rectangle" || type === "trapezoid") {
      y = sqrWave(frac);
    } else if (type === "bump") {
      y = frac < 0.1 ? (1 - frac / 0.1) : 0;
    } else {
      y = Math.sin(2 * Math.PI * frac);
    }

    // Other effects: -1 to 1 range, scaled by amplitude
    const delta = (ampPct / 100) * 127.5;
    return delta * y;
  }
}

///////////////////////
// RUNNER (AVEC DOUBLE PROTECTION)
///////////////////////

async function effectTick() {
  // ========================================
  // PROTECTION 1 : Pendant playback
  // ========================================
  if (playbackActive) {
    return;
  }
  
  // ========================================
  // PROTECTION 2 : Pendant transition (verrou DMX)
  // ========================================
  if (window.dmxLocked) {
    console.log('[FX] Blocked by DMX lock');
    return;
  }
  
  // ========================================
  // PROTECTION 3 : Pas de devices configurés
  // ========================================
  if (Object.keys(rigDevices).length === 0) {
    return;
  }
  
  const tMs = performance.now() - window.effectStartEpoch;
  const perUniverseMap = {};
  
  devicePreviewRGB = {};
  devicePreviewDimmer = {};
  
  ensureVirtualGroupsRoot();
  
  for (const dev of Object.values(rigDevices)) {
    const fi = fixtures[dev.fixture] || {};
    const funcs = fi.functions || {};
    const absMap = getDeviceAttrAbsChannels(dev);
    const lv = deviceLocalValues[dev.id] || {};
    const devGroups = Array.from(deviceCurrentGroups[dev.id] || []);
    
    const u = dev.universe || 0;
    perUniverseMap[u] ||= {};
    
    // Base
    const addrCount = fi.addr_count || 1;
    for (let li = 0; li < addrCount; li++) {
      const absCh = dev.address + li;
      perUniverseMap[u][absCh] = lv[li] ?? 0;
    }
    
    // Effets
    for (const gId of devGroups) {
      const group = virtualGroups[gId];
      if (!group) continue;
      
      const attr = group.attrKey;
      const absCh = absMap[attr];
      if (absCh == null) continue;
      
      const base = perUniverseMap[u][absCh] ?? 0;
      const delta = evalGroupEffect(group, tMs, dev.id);
      const val = clamp(Math.round(base + delta), 0, 255);
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
  
  // Envoyer (seulement si des données)
  for (const [uStr, chMap] of Object.entries(perUniverseMap)) {
    if (Object.keys(chMap).length === 0) continue;
    const u = parseInt(uStr, 10) || 0;
    await applyUniverseState(u, chMap, false, "ui_effect");
  }
  
  drawRig();
  syncRgbWidgetFromFirstDevice();
  syncPosWidgetFromFirstDevice();
}

function startEffectRunner() {
  if (effectTickHandle) return;
  window.effectStartEpoch = performance.now();
  effectTickHandle = setInterval(effectTick, 20);
}

function stopEffectRunner() {
  if (!effectTickHandle) return;
  clearInterval(effectTickHandle);
  effectTickHandle = null;
}
