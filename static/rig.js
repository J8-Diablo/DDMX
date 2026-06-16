// static/rig.js
// Gestion du rig : devices, canvas pan/zoom, multi-select, controller (sliders, color, position)


// Shared drag handler — a single pair of window listeners dispatches to the
// currently-active widget instead of every widget instance registering its own
// pair on every render (which leaked listeners on each refreshControllerFromSelection).
let _activeDragHandler = null;
window.addEventListener("mousemove", (e) => { if (_activeDragHandler) _activeDragHandler(e); });
window.addEventListener("mouseup", () => { _activeDragHandler = null; });

// Initialize default values for a device (RGB=white, dimmer=255)
function initDeviceDefaults(deviceId, fixtureName) {
  const fi = fixtures[fixtureName];
  if (!fi) return;
  deviceLocalValues[deviceId] = deviceLocalValues[deviceId] || {};
  const groups = getFixtureGroups(fi);
  if (groups.length) {
    groups.forEach(group => {
      getGroupChannels(group).forEach(channel => {
        const offset = parseInt(channel?.offset ?? -1, 10);
        if (!Number.isFinite(offset) || offset < 0) return;
        deviceLocalValues[deviceId][offset] = clamp(parseInt(channel?.default ?? 0, 10) || 0, 0, 255);
      });
    });
    return;
  }

  const funcs = fi.functions || {};
  if (funcs.rgb) {
    if (funcs.rgb.red != null) deviceLocalValues[deviceId][funcs.rgb.red] = 255;
    if (funcs.rgb.green != null) deviceLocalValues[deviceId][funcs.rgb.green] = 255;
    if (funcs.rgb.blue != null) deviceLocalValues[deviceId][funcs.rgb.blue] = 255;
  }
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
let rigSyncTimer = null;
let lastRigPayload = "";

const DUMMY_MIN_CHANNELS = 13;

function buildMovementChannelsByUniverse() {
  const map = {};

  for (const dev of Object.values(rigDevices)) {
    if (!dev) continue;
    const fi = fixtures[dev.fixture] || {};
    const u = parseInt(dev.universe, 10) || 0;
    map[u] ||= new Set();
    const groups = getFixtureGroups(fi, "position");
    groups.forEach(group => {
      ["pan", "tilt"].forEach(role => {
        const channel = getGroupChannel(group, role);
        if (!channel) return;
        const abs = dev.address + (parseInt(channel.offset ?? 0, 10) || 0);
        if (Number.isFinite(abs)) map[u].add(abs);
      });
    });
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
    const addrCount = getFixtureFootprint(fi);
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

function buildRigRegisterPayload() {
  const devices = [];
  for (const dev of Object.values(rigDevices)) {
    if (!dev) continue;
    const attrMap = (typeof getDeviceAttrAbsChannels === "function")
      ? getDeviceAttrAbsChannels(dev)
      : {};
    devices.push({
      device_id: String(dev.id),
      universe: parseInt(dev.universe, 10) || 0,
      attr_map: attrMap || {},
      address: dev.address ?? 0,
      x: Number.isFinite(Number(dev.x)) ? Number(dev.x) : null,
      y: Number.isFinite(Number(dev.y)) ? Number(dev.y) : null,
      fixture: String(dev.fixture || ""),
      cname: String(dev.cname || ""),
      home_pan: dev.home_pan ?? null,
      home_tilt: dev.home_tilt ?? null,
      invert_pan: !!dev.invert_pan,
      invert_tilt: !!dev.invert_tilt,
    });
  }
  // The UI always pushes the COMPLETE rig, so request replace semantics:
  // the backend prunes any device no longer present (no ghost fixtures).
  return { devices, replace: true };
}

async function syncRigToBackend(force = false) {
  // Always push the rig to the backend: even in UI render mode AutoLight
  // needs the device list to know which channels to drive.
  const payload = buildRigRegisterPayload();
  const body = JSON.stringify(payload);
  if (!force && body === lastRigPayload) return;
  lastRigPayload = body;
  try {
    await fetch("/api/rig/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
    });
  } catch (e) {
    console.warn("[DMX] rig register failed:", e);
    if (typeof window.fallbackToUiMode === "function") {
      window.fallbackToUiMode("Backend unavailable, fallback to UI render mode.");
    }
  }
}

function scheduleRigSync() {
  if (rigSyncTimer) return;
  rigSyncTimer = setTimeout(() => {
    rigSyncTimer = null;
    syncRigToBackend();
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
    const c = getFixtureFootprint(fi);
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

function findNextFreeAddressExcludingDevice(universe, addrCount, excludedDeviceId = null) {
  const ranges = [];
  const targetUniverse = parseInt(universe, 10) || 0;

  for (const d of Object.values(rigDevices)) {
    if (!d) continue;
    if (excludedDeviceId != null && String(d.id) === String(excludedDeviceId)) continue;
    if ((parseInt(d.universe, 10) || 0) !== targetUniverse) continue;
    const fi = fixtures[d.fixture] || {};
    const c = getFixtureFootprint(fi);
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

function findAutoRemapSlot(fixtureName, preferredUniverse, excludedDeviceId = null) {
  const fi = fixtures?.[fixtureName] || {};
  const footprint = getFixtureFootprint(fi);
  const preferred = clamp(parseInt(preferredUniverse, 10) || 0, 0, 9999);

  const currentAddr = findNextFreeAddressExcludingDevice(preferred, footprint, excludedDeviceId);
  if (currentAddr != null) {
    return { universe: preferred, address: currentAddr, footprint };
  }

  for (let universe = 0; universe <= 9999; universe++) {
    if (universe === preferred) continue;
    const address = findNextFreeAddressExcludingDevice(universe, footprint, excludedDeviceId);
    if (address != null) {
      return { universe, address, footprint };
    }
  }
  return null;
}

const FIXTURE_REMAP_ALIAS_SPECS = {
  dimmer: { family: "dimmer", role: "level", label: "Primary Dimmer" },
  r: { family: "color", role: "red", label: "Primary Red" },
  g: { family: "color", role: "green", label: "Primary Green" },
  b: { family: "color", role: "blue", label: "Primary Blue" },
  pan: { family: "position", role: "pan", label: "Primary Pan" },
  tilt: { family: "position", role: "tilt", label: "Primary Tilt" },
};

function buildFixtureRemapMeta(fixtureName) {
  const fi = fixtures?.[fixtureName] || {};
  const exactDefs = Object.values(getFixtureAttrDefinitions(fi, { includeLegacy: false }));
  const legacyDefs = getFixtureAttrDefinitions(fi, { includeLegacy: true });
  const exactByKey = {};
  const exactByOffset = {};
  const legacyByKey = {};
  const supportedDeviceKeys = new Set();
  const elementTargets = {};

  exactDefs.forEach((def) => {
    exactByKey[def.key] = def;
    exactByOffset[parseInt(def.offset ?? 0, 10) || 0] = def;
  });

  Object.entries(legacyDefs).forEach(([key, def]) => {
    legacyByKey[key] = def;
    supportedDeviceKeys.add(key);
  });

  getFixtureElementDefs(fi).forEach((element) => {
    Object.entries(element?.targets || {}).forEach(([key, target]) => {
      const offset = parseInt(target?.offset ?? 0, 10) || 0;
      const exact = exactByOffset[offset] || null;
      elementTargets[key] ||= [];
      elementTargets[key].push({
        offset,
        exactKey: exact?.key || "",
      });
    });
  });

  return {
    fixtureName,
    fi,
    footprint: getFixtureFootprint(fi),
    exactDefs,
    exactByKey,
    exactByOffset,
    legacyByKey,
    supportedDeviceKeys,
    elementTargets,
    profileCache: {},
  };
}

function buildRemapSourceLabel(profile) {
  if (!profile) return "Unknown channel";
  if (profile.label) return profile.label;
  if (profile.sourceKey && profile.sourceKey.startsWith("offset.")) {
    const offset = parseInt(profile.sourceKey.split(".")[1], 10) || 0;
    return `Channel offset ${offset}`;
  }
  return profile.sourceKey || "Unknown channel";
}

function getFixtureSourceProfile(meta, sourceKey) {
  const cacheKey = String(sourceKey || "");
  if (meta.profileCache[cacheKey]) return meta.profileCache[cacheKey];

  const exact = meta.exactByKey[cacheKey];
  if (exact) {
    const profile = {
      sourceKey: cacheKey,
      exactKey: exact.key,
      family: String(exact.family || ""),
      role: String(exact.role || ""),
      kind: String(exact.kind || ""),
      label: String(exact.label || cacheKey),
      offset: parseInt(exact.offset ?? 0, 10) || 0,
    };
    meta.profileCache[cacheKey] = profile;
    return profile;
  }

  const legacy = meta.legacyByKey[cacheKey];
  if (legacy) {
    const exactByOffset = meta.exactByOffset[parseInt(legacy.offset ?? 0, 10) || 0] || null;
    const profile = {
      sourceKey: cacheKey,
      exactKey: exactByOffset?.key || "",
      family: String(exactByOffset?.family || legacy.family || ""),
      role: String(exactByOffset?.role || legacy.role || ""),
      kind: String(exactByOffset?.kind || legacy.kind || ""),
      label: String(legacy.label || exactByOffset?.label || cacheKey),
      offset: exactByOffset ? (parseInt(exactByOffset.offset ?? 0, 10) || 0) : (parseInt(legacy.offset ?? 0, 10) || 0),
    };
    meta.profileCache[cacheKey] = profile;
    return profile;
  }

  const elementEntries = meta.elementTargets[cacheKey] || [];
  const uniqueExactKeys = Array.from(new Set(
    elementEntries.map((entry) => String(entry?.exactKey || "")).filter(Boolean)
  ));
  if (uniqueExactKeys.length === 1 && meta.exactByKey[uniqueExactKeys[0]]) {
    const resolved = meta.exactByKey[uniqueExactKeys[0]];
    const profile = {
      sourceKey: cacheKey,
      exactKey: resolved.key,
      family: String(resolved.family || ""),
      role: String(resolved.role || ""),
      kind: String(resolved.kind || ""),
      label: String(resolved.label || cacheKey),
      offset: parseInt(resolved.offset ?? 0, 10) || 0,
    };
    meta.profileCache[cacheKey] = profile;
    return profile;
  }

  const sharedSpec = getSharedFixtureTargetSpec(cacheKey) || FIXTURE_REMAP_ALIAS_SPECS[cacheKey] || null;
  if (sharedSpec) {
    const profile = {
      sourceKey: cacheKey,
      exactKey: "",
      family: String(sharedSpec.family || ""),
      role: String(sharedSpec.role || ""),
      kind: "",
      label: String(sharedSpec.label || humanizeFixtureToken(cacheKey)),
      offset: 0,
    };
    meta.profileCache[cacheKey] = profile;
    return profile;
  }

  if (cacheKey.startsWith("offset.")) {
    const offset = parseInt(cacheKey.split(".")[1], 10) || 0;
    const fallback = meta.exactByOffset[offset] || null;
    const profile = {
      sourceKey: cacheKey,
      exactKey: fallback?.key || "",
      family: String(fallback?.family || ""),
      role: String(fallback?.role || ""),
      kind: String(fallback?.kind || ""),
      label: fallback?.label ? `${fallback.label} (offset ${offset})` : `Offset ${offset}`,
      offset,
    };
    meta.profileCache[cacheKey] = profile;
    return profile;
  }

  const profile = {
    sourceKey: cacheKey,
    exactKey: "",
    family: "",
    role: "",
    kind: "",
    label: humanizeFixtureToken(cacheKey),
    offset: 0,
  };
  meta.profileCache[cacheKey] = profile;
  return profile;
}

function fixtureSupportsTarget(meta, sourceKey, scope = "devices") {
  const key = String(sourceKey || "");
  if (!key) return false;
  if (scope === "fixture_elements") {
    return (
      (Array.isArray(meta.elementTargets[key]) && meta.elementTargets[key].length > 0) ||
      meta.supportedDeviceKeys.has(key)
    );
  }
  return meta.supportedDeviceKeys.has(key);
}

function rankRemapCandidate(def, profile) {
  let score = 0;
  if (!def || !profile) return score;
  if (def.key === profile.exactKey) score += 1000;
  if (String(def.family || "") === String(profile.family || "")) score += 200;
  if (String(def.role || "") === String(profile.role || "")) score += 120;
  if (String(def.kind || "") === String(profile.kind || "")) score += 80;
  return score;
}

function listManualRemapOptions(meta, sourceKey) {
  const profile = getFixtureSourceProfile(meta, sourceKey);
  return [...meta.exactDefs]
    .sort((left, right) => {
      const delta = rankRemapCandidate(right, profile) - rankRemapCandidate(left, profile);
      if (delta !== 0) return delta;
      return String(left.label || left.key).localeCompare(String(right.label || right.key));
    })
    .map((def) => ({
      value: def.key,
      label: `${def.label} (${def.key}, ch ${parseInt(def.offset ?? 0, 10) + 1})`,
    }));
}

function autoResolveRemapTarget(sourceKey, oldMeta, newMeta) {
  const profile = getFixtureSourceProfile(oldMeta, sourceKey);

  if (profile.exactKey && newMeta.exactByKey[profile.exactKey]) {
    return {
      key: profile.exactKey,
      reason: "exact",
    };
  }

  if (sourceKey.startsWith("offset.")) {
    const offset = parseInt(sourceKey.split(".")[1], 10) || 0;
    const sameOffset = newMeta.exactByOffset[offset] || null;
    if (sameOffset) {
      return {
        key: sameOffset.key,
        reason: "offset",
      };
    }
  }

  const sameKindRole = newMeta.exactDefs.filter((def) =>
    String(def.family || "") === String(profile.family || "") &&
    String(def.role || "") === String(profile.role || "") &&
    String(def.kind || "") === String(profile.kind || "")
  );
  if (sameKindRole.length === 1) {
    return {
      key: sameKindRole[0].key,
      reason: "family-kind-role",
    };
  }

  const sameFamilyRole = newMeta.exactDefs.filter((def) =>
    String(def.family || "") === String(profile.family || "") &&
    String(def.role || "") === String(profile.role || "")
  );
  if (sameFamilyRole.length === 1) {
    return {
      key: sameFamilyRole[0].key,
      reason: "family-role",
    };
  }

  const sameFamily = newMeta.exactDefs.filter((def) =>
    profile.family && String(def.family || "") === String(profile.family || "")
  );
  if (sameFamily.length === 1) {
    return {
      key: sameFamily[0].key,
      reason: "family",
    };
  }

  return null;
}

function collectCueChannelSourceUsage(deviceId, address, footprint, oldMeta) {
  const usage = {};

  for (const step of (cuesObj.sequence || [])) {
    const entry = step?.devices?.[deviceId];
    if (!entry || typeof entry !== "object") continue;
    const channels = entry.channels || {};

    Object.entries(channels).forEach(([rawKey]) => {
      if (rawKey === "Universe" || !/^\d+$/.test(rawKey)) return;
      const absCh = parseInt(rawKey, 10);
      if (!Number.isFinite(absCh)) return;
      const offset = absCh - address;
      if (offset < 0 || offset >= footprint) return;
      const exact = oldMeta.exactByOffset[offset] || null;
      const sourceKey = exact?.key || `offset.${offset}`;
      if (!usage[sourceKey]) {
        const profile = getFixtureSourceProfile(oldMeta, sourceKey);
        usage[sourceKey] = {
          sourceKey,
          label: buildRemapSourceLabel(profile),
          helpText: `Cue channel at DMX ${absCh}`,
          kind: "channel",
          offset,
        };
      }
    });
  }

  return usage;
}

function collectCueGroupSourceUsage(deviceId, oldMeta, newMeta) {
  const usage = {};
  const seenGroups = new Set();

  for (const step of (cuesObj.sequence || [])) {
    const groups = step?.device_groups?.[deviceId];
    if (!Array.isArray(groups)) continue;

    for (const gid of groups) {
      const groupId = String(gid || "");
      if (!groupId || seenGroups.has(groupId)) continue;
      seenGroups.add(groupId);

      const group = virtualGroups[groupId];
      if (!group) continue;

      const sourceKey = getGroupTargetKey(group);
      if (!sourceKey) continue;
      const scope = getGroupSelectionScope(group);
      if (fixtureSupportsTarget(newMeta, sourceKey, scope)) continue;

      const profile = getFixtureSourceProfile(oldMeta, sourceKey);
      usage[sourceKey] ||= {
        sourceKey,
        label: `${buildRemapSourceLabel(profile)} effect target`,
        helpText: `Effect target used by group ${groupId}`,
        kind: "group",
        groupIds: [],
      };
      usage[sourceKey].groupIds.push(groupId);
    }
  }

  return usage;
}

function buildFixtureRemapPlan(deviceId, oldFixtureName, newFixtureName, oldUniverse, oldAddress, newUniverse, newAddress) {
  const oldMeta = buildFixtureRemapMeta(oldFixtureName);
  const newMeta = buildFixtureRemapMeta(newFixtureName);
  const channelUsage = collectCueChannelSourceUsage(deviceId, oldAddress, oldMeta.footprint, oldMeta);
  const groupUsage = collectCueGroupSourceUsage(deviceId, oldMeta, newMeta);
  const mergedUsage = { ...channelUsage };

  Object.entries(groupUsage).forEach(([sourceKey, entry]) => {
    if (!mergedUsage[sourceKey]) {
      mergedUsage[sourceKey] = entry;
      return;
    }
    mergedUsage[sourceKey].kind = mergedUsage[sourceKey].kind === "channel" ? "channel+group" : mergedUsage[sourceKey].kind;
    mergedUsage[sourceKey].groupIds = Array.from(new Set([
      ...(mergedUsage[sourceKey].groupIds || []),
      ...(entry.groupIds || []),
    ]));
    if (!mergedUsage[sourceKey].helpText && entry.helpText) {
      mergedUsage[sourceKey].helpText = entry.helpText;
    }
  });

  const autoResolutions = [];
  const manualResolutions = [];
  const sourceToNewKey = {};

  Object.values(mergedUsage).forEach((entry) => {
    const sourceKey = entry.sourceKey;
    const auto = autoResolveRemapTarget(sourceKey, oldMeta, newMeta);
    const profile = getFixtureSourceProfile(oldMeta, sourceKey);

    if (auto?.key && newMeta.exactByKey[auto.key]) {
      sourceToNewKey[sourceKey] = auto.key;
      const newDef = newMeta.exactByKey[auto.key];
      const oldDef = profile.exactKey ? oldMeta.exactByKey[profile.exactKey] : null;
      const detail = oldDef
        ? `${oldDef.label} (${oldAddress + (parseInt(oldDef.offset ?? 0, 10) || 0)} / U${oldUniverse}) -> ${newDef.label} (${newAddress + (parseInt(newDef.offset ?? 0, 10) || 0)} / U${newUniverse})`
        : `${sourceKey} -> ${newDef.label}`;
      autoResolutions.push({
        sourceKey,
        label: entry.label,
        detail,
      });
      return;
    }

    manualResolutions.push({
      sourceKey,
      label: entry.label,
      helpText: entry.helpText || "",
      options: listManualRemapOptions(newMeta, sourceKey),
    });
  });

  return {
    oldMeta,
    newMeta,
    channelUsage,
    groupUsage,
    sourceToNewKey,
    autoResolutions,
    manualResolutions,
  };
}

function applyCueChannelRemapForDevice(deviceId, oldAddress, oldFootprint, newUniverse, newAddress, oldMeta, newMeta, sourceToNewKey) {
  for (const step of (cuesObj.sequence || [])) {
    const entry = step?.devices?.[deviceId];
    if (!entry || typeof entry !== "object") continue;

    const previous = entry.channels || {};
    const nextChannels = { Universe: newUniverse };

    Object.entries(previous).forEach(([rawKey, value]) => {
      if (rawKey === "Universe") return;
      if (!/^\d+$/.test(rawKey)) {
        nextChannels[rawKey] = value;
        return;
      }

      const absCh = parseInt(rawKey, 10);
      if (!Number.isFinite(absCh)) return;
      const offset = absCh - oldAddress;

      if (offset < 0 || offset >= oldFootprint) {
        nextChannels[rawKey] = value;
        return;
      }

      const exact = oldMeta.exactByOffset[offset] || null;
      const sourceKey = exact?.key || `offset.${offset}`;
      const targetKey = String(sourceToNewKey[sourceKey] || "").trim();
      const newDef = newMeta.exactByKey[targetKey];
      if (!newDef) return;

      const newAbs = newAddress + (parseInt(newDef.offset ?? 0, 10) || 0);
      nextChannels[String(newAbs)] = value;
    });

    entry.channels = nextChannels;
  }
}

function cloneRemappedGroup(groupId, targetKey) {
  const original = virtualGroups[groupId];
  if (!original) return groupId;

  const newGid = allocVirtualGroupId();
  const clone = JSON.parse(JSON.stringify(original));
  clone.id = newGid;

  if ("targetKey" in clone || getGroupSelectionScope(clone) === "fixture_elements") {
    clone.targetKey = targetKey;
  }
  if ("attrKey" in clone || !("targetKey" in clone)) {
    clone.attrKey = targetKey;
  }

  virtualGroups[newGid] = clone;
  cuesObj.virtual_groups = virtualGroups;
  return newGid;
}

function applyCueGroupRemapForDevice(deviceId, oldMeta, newMeta, sourceToNewKey) {
  const replacementByGroup = {};

  const ensureReplacement = (groupId) => {
    if (replacementByGroup[groupId]) return replacementByGroup[groupId];

    const group = virtualGroups[groupId];
    if (!group) {
      replacementByGroup[groupId] = groupId;
      return groupId;
    }

    const sourceKey = getGroupTargetKey(group);
    const scope = getGroupSelectionScope(group);
    if (!sourceKey || fixtureSupportsTarget(newMeta, sourceKey, scope)) {
      replacementByGroup[groupId] = groupId;
      return groupId;
    }

    const newTargetKey = String(sourceToNewKey[sourceKey] || "").trim();
    if (!newTargetKey || newTargetKey === sourceKey) {
      replacementByGroup[groupId] = groupId;
      return groupId;
    }

    replacementByGroup[groupId] = cloneRemappedGroup(groupId, newTargetKey);
    return replacementByGroup[groupId];
  };

  for (const step of (cuesObj.sequence || [])) {
    const groups = step?.device_groups?.[deviceId];
    if (!Array.isArray(groups) || !groups.length) continue;
    step.device_groups[deviceId] = groups.map((gid) => ensureReplacement(String(gid || "")));
  }

  const currentGroups = Array.from(deviceCurrentGroups[deviceId] || []);
  if (currentGroups.length) {
    deviceCurrentGroups[deviceId] = new Set(currentGroups.map((gid) => ensureReplacement(String(gid || ""))));
  }
}

function buildFixtureRangeSummary(universe, address, fixtureName) {
  const range = getAddressRangeForFixture(fixtureName, address);
  return `Universe ${parseInt(universe, 10) || 0}, addresses ${range.start}-${range.end} (${range.footprint} channel${range.footprint > 1 ? "s" : ""})`;
}

function buildFixtureChangeWarningConfig(device, oldFixtureName, newFixtureName, keepUniverse, keepAddress, remapSlot, remapPlan) {
  const oldLabel = getFixtureDisplayName(oldFixtureName);
  const newLabel = getFixtureDisplayName(newFixtureName);
  const oldRange = buildFixtureRangeSummary(device.universe, device.address, oldFixtureName);
  const keepRange = buildFixtureRangeSummary(keepUniverse, keepAddress, newFixtureName);
  const remapSummary = remapSlot
    ? buildFixtureRangeSummary(remapSlot.universe, remapSlot.address, newFixtureName)
    : "No free DMX slot was found for the new fixture.";
  const keepOverlapReport = findRigAddressOverlaps({
    deviceId: device.id,
    fixtureName: newFixtureName,
    universe: keepUniverse,
    address: keepAddress,
  });
  const overlapCount = keepOverlapReport.overlaps.length;
  const overlapText = overlapCount
    ? `Keep mode will overlap ${overlapCount} existing fixture${overlapCount > 1 ? "s" : ""} on the current DMX range.`
    : "Keep mode preserves the edited address exactly as entered.";

  return {
    title: `Change Fixture For Device ${device.id}`,
    warningText: `${oldLabel} -> ${newLabel}\n\nCurrent: ${oldRange}\nKeep mode: ${keepRange}`,
    keepText: "Keep the edited address and preserve the current behavior, even if some cues/effects break.",
    keepDetail: `${keepRange}\n${overlapText}`,
    remapText: "Find the next free DMX slot automatically and update cue channels to match the new fixture.",
    remap: {
      available: !!remapSlot,
      summary: remapSummary,
    },
    defaultStrategy: remapSlot ? "remap" : "keep",
  };
}

function buildFixtureRemapResolutionConfig(device, newFixtureName, remapSlot, remapPlan) {
  return {
    title: `Resolve Remap For Device ${device.id}`,
    warningText: `The software found a new DMX slot for ${getFixtureDisplayName(newFixtureName)}.\nReview the automatic channel resolution below before applying the remap.`,
    remap: {
      available: !!remapSlot,
      summary: remapSlot
        ? buildFixtureRangeSummary(remapSlot.universe, remapSlot.address, newFixtureName)
        : "No free DMX slot was found for the new fixture.",
    },
    autoResolutions: remapPlan?.autoResolutions || [],
    manualResolutions: remapPlan?.manualResolutions || [],
  };
}

function buildFixtureStatusHero(universe, address, fixtureName) {
  const range = getAddressRangeForFixture(fixtureName, address);
  return `U${parseInt(universe, 10) || 0}  ${range.start}-${range.end}`;
}

function getFixtureDisplayName(fixtureName) {
  const fi = fixtures?.[fixtureName] || {};
  return String(fi?.meta?.model || fi?.info?.model || fixtureName || "Fixture");
}

function getAddressRangeForFixture(fixtureName, address) {
  const fi = fixtures?.[fixtureName] || {};
  const footprint = getFixtureFootprint(fi);
  const start = clamp(parseInt(address, 10) || 0, 0, 511);
  return {
    start,
    end: start + footprint - 1,
    footprint,
  };
}

function findRigAddressOverlaps({ deviceId = null, fixtureName, universe, address }) {
  const targetUniverse = parseInt(universe, 10) || 0;
  const targetRange = getAddressRangeForFixture(fixtureName, address);
  const overlaps = [];

  for (const other of Object.values(rigDevices)) {
    if (!other) continue;
    if (deviceId != null && String(other.id) === String(deviceId)) continue;
    if ((parseInt(other.universe, 10) || 0) !== targetUniverse) continue;

    const otherRange = getAddressRangeForFixture(other.fixture, other.address);
    const overlapStart = Math.max(targetRange.start, otherRange.start);
    const overlapEnd = Math.min(targetRange.end, otherRange.end);
    if (overlapStart > overlapEnd) continue;

    overlaps.push({
      deviceId: String(other.id),
      cname: String(other.cname || `Device ${other.id}`),
      fixtureName: String(other.fixture || ""),
      start: otherRange.start,
      end: otherRange.end,
      overlapStart,
      overlapEnd,
    });
  }

  return {
    universe: targetUniverse,
    fixtureName: String(fixtureName || ""),
    targetRange,
    overlaps,
  };
}

function buildRigOverlapKey(overlap) {
  return [
    String(overlap?.deviceId || ""),
    parseInt(overlap?.overlapStart, 10) || 0,
    parseInt(overlap?.overlapEnd, 10) || 0
  ].join(":");
}

function buildRigOverlapWarningText(report, overlaps) {
  const targetLabel = getFixtureDisplayName(report?.fixtureName);
  const range = report?.targetRange || { start: 0, end: 0, footprint: 1 };
  const countLabel = range.footprint === 1 ? "1 DMX address" : `${range.footprint} DMX addresses`;
  const lines = [
    `The selected fixture "${targetLabel}" uses ${countLabel}.`,
    `Universe ${report?.universe ?? 0}, range ${range.start}-${range.end}.`,
    "",
    "This creates overlaps with:",
  ];

  overlaps.forEach((overlap) => {
    const fixtureLabel = getFixtureDisplayName(overlap.fixtureName);
    lines.push(
      `- Device ${overlap.deviceId} (${overlap.cname}) - ${fixtureLabel}, range ${overlap.start}-${overlap.end}, overlap ${overlap.overlapStart}-${overlap.overlapEnd}`
    );
  });

  lines.push("");
  lines.push("The change will still be applied.");
  return lines.join("\n");
}

///////////////////////
// DEVICE CRUD
///////////////////////

function addDeviceFromUI() {
  const fixtureName = $id("fixture-type-select")?.value;
  if (!fixtureName) return toast("Select a fixture first.", "error");

  const fi = fixtures[fixtureName] || {};
  const addrCount = getFixtureFootprint(fi);

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
  scheduleRigSync();

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

  try {
    const res = await deviceEditModal(dev);
    if (!res) return;

    const prevFixture = String(dev.fixture || "");
    const prevUniverse = clamp(parseInt(dev.universe, 10) || 0, 0, 9999);
    const prevAddress = clamp(parseInt(dev.address, 10) || 0, 0, 511);

    const requestedFixture = String(res.fixture || prevFixture).trim();
    const nextFixture = fixtures?.[requestedFixture] && !fixtures[requestedFixture]?.error
      ? requestedFixture
      : prevFixture;
    const nextUniverse = clamp(parseInt(res.universe, 10) || 0, 0, 9999);
    const nextAddress = clamp(parseInt(res.address, 10) || 0, 0, 511);
    const fixtureChanged = nextFixture !== prevFixture;
    let finalUniverse = nextUniverse;
    let finalAddress = nextAddress;
    let appliedRemap = false;

    if (fixtureChanged) {
      const remapSlot = findAutoRemapSlot(nextFixture, nextUniverse, id);
      const effectiveRemapUniverse = remapSlot?.universe ?? nextUniverse;
      const effectiveRemapAddress = remapSlot?.address ?? nextAddress;
      const remapPlan = buildFixtureRemapPlan(
        id,
        prevFixture,
        nextFixture,
        prevUniverse,
        prevAddress,
        effectiveRemapUniverse,
        effectiveRemapAddress
      );

      const decision = await fixtureChangeDecisionModal(
        buildFixtureChangeWarningConfig(
          dev,
          prevFixture,
          nextFixture,
          nextUniverse,
          nextAddress,
          remapSlot,
          remapPlan
        )
      );
      if (!decision) {
        await operationStatusModal({
          status: "error",
          title: "Fixture Update Canceled",
          message: "The fixture change was canceled by the user.",
          hero: "No Changes Applied",
          details: "The device kept its previous fixture and DMX address."
        });
        return;
      }

      if (decision.strategy === "remap") {
        if (!remapSlot) {
          await operationStatusModal({
            status: "error",
            title: "Fixture Update Failed",
            message: "No free DMX slot was found for the selected fixture.",
            hero: "No Free Address",
            details: "The fixture was not changed because the software could not place it on any universe."
          });
          return;
        }

        let sourceToNewKey = { ...remapPlan.sourceToNewKey };
        const needsResolution = remapPlan.autoResolutions.length > 0 || remapPlan.manualResolutions.length > 0;
        if (needsResolution) {
          const resolution = await fixtureRemapModal(
            buildFixtureRemapResolutionConfig(dev, nextFixture, remapSlot, remapPlan)
          );
          if (!resolution) {
            await operationStatusModal({
              status: "error",
              title: "Fixture Update Canceled",
              message: "The remap resolution step was canceled by the user.",
              hero: "No Changes Applied",
              details: "The device kept its previous fixture and DMX address."
            });
            return;
          }
          sourceToNewKey = {
            ...sourceToNewKey,
            ...(resolution.manualMappings || {}),
          };
        }

        finalUniverse = effectiveRemapUniverse;
        finalAddress = effectiveRemapAddress;

        applyCueChannelRemapForDevice(
          id,
          prevAddress,
          remapPlan.oldMeta.footprint,
          finalUniverse,
          finalAddress,
          remapPlan.oldMeta,
          remapPlan.newMeta,
          sourceToNewKey
        );
        applyCueGroupRemapForDevice(
          id,
          remapPlan.oldMeta,
          remapPlan.newMeta,
          sourceToNewKey
        );

        appliedRemap = true;
      }
    }

    rigDevices[id].fixture = nextFixture;
    rigDevices[id].cname = res.cname;
    rigDevices[id].universe = finalUniverse;
    rigDevices[id].address = finalAddress;

    if (fixtureChanged) {
      deviceLocalValues[id] = {};
      deviceCurrentGroups[id] = new Set();
      initDeviceDefaults(id, nextFixture);
    }

    scheduleMovementSync();
    scheduleDummySync();
    scheduleRigSync();

    drawRig();
    refreshControllerFromSelection();

    if (fixtureChanged) {
      const successDetails = appliedRemap
        ? `The fixture was updated and all cue addresses for device ${id} were recalculated automatically.`
        : `The fixture was updated and the edited DMX address was kept as requested.`;
      await operationStatusModal({
        status: "success",
        title: "Fixture Updated",
        message: `${getFixtureDisplayName(prevFixture)} -> ${getFixtureDisplayName(nextFixture)}`,
        hero: buildFixtureStatusHero(finalUniverse, finalAddress, nextFixture),
        details: `${successDetails}\n${buildFixtureRangeSummary(finalUniverse, finalAddress, nextFixture)}`
      });
    }
  } catch (error) {
    console.error("[RIG] fixture edit failed:", error);
    await operationStatusModal({
      status: "error",
      title: "Fixture Update Failed",
      message: "The fixture change crashed before completion.",
      hero: "Update Failed",
      details: String(error?.message || error || "Unknown error")
    });
  }
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
    if (typeof invalidateDevicePreviewCache === "function") invalidateDevicePreviewCache(id);
    if (typeof invalidateDeviceAttrCache === "function") invalidateDeviceAttrCache(id);
  }
  scheduleMovementSync();
  scheduleDummySync();
  scheduleRigSync();

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
      home_pan: dev.home_pan ?? null,
      home_tilt: dev.home_tilt ?? null,
      invert_pan: !!dev.invert_pan,
      invert_tilt: !!dev.invert_tilt,
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
    scheduleRigSync();
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
      home_pan: dev.home_pan ?? null,
      home_tilt: dev.home_tilt ?? null,
      invert_pan: !!dev.invert_pan,
      invert_tilt: !!dev.invert_tilt,
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
  scheduleRigSync();
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

// Cheap in-drag update: change just the selection count text in the
// controller header, without rebuilding the dimmer/color/position/other
// sections. Used by the select-rect drag loop so the page DOM doesn't
// reflow on every mousemove.
function updateSelectionCountOnly() {
  const info = (typeof $id === "function") ? $id("controller-info") : null;
  if (!info) return;
  const n = selectedDeviceOrder ? selectedDeviceOrder.length : 0;
  info.textContent = n ? `${n} device(s) selected.` : "Select device(s) in rig.";
}

// One button per distinct fixture template currently present in the rig.
// Clicking a button selects every device of that type at once.
let _rigTypeButtonsSig = "";
function renderRigTypeButtons() {
  const bar = (typeof $id === "function") ? $id("rig-type-bar") : null;
  const host = (typeof $id === "function") ? $id("rig-type-buttons") : null;
  if (!host || !bar) return;

  // Count devices per fixture template.
  const counts = {};
  for (const dev of Object.values(rigDevices)) {
    const t = String(dev.fixture || "").trim();
    if (!t) continue;
    counts[t] = (counts[t] || 0) + 1;
  }
  const types = Object.keys(counts).sort();
  // Signature avoids DOM rebuild when nothing relevant changed.
  const sig = types.map(t => `${t}:${counts[t]}`).join("|");
  if (sig === _rigTypeButtonsSig) return;
  _rigTypeButtonsSig = sig;

  if (types.length === 0) {
    host.innerHTML = "";
    bar.hidden = true;
    return;
  }
  bar.hidden = false;

  // Build buttons.
  host.innerHTML = "";
  for (const t of types) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "secondary";
    btn.dataset.fixtureType = t;
    btn.title = `Select all ${counts[t]} "${t}" device(s)`;
    btn.innerHTML = `<span class="rig-type-label">${escapeHtmlSimple(prettifyFixtureName(t))}</span><span class="rig-type-count">${counts[t]}</span>`;
    btn.addEventListener("click", (e) => {
      const additive = e.ctrlKey || e.metaKey || e.shiftKey;
      selectAllDevicesOfType(t, additive);
    });
    host.appendChild(btn);
  }
}

function prettifyFixtureName(name) {
  return String(name || "")
    .replace(/[_\-]+/g, " ")
    .replace(/\b\w/g, c => c.toUpperCase());
}

function escapeHtmlSimple(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

// Replace (or augment with Ctrl/Shift) the current selection with every
// rigDevice whose `fixture` equals `type`.
function selectAllDevicesOfType(type, additive) {
  const t = String(type || "").trim();
  if (!t) return;
  const matchIds = Object.entries(rigDevices)
    .filter(([_, d]) => String(d.fixture || "") === t)
    .map(([id, _]) => String(id));
  if (!matchIds.length) return;

  if (additive) {
    for (const id of matchIds) {
      if (!selectedDeviceSet.has(id)) {
        selectedDeviceSet.add(id);
        selectedDeviceOrder.push(id);
      }
    }
  } else {
    selectedDeviceOrder = matchIds.slice();
    selectedDeviceSet = new Set(selectedDeviceOrder);
  }
  refreshControllerFromSelection();
  drawRig();
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
  rgbWidgetRefs = [];
  posWidgetRefs = [];
  updateRigSortButtonsState();

  if (!selectedDeviceOrder.length) {
    if (info) info.textContent = "Select device(s) in rig.";
    $id("intensity-body") && ($id("intensity-body").innerHTML = "");
    $id("color-body") && ($id("color-body").innerHTML = "");
    $id("position-body") && ($id("position-body").innerHTML = "");
    $id("other-body") && ($id("other-body").innerHTML = "");
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
    $id("other-body") && ($id("other-body").innerHTML = "");
    if ($id('tab-effects')?.classList.contains('active')) renderEffectsTargets();
    return;
  }

  const fi = fixtures[first.fixture] || {};

  const ib = $id("intensity-body");
  if (ib) {
    ib.innerHTML = "";
    renderFixtureGroupSection(ib, getFixtureGroups(fi, "dimmer"), renderDimmerGroupControls, "rig.info.noDimmer", "No dimmer defined.", renderGlobalDimmerGroupControls);
  }

  const cb = $id("color-body");
  if (cb) {
    cb.innerHTML = "";
    renderFixtureGroupSection(cb, getFixtureGroups(fi, "color"), renderColorGroupControls, "rig.info.noColor", "No color functions defined.", renderGlobalColorGroupControls);
  }

  const pb = $id("position-body");
  if (pb) {
    pb.innerHTML = "";
    renderFixtureGroupSection(pb, getFixtureGroups(fi, "position"), renderPositionGroupControls, "rig.info.noPosition", "No position function defined.", renderGlobalPositionGroupControls);
    addDistributeControls(pb);
  }

  const ob = $id("other-body");
  if (ob) {
    ob.innerHTML = "";
    renderFixtureGroupSection(ob, getFixtureGroups(fi, "other"), renderOtherGroupControls, "rig.info.noOther", "No other controls yet.");
  }

  // Si l’onglet "Effects" est actif, on rafraîchit l’affichage des groupes
  if ($id('tab-effects')?.classList.contains('active')) {
    renderEffectsTargets();
  }
}


///////////////////////
// SLIDERS GÉNÉRIQUES
///////////////////////

function normalizeLocalIndices(localIndexOrList) {
  const raw = Array.isArray(localIndexOrList) ? localIndexOrList : [localIndexOrList];
  return Array.from(new Set(
    raw
      .map(idx => parseInt(idx, 10))
      .filter(idx => Number.isFinite(idx))
  ));
}

function getPrimaryLocalIndex(localIndexOrList) {
  return normalizeLocalIndices(localIndexOrList)[0] ?? null;
}

function getSelectionLocalValue(localIndexOrList, fallback = 0) {
  const indices = normalizeLocalIndices(localIndexOrList);
  if (!indices.length || !selectedDeviceOrder.length) return fallback;
  const firstId = selectedDeviceOrder[0];
  const vals = deviceLocalValues[firstId] || {};
  return vals[indices[0]] ?? fallback;
}

function applyValueToSelectionLocals(localIndexOrList, value) {
  const indices = normalizeLocalIndices(localIndexOrList);
  if (!indices.length) return;
  for (const id of selectedDeviceOrder) {
    deviceLocalValues[id] ||= {};
    indices.forEach(idx => {
      deviceLocalValues[id][idx] = value;
    });
  }
}

function addLocalSlider(container, label, localIndex, opts = {}) {
  const indices = normalizeLocalIndices(localIndex);
  const primaryIndex = indices[0] ?? 0;
  const row = document.createElement("div");
  row.className = "ctrl-row";

  const lab = document.createElement("label");
  const showIndex = opts.showIndex !== false && indices.length === 1;
  lab.textContent = showIndex ? `${label} (${primaryIndex})` : label;

  const slider = document.createElement("input");
  slider.type = "range";
  slider.min = "0";
  slider.max = "255";
  slider.className = "ctrl-slider " + (opts.className || "");

  const valSpan = document.createElement("div");
  valSpan.className = "slider-value";

  const getCommonValue = () => getSelectionLocalValue(indices, 0);

  slider.value = getCommonValue();
  valSpan.textContent = slider.value;

  slider.oninput = () => {
    const v = parseInt(slider.value, 10) || 0;
    valSpan.textContent = v;

    applyValueToSelectionLocals(indices, v);

    scheduleSelectionApply();
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
// GROUPED CONTROLS
///////////////////////

function collectFamilyChannelMap(groups) {
  const out = {};
  (Array.isArray(groups) ? groups : []).forEach(group => {
    getGroupChannels(group).forEach(channel => {
      const role = String(channel?.role || "").trim().toLowerCase();
      if (!role) return;
      out[role] ||= [];
      out[role].push(parseInt(channel?.offset ?? 0, 10) || 0);
    });
  });
  Object.keys(out).forEach(role => {
    out[role] = Array.from(new Set(out[role]));
  });
  return out;
}

function appendGlobalFixtureCard(container, label, renderer, groups) {
  if (typeof renderer !== "function") return;
  const card = document.createElement("div");
  card.className = "fixture-group-card";
  const title = document.createElement("div");
  title.className = "fixture-group-title";
  title.textContent = label;
  card.appendChild(title);
  renderer(card, groups);
  container.appendChild(card);
}

function renderFixtureGroupSection(container, groups, renderer, emptyI18nKey, emptyFallback, globalRenderer = null) {
  const list = Array.isArray(groups) ? groups : [];
  if (!list.length) {
    const empty = document.createElement("span");
    empty.className = "muted";
    empty.textContent = window.t ? window.t(emptyI18nKey, emptyFallback) : emptyFallback;
    container.appendChild(empty);
    return;
  }
  if (list.length > 1 && typeof globalRenderer === "function") {
    appendGlobalFixtureCard(container, "Global", globalRenderer, list);
  }
  list.forEach(group => {
    const card = document.createElement("div");
    card.className = "fixture-group-card";
    const title = document.createElement("div");
    title.className = "fixture-group-title";
    title.textContent = getFixtureGroupLabel(group);
    card.appendChild(title);
    renderer(card, group);
    container.appendChild(card);
  });
}

function renderPresetButtons(container, slider, presets) {
  if (!Array.isArray(presets) || !presets.length || !slider) return;
  const row = document.createElement("div");
  row.className = "fixture-presets";
  presets.forEach(preset => {
    if (!preset || preset.label == null) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "secondary";
    btn.textContent = `${preset.label} (${preset.min}-${preset.max})`;
    btn.onclick = () => {
      const min = clamp(parseInt(preset.min ?? 0, 10) || 0, 0, 255);
      const max = clamp(parseInt(preset.max ?? min, 10) || min, 0, 255);
      slider.value = String(Math.round((min + max) / 2));
      slider.dispatchEvent(new Event("input"));
    };
    row.appendChild(btn);
  });
  container.appendChild(row);
}

function renderDimmerGroupControls(container, group) {
  getGroupChannels(group).forEach(channel => {
    const label = humanizeFixtureToken(channel?.role || "Level");
    const slider = addLocalSlider(container, label, parseInt(channel?.offset ?? 0, 10) || 0);
    renderPresetButtons(container, slider, channel?.presets);
  });
}

function renderGlobalDimmerGroupControls(container, groups) {
  const channelMap = collectFamilyChannelMap(groups);
  Object.entries(channelMap).forEach(([role, offsets]) => {
    addLocalSlider(container, humanizeFixtureToken(role || "Level"), offsets, { showIndex: false });
  });
}

function addRgbControls(container, rgbMap, extraChannels = [], groupKey = "color") {
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
    sliders[c.key] = addLocalSlider(container, c.name, li, { showIndex: !Array.isArray(li) });
  });

  extraChannels.forEach(channel => {
    const label = humanizeFixtureToken(channel?.role || "Value");
    const offsets = channel?.offsets || (parseInt(channel?.offset ?? 0, 10) || 0);
    const slider = addLocalSlider(container, label, offsets, { showIndex: !Array.isArray(offsets) });
    if (!Array.isArray(offsets)) {
      renderPresetButtons(container, slider, channel?.presets);
    }
  });

  const widgetRef = { key: groupKey, wheelEl: wheel, cursorEl: cursor, sliders, rgbMap, previewEl: preview };
  rgbWidgetRefs.push(widgetRef);

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

    moveWheelCursor(widgetRef, hDeg, s);
  }

  wheel.onmousedown = (e) => { _activeDragHandler = setFromWheelEvent; setFromWheelEvent(e); };

  wheel.ondblclick = () => {
    sliders.red.value = 255;
    sliders.green.value = 255;
    sliders.blue.value = 255;
    sliders.red.dispatchEvent(new Event("input"));
    sliders.green.dispatchEvent(new Event("input"));
    sliders.blue.dispatchEvent(new Event("input"));
    moveWheelCursor(widgetRef, 0, 0);
  };

  updateColorPreview(preview, rgbMap);
  syncRgbWidgetFromFirstDevice();
}

function renderColorGroupControls(container, group) {
  const channels = getGroupChannels(group);
  const rgbMap = {};
  const extras = [];
  channels.forEach(channel => {
    const role = String(channel?.role || "").toLowerCase();
    if (role === "red" || role === "green" || role === "blue") {
      rgbMap[role] = parseInt(channel?.offset ?? 0, 10) || 0;
    } else {
      extras.push(channel);
    }
  });

  if (rgbMap.red != null && rgbMap.green != null && rgbMap.blue != null) {
    addRgbControls(container, rgbMap, extras, String(group?.id || ""));
    return;
  }

  channels.forEach(channel => {
    const label = humanizeFixtureToken(channel?.role || "Value");
    const slider = addLocalSlider(container, label, parseInt(channel?.offset ?? 0, 10) || 0);
    renderPresetButtons(container, slider, channel?.presets);
  });
}

function renderGlobalColorGroupControls(container, groups) {
  const channelMap = collectFamilyChannelMap(groups);
  const rgbMap = {
    red: channelMap.red,
    green: channelMap.green,
    blue: channelMap.blue,
  };
  const extras = Object.entries(channelMap)
    .filter(([role]) => !["red", "green", "blue"].includes(role))
    .map(([role, offsets]) => ({ role, offsets }));

  if (rgbMap.red?.length && rgbMap.green?.length && rgbMap.blue?.length) {
    addRgbControls(container, rgbMap, extras, "color.global");
    return;
  }

  Object.entries(channelMap).forEach(([role, offsets]) => {
    addLocalSlider(container, humanizeFixtureToken(role || "Value"), offsets, { showIndex: false });
  });
}

function moveWheelCursor(widgetRef, hDeg, s) {
  if (!widgetRef) return;
  const wheel = widgetRef.wheelEl;
  const cursor = widgetRef.cursorEl;
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
  const r = vals[getPrimaryLocalIndex(rgbMap.red)] ?? 0;
  const g = vals[getPrimaryLocalIndex(rgbMap.green)] ?? 0;
  const b = vals[getPrimaryLocalIndex(rgbMap.blue)] ?? 0;
  preview.style.background = `rgb(${r},${g},${b})`;
}

function syncRgbWidgetFromFirstDevice() {
  if (!rgbWidgetRefs.length || !selectedDeviceOrder.length) return;
  const id = selectedDeviceOrder[0];
  const vals = deviceLocalValues[id] || {};
  rgbWidgetRefs.forEach(widgetRef => {
    const { rgbMap, previewEl } = widgetRef;
    const r = vals[getPrimaryLocalIndex(rgbMap.red)] ?? 0;
    const g = vals[getPrimaryLocalIndex(rgbMap.green)] ?? 0;
    const b = vals[getPrimaryLocalIndex(rgbMap.blue)] ?? 0;
    const hsv = rgbToHsv(r, g, b);
    moveWheelCursor(widgetRef, hsv.h, hsv.s);
    if (previewEl) updateColorPreview(previewEl, rgbMap);
  });
}

///////////////////////
// Position XY + Pan/Tilt
///////////////////////

function addPositionControls(container, posMap, groupKey = "position", extraChannels = []) {
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
    panSlider = addLocalSlider(panWrapper, "Pan", panIdx, { showIndex: !Array.isArray(panIdx) });
  }

  if (tiltIdx != null) {
    // on lui donne juste une classe en plus pour le style vertical
    tiltSlider = addLocalSlider(tiltWrapper, "Tilt", tiltIdx, {
      className: "tilt-row",
      showIndex: !Array.isArray(tiltIdx)
    });
  }

  const widgetRef = { key: groupKey, xyEl: xy, cursorEl: cursor, panSlider, tiltSlider, panIdx, tiltIdx };
  posWidgetRefs.push(widgetRef);

  function setFromXYEvent(e) {
    const r = xy.getBoundingClientRect();
    const denomX = Math.max(1, r.width - 1);
    const denomY = Math.max(1, r.height - 1);
    let nx = clamp((e.clientX - r.left) / denomX, 0, 1);
    let ny = clamp((e.clientY - r.top) / denomY, 0, 1);

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

    moveXYCursor(widgetRef, nx, ny);
  }

  xy.onmousedown = (e) => { _activeDragHandler = setFromXYEvent; setFromXYEvent(e); };

  xy.ondblclick = () => {
    if (panSlider) { panSlider.value = "128"; panSlider.dispatchEvent(new Event("input")); }
    if (tiltSlider) { tiltSlider.value = "128"; tiltSlider.dispatchEvent(new Event("input")); }
    moveXYCursor(widgetRef, 0.5, 0.5);
  };

  extraChannels.forEach(channel => {
    const label = humanizeFixtureToken(channel?.role || "Value");
    const offsets = channel?.offsets || (parseInt(channel?.offset ?? 0, 10) || 0);
    const slider = addLocalSlider(container, label, offsets, { showIndex: !Array.isArray(offsets) });
    if (!Array.isArray(offsets)) {
      renderPresetButtons(container, slider, channel?.presets);
    }
  });

  syncPosWidgetFromFirstDevice();
}

///////////////////////
// Other / Focus / Generic
///////////////////////

function renderPositionGroupControls(container, group) {
  const panChannel = getGroupChannel(group, "pan");
  const tiltChannel = getGroupChannel(group, "tilt");
  const extras = getGroupChannels(group).filter(channel => {
    const role = String(channel?.role || "").toLowerCase();
    return role !== "pan" && role !== "tilt";
  });

  if (panChannel || tiltChannel) {
    addPositionControls(container, {
      pan: panChannel ? { channel: parseInt(panChannel.offset ?? 0, 10) || 0 } : null,
      tilt: tiltChannel ? { channel: parseInt(tiltChannel.offset ?? 0, 10) || 0 } : null,
    }, String(group?.id || ""), extras);
    return;
  }

  getGroupChannels(group).forEach(channel => {
    const label = humanizeFixtureToken(channel?.role || "Value");
    const slider = addLocalSlider(container, label, parseInt(channel?.offset ?? 0, 10) || 0);
    renderPresetButtons(container, slider, channel?.presets);
  });
}

function renderGlobalPositionGroupControls(container, groups) {
  const channelMap = collectFamilyChannelMap(groups);
  const extras = Object.entries(channelMap)
    .filter(([role]) => !["pan", "tilt"].includes(role))
    .map(([role, offsets]) => ({ role, offsets }));

  if (channelMap.pan?.length || channelMap.tilt?.length) {
    addPositionControls(container, {
      pan: channelMap.pan?.length ? { channel: channelMap.pan } : null,
      tilt: channelMap.tilt?.length ? { channel: channelMap.tilt } : null,
    }, "position.global", extras);
    return;
  }

  Object.entries(channelMap).forEach(([role, offsets]) => {
    addLocalSlider(container, humanizeFixtureToken(role || "Value"), offsets, { showIndex: false });
  });
}

///////////////////////
// Distribute Pan/Tilt across selection
///////////////////////

// Distribute works directly in DMX values (0-255), not degrees.
let distributeState = {
  pan:  { from: 0, to: 255, mode: "linear", seed: null },
  tilt: { from: 0, to: 255, mode: "linear", seed: null },
};

function enumeratePositionSlots(role) {
  const slots = [];
  for (const id of selectedDeviceOrder) {
    const dev = rigDevices[id];
    if (!dev) continue;
    const fi = fixtures[dev.fixture] || {};
    const posGroups = getFixtureGroups(fi, "position");
    for (const g of posGroups) {
      const ch = getGroupChannel(g, role);
      if (!ch) continue;
      const offset = parseInt(ch.offset ?? 0, 10) || 0;
      const rangeDegRaw = parseInt(ch.range_deg ?? 0, 10) || 0;
      const rangeDeg = rangeDegRaw > 0 ? rangeDegRaw : (role === "pan" ? 540 : 270);
      slots.push({ deviceId: id, groupId: String(g.id || ""), offset, rangeDeg });
    }
  }
  return slots;
}

function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function seededShuffle(arr, seed) {
  const out = arr.slice();
  const rand = mulberry32(seed);
  for (let i = out.length - 1; i > 0; i--) {
    const j = Math.floor(rand() * (i + 1));
    [out[i], out[j]] = [out[j], out[i]];
  }
  return out;
}

function applyDistribute(role) {
  const slots = enumeratePositionSlots(role);
  const n = slots.length;
  if (n < 2) return;

  const st = distributeState[role];
  const fromVal = st.from;
  const toVal = st.to;

  let order = slots.map((_, i) => i);
  if (st.mode === "random") {
    if (st.seed == null) st.seed = (Math.random() * 0x7FFFFFFF) | 0;
    order = seededShuffle(order, st.seed);
  }

  for (let i = 0; i < n; i++) {
    const slot = slots[order[i]];
    const t = i / (n - 1);
    // from/to are DMX values directly; interpolate and clamp to 0-255.
    const dmx = Math.max(0, Math.min(255, Math.round(fromVal + (toVal - fromVal) * t)));
    deviceLocalValues[slot.deviceId] ||= {};
    deviceLocalValues[slot.deviceId][slot.offset] = dmx;
  }

  applySelectionToEngine(true);
  drawRig();
  syncPosWidgetFromFirstDevice();
}

function addDistributeControls(container) {
  const panSlots = enumeratePositionSlots("pan");
  const tiltSlots = enumeratePositionSlots("tilt");
  if (!panSlots.length && !tiltSlots.length) return;

  const card = document.createElement("div");
  card.className = "fixture-group-card distribute-card";

  const title = document.createElement("div");
  title.className = "fixture-group-title";
  title.textContent = window.t ? window.t("controller.distribute", "Distribute") : "Distribute";
  card.appendChild(title);

  function buildRoleBlock(role, slotsCount) {
    const block = document.createElement("div");
    block.className = "distribute-block";

    const st = distributeState[role];

    const lab = document.createElement("div");
    lab.className = "distribute-label";
    const labKey = role === "pan" ? "rig.label.pan" : "rig.label.tilt";
    const labFallback = role === "pan" ? "Pan" : "Tilt";
    const slotsHint = ` (${slotsCount})`;
    lab.textContent = (window.t ? window.t(labKey, labFallback) : labFallback) + slotsHint;
    block.appendChild(lab);

    const row = document.createElement("div");
    row.className = "distribute-row";

    const fromLab = document.createElement("span");
    fromLab.className = "distribute-small";
    fromLab.textContent = window.t ? window.t("controller.distributeFrom", "From") : "From";
    row.appendChild(fromLab);

    const fromInput = document.createElement("input");
    fromInput.type = "number";
    fromInput.step = "1";
    fromInput.min = "0";
    fromInput.max = "255";
    fromInput.className = "distribute-input";
    fromInput.value = st.from;
    row.appendChild(fromInput);

    const sep = document.createElement("span");
    sep.className = "distribute-sep";
    sep.textContent = "→";
    row.appendChild(sep);

    const toLab = document.createElement("span");
    toLab.className = "distribute-small";
    toLab.textContent = window.t ? window.t("controller.distributeTo", "To") : "To";
    row.appendChild(toLab);

    const toInput = document.createElement("input");
    toInput.type = "number";
    toInput.step = "1";
    toInput.min = "0";
    toInput.max = "255";
    toInput.className = "distribute-input";
    toInput.value = st.to;
    row.appendChild(toInput);

    const unit = document.createElement("span");
    unit.className = "distribute-small";
    unit.textContent = "DMX";
    row.appendChild(unit);

    block.appendChild(row);

    const sliderRow = document.createElement("div");
    sliderRow.className = "distribute-slider-row";

    const fromSlider = document.createElement("input");
    fromSlider.type = "range";
    fromSlider.className = "distribute-slider";
    fromSlider.min = "0";
    fromSlider.max = "255";
    fromSlider.step = "1";
    fromSlider.value = st.from;
    sliderRow.appendChild(fromSlider);

    const toSlider = document.createElement("input");
    toSlider.type = "range";
    toSlider.className = "distribute-slider";
    toSlider.min = "0";
    toSlider.max = "255";
    toSlider.step = "1";
    toSlider.value = st.to;
    sliderRow.appendChild(toSlider);

    block.appendChild(sliderRow);

    const btnRow = document.createElement("div");
    btnRow.className = "distribute-btn-row";

    const linearBtn = document.createElement("button");
    linearBtn.type = "button";
    linearBtn.className = "distribute-btn secondary";
    linearBtn.textContent = window.t ? window.t("controller.distributeLinear", "Linear") : "Linear";

    const randomBtn = document.createElement("button");
    randomBtn.type = "button";
    randomBtn.className = "distribute-btn secondary";
    randomBtn.textContent = window.t ? window.t("controller.distributeRandom", "Random") : "Random";

    if (slotsCount < 2) {
      linearBtn.disabled = true;
      randomBtn.disabled = true;
      const hint = window.t ? window.t("controller.distributeNeedTwo", "Need 2+ slots in selection") : "Need 2+ slots in selection";
      linearBtn.title = hint;
      randomBtn.title = hint;
    }

    btnRow.appendChild(linearBtn);
    btnRow.appendChild(randomBtn);
    block.appendChild(btnRow);

    const clampDmx = (n) => Math.max(0, Math.min(255, n));
    function setFrom(v) {
      const n = parseInt(v, 10);
      st.from = clampDmx(Number.isFinite(n) ? n : 0);
      fromInput.value = st.from;
      fromSlider.value = st.from;
    }
    function setTo(v) {
      const n = parseInt(v, 10);
      st.to = clampDmx(Number.isFinite(n) ? n : 0);
      toInput.value = st.to;
      toSlider.value = st.to;
    }

    fromInput.oninput = () => setFrom(fromInput.value);
    toInput.oninput = () => setTo(toInput.value);
    fromSlider.oninput = () => setFrom(fromSlider.value);
    toSlider.oninput = () => setTo(toSlider.value);

    linearBtn.onclick = () => {
      st.mode = "linear";
      applyDistribute(role);
    };
    randomBtn.onclick = () => {
      st.mode = "random";
      st.seed = (Math.random() * 0x7FFFFFFF) | 0;
      applyDistribute(role);
    };

    return block;
  }

  card.appendChild(buildRoleBlock("pan", panSlots.length));
  card.appendChild(buildRoleBlock("tilt", tiltSlots.length));

  container.appendChild(card);
}

function renderOtherGroupControls(container, group) {
  getGroupChannels(group).forEach(channel => {
    const roleLabel = humanizeFixtureToken(channel?.role || group?.kind || "Value");
    const slider = addLocalSlider(container, roleLabel, parseInt(channel?.offset ?? 0, 10) || 0);
    renderPresetButtons(container, slider, channel?.presets);
  });
}

function moveXYCursor(widgetRef, nx, ny) {
  if (!widgetRef) return;
  const xy = widgetRef.xyEl, cursor = widgetRef.cursorEl;
  const w = Math.max(1, xy.clientWidth - 1);
  const h = Math.max(1, xy.clientHeight - 1);
  cursor.style.left = `${nx * w}px`;
  cursor.style.top = `${ny * h}px`;
}

function syncPosWidgetFromFirstDevice() {
  if (!posWidgetRefs.length || !selectedDeviceOrder.length) return;
  const id = selectedDeviceOrder[0];
  const vals = deviceLocalValues[id] || {};
  posWidgetRefs.forEach(widgetRef => {
    const panKey = getPrimaryLocalIndex(widgetRef.panIdx);
    const tiltKey = getPrimaryLocalIndex(widgetRef.tiltIdx);
    const panVal = panKey != null ? (vals[panKey] ?? 128) : 128;
    const tiltVal = tiltKey != null ? (vals[tiltKey] ?? 128) : 128;
    moveXYCursor(widgetRef, panVal / 255, tiltVal / 255);
  });
}


///////////////////////
// APPLY LIVE SELECTION -> ENGINE
///////////////////////

// Debounced wrapper for high-frequency callers (slider oninput). Coalesces
// multiple calls within `delayMs` into a single apply. Used during slider drag
// so we don't rebuild the per-universe payload at 60+ Hz; the DMX pump still
// runs at 50Hz independently so latency feels identical.
let _selectionApplyTimer = null;
function scheduleSelectionApply(delayMs = 30) {
  if (_selectionApplyTimer) clearTimeout(_selectionApplyTimer);
  _selectionApplyTimer = setTimeout(() => {
    _selectionApplyTimer = null;
    applySelectionToEngine(true);
  }, delayMs);
}

async function applySelectionToEngine(silent = false) {
  // ========================================
  // PROTECTION : Verrou DMX
  // ========================================
  // A *paused* backend playback still owns the rig (backendPlaybackOwned) and
  // applyUniverseState() would silently drop this write — the rig looks frozen
  // and manual edits do nothing ("rien ne joue mais je ne peux rien changer").
  // The user grabbing a control means "hand me manual control now": stop the
  // frozen playback so the edit actually lands. An actively-running playback is
  // intentionally left alone (manual edits shouldn't fight live cues).
  if (
    window.backendPlaybackOwned &&
    typeof playbackPaused !== "undefined" && playbackPaused &&
    typeof stopRun === "function"
  ) {
    try { await stopRun(true); }
    catch (e) { console.warn("[RIG] could not release paused playback for manual edit:", e); }
  }

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

// Public entry point: coalesces multiple calls in the same frame to a single
// canvas repaint. Slider drags / SSE bursts can call drawRig() dozens of times
// per second; without throttling each call did a full clearRect + grid + devices
// repaint. rAF caps to ~60fps and merges intra-frame calls into one.
let _drawRigPending = false;
function drawRig() {
  if (_drawRigPending) return;
  _drawRigPending = true;
  requestAnimationFrame(() => {
    _drawRigPending = false;
    _drawRigImpl();
  });
}

function _drawRigImpl() {
  if (!rigCtx || !rigCanvas) return;
  // Keep the "select by type" buttons in sync with rigDevices. The function
  // signature-caches the rendered state, so this is a no-op when nothing
  // about the set of present fixture types has changed.
  renderRigTypeButtons();
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
    const previewChannels = getDevicePrimaryPreviewChannels(dev);
    if (previewChannels.r != null && previewChannels.g != null && previewChannels.b != null) {
      const pv = devicePreviewRGB[dev.id];
      let r = 0, g = 0, b = 0;
      if (pv) { r = pv.r; g = pv.g; b = pv.b; }
      else {
        const lv = deviceLocalValues[dev.id] || {};
        r = lv[previewChannels.r - (parseInt(dev.address, 10) || 0)] ?? 255;
        g = lv[previewChannels.g - (parseInt(dev.address, 10) || 0)] ?? 255;
        b = lv[previewChannels.b - (parseInt(dev.address, 10) || 0)] ?? 255;
      }

      // Apply dimmer to the color preview
      let dimmerFactor = 1.0;
      if (previewChannels.dimmer != null) {
        const dimmerLocal = previewChannels.dimmer - (parseInt(dev.address, 10) || 0);
        const dimmerVal = devicePreviewDimmer[dev.id] ?? (deviceLocalValues[dev.id]?.[dimmerLocal] ?? 255);
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

    // Prevent the browser from starting a text-selection drag on surrounding
    // page content (which would auto-scroll the viewport once the cursor
    // approaches a window edge during a long select-rect gesture).
    e.preventDefault();

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

      // Cheap in-drag update only: refresh the count text and the sort
      // buttons, but DO NOT rebuild the full controller panel here. The
      // full rebuild grows/shrinks the page DOM (dimmer/color/position/other
      // sections), which causes the viewport to scroll under the user's
      // mouse mid-drag and the selection rect to seem to "drift". The full
      // refresh runs once on mouseup.
      updateSelectionCountOnly();
      updateRigSortButtonsState();
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
      // Now that the drag is over, commit the full controller refresh.
      // Doing it here (instead of on every mousemove) makes the page DOM
      // grow/shrink at most once per gesture, eliminating the layout shift
      // that was scrolling the UI mid-drag.
      refreshControllerFromSelection();
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

// Hook render mode changes
if (typeof window.addRenderModeListener === "function") {
  window.addRenderModeListener((mode) => {
    if (mode === "backend") {
      scheduleRigSync();
    }
  });
}

/* ============================================================
   RIG canvas right-click menu (replaces the top toolbar).
   Name / Universe / Address are auto-assigned on add.
   ============================================================ */
function _rmT(key, fb) { return (typeof window.t === "function") ? window.t(key, fb) : fb; }

function addDeviceAuto(fixtureName, wx, wy) {
  if (!fixtureName || !fixtures[fixtureName]) { toast("Unknown fixture.", "error"); return; }
  const fi = fixtures[fixtureName] || {};
  const addrCount = getFixtureFootprint(fi);
  // Auto universe + address: first universe with a free block.
  let universe = 0, address = null;
  for (let u = 0; u < 64; u++) {
    const free = findNextFreeAddress(u, addrCount);
    if (free != null) { universe = u; address = free; break; }
  }
  if (address == null) { toast("No free DMX address.", "error"); return; }
  const id = String(nextDeviceId++);
  rigDevices[id] = {
    id, fixture: fixtureName, cname: `Device ${id}`, universe, address,
    x: Number.isFinite(wx) ? Math.round(wx) : 100,
    y: Number.isFinite(wy) ? Math.round(wy) : 100,
  };
  deviceLocalValues[id] = {};
  deviceCurrentGroups[id] = new Set();
  initDeviceDefaults(id, fixtureName);
  scheduleMovementSync(); scheduleDummySync(); scheduleRigSync();
  selectedDeviceOrder = [id]; selectedDeviceSet = new Set(selectedDeviceOrder);
  refreshControllerFromSelection(); drawRig();
  if (typeof renderRigTypeButtons === "function") renderRigTypeButtons();
  toast(`Device ${id} added (U${universe}.${address})`, "success");
}

function selectDevicesByType(fixtureName) {
  selectedDeviceOrder = Object.values(rigDevices)
    .filter((d) => d && d.fixture === fixtureName).map((d) => String(d.id));
  selectedDeviceSet = new Set(selectedDeviceOrder);
  refreshControllerFromSelection(); drawRig();
  if (typeof updateRigSortButtonsState === "function") updateRigSortButtonsState();
}

function openRigCalibration() {
  const p = document.getElementById("calib-panel");
  if (!p) return;
  p.classList.add("calib-floating-open");
  p.open = true;
  if (typeof window.refreshCalibrationPanel === "function") window.refreshCalibrationPanel();
}

let _rigMenuEl = null;
function closeRigMenu() {
  if (_rigMenuEl) { _rigMenuEl.remove(); _rigMenuEl = null; }
  document.removeEventListener("mousedown", _rigMenuOutside, true);
  document.removeEventListener("keydown", _rigMenuKey, true);
}
function _rigMenuOutside(e) { if (_rigMenuEl && !_rigMenuEl.contains(e.target)) closeRigMenu(); }
function _rigMenuKey(e) { if (e.key === "Escape") closeRigMenu(); }

function _rmItem(label, onClick, opts) {
  opts = opts || {};
  const b = document.createElement("button");
  b.type = "button";
  b.className = "rig-menu-item" + (opts.back ? " rig-menu-back" : "");
  b.textContent = label + (opts.arrow ? "   ▸" : "");
  if (opts.disabled) b.disabled = true;
  else b.addEventListener("click", (ev) => { ev.stopPropagation(); onClick(); });
  return b;
}
function _rmSep() { const d = document.createElement("div"); d.className = "rig-menu-sep"; return d; }

function _rmRoot(menu) {
  const wx = menu._wx, wy = menu._wy;
  menu.innerHTML = "";
  const nSel = (selectedDeviceOrder || []).length;
  const hasDevices = Object.keys(rigDevices || {}).length > 0;
  menu.appendChild(_rmItem(_rmT("rigmenu.add", "Add device"), () => _rmFixtures(menu), { arrow: true }));
  menu.appendChild(_rmItem(_rmT("rigmenu.selectType", "Select by type"), () => _rmTypes(menu), { arrow: true, disabled: !hasDevices }));
  menu.appendChild(_rmItem(_rmT("rigmenu.order", "Order selection"), () => _rmOrder(menu), { arrow: true, disabled: nSel < 2 }));
  menu.appendChild(_rmSep());
  menu.appendChild(_rmItem(_rmT("rigmenu.delete", "Delete selected"), () => { closeRigMenu(); deleteSelectedDevices(); }, { disabled: nSel === 0 }));
  menu.appendChild(_rmItem(_rmT("calib.title", "Position calibration"), () => { closeRigMenu(); openRigCalibration(); }));
}
function _rmFixtures(menu) {
  const wx = menu._wx, wy = menu._wy;
  menu.innerHTML = "";
  menu.appendChild(_rmItem(_rmT("rigmenu.back", "Back"), () => _rmRoot(menu), { back: true }));
  const names = Object.keys(fixtures || {}).sort();
  if (!names.length) { menu.appendChild(_rmItem(_rmT("rigmenu.noFixtures", "No fixtures loaded"), () => {}, { disabled: true })); return; }
  for (const n of names) {
    const label = (typeof prettifyFixtureName === "function") ? prettifyFixtureName(n) : n;
    menu.appendChild(_rmItem(label, () => { closeRigMenu(); addDeviceAuto(n, wx, wy); }));
  }
}
function _rmTypes(menu) {
  menu.innerHTML = "";
  menu.appendChild(_rmItem(_rmT("rigmenu.back", "Back"), () => _rmRoot(menu), { back: true }));
  const counts = {};
  Object.values(rigDevices || {}).forEach((d) => { if (d) counts[d.fixture] = (counts[d.fixture] || 0) + 1; });
  const types = Object.keys(counts).sort();
  for (const tp of types) {
    const label = ((typeof prettifyFixtureName === "function") ? prettifyFixtureName(tp) : tp) + ` (${counts[tp]})`;
    menu.appendChild(_rmItem(label, () => { closeRigMenu(); selectDevicesByType(tp); }));
  }
}
function _rmOrder(menu) {
  menu.innerHTML = "";
  menu.appendChild(_rmItem(_rmT("rigmenu.back", "Back"), () => _rmRoot(menu), { back: true }));
  const items = [
    ["rig.sortVertical", "Vertical", sortSelectionVertical],
    ["rig.sortHorizontal", "Horizontal", sortSelectionHorizontal],
    ["rig.sortVertOne", "Vert ONE", sortSelectionVerticalOne],
    ["rig.sortHorizOne", "Horiz ONE", sortSelectionHorizontalOne],
    ["rig.sortId", "ID", sortSelectionById],
    ["rig.sortRandom", "Random", shuffleSelection],
    ["rig.sortReverse", "Revert", reverseSelectionOrder],
  ];
  for (const [k, lbl, fn] of items) {
    menu.appendChild(_rmItem(_rmT(k, lbl), () => { closeRigMenu(); try { fn(); } catch (e) {} }));
  }
}

function openRigContextMenu(e) {
  e.preventDefault();
  closeRigMenu();
  const w = (typeof eventToWorld === "function") ? eventToWorld(e) : { wx: 100, wy: 100 };
  const menu = document.createElement("div");
  menu.className = "rig-context-menu";
  menu._wx = w.wx; menu._wy = w.wy;
  _rigMenuEl = menu;
  _rmRoot(menu);
  document.body.appendChild(menu);
  let x = e.clientX, y = e.clientY;
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  if (x + mw > window.innerWidth) x = Math.max(4, window.innerWidth - mw - 4);
  if (y + mh > window.innerHeight) y = Math.max(4, window.innerHeight - mh - 4);
  menu.style.left = x + "px"; menu.style.top = y + "px";
  setTimeout(() => {
    document.addEventListener("mousedown", _rigMenuOutside, true);
    document.addEventListener("keydown", _rigMenuKey, true);
  }, 0);
}

document.addEventListener("DOMContentLoaded", () => {
  const cv = document.getElementById("rig-canvas");
  if (cv) cv.addEventListener("contextmenu", openRigContextMenu);
  // Collapsing the floating calibration panel removes its overlay state.
  const calib = document.getElementById("calib-panel");
  if (calib) calib.addEventListener("toggle", () => { if (!calib.open) calib.classList.remove("calib-floating-open"); });
});
