// static/calibration.js
// Per-fixture "audience home" position + axis inversion for AutoLight 2.0.
// Inline collapsible panel (NOT a modal) so it never depends on modal stacking.
//
// Reads/writes rigDevices[id].home_pan / home_tilt / invert_pan / invert_tilt
// (persisted into the project by buildDevicesDefFromRig and pushed to the
// engine by buildRigRegisterPayload). Uses shared globals from core.js/rig.js:
//   rigDevices, selectedDeviceOrder, getDeviceAttrAbsChannels(),
//   queueDeviceAttrs(), scheduleRigSync(), invalidateDeviceAttrCache(),
//   lastDmxFrames, t().

(function () {
  function $c(id) { return document.getElementById(id); }
  function _ct(key, fallback) { return (typeof window.t === "function") ? window.t(key, fallback) : fallback; }
  function _clamp255(v) { v = parseInt(v, 10); if (!Number.isFinite(v)) return 0; return Math.max(0, Math.min(255, v)); }

  let _lastEditedId = null;

  // Resolve a device's absolute pan/tilt channels from its attr map.
  function movementChannels(dev) {
    if (typeof getDeviceAttrAbsChannels !== "function") return { pan: null, tilt: null };
    const map = getDeviceAttrAbsChannels(dev) || {};
    let pan = null, tilt = null;
    for (const [key, ch] of Object.entries(map)) {
      const k = String(key).toLowerCase();
      if (pan === null && k.includes("pan")) pan = parseInt(ch, 10);
      if (tilt === null && k.includes("tilt")) tilt = parseInt(ch, 10);
    }
    return { pan: Number.isFinite(pan) ? pan : null, tilt: Number.isFinite(tilt) ? tilt : null };
  }

  function movementDevices() {
    const out = [];
    for (const dev of Object.values(rigDevices || {})) {
      if (!dev) continue;
      const ch = movementChannels(dev);
      if (ch.pan !== null || ch.tilt !== null) out.push({ dev, ch });
    }
    out.sort((a, b) => (parseInt(a.dev.id, 10) || 0) - (parseInt(b.dev.id, 10) || 0));
    return out;
  }

  // Push a fixture's home pan/tilt live so the user sees it aim while editing.
  // Attribute intents: the engine owns the channels.
  function liveAim(dev, ch) {
    if (!$c("calib-live-aim") || !$c("calib-live-aim").checked) return;
    if (typeof window.queueDeviceAttrs !== "function") return;
    const attrs = _positionAttrKeys(dev);
    const updates = [];
    if (attrs.pan && dev.home_pan != null) {
      updates.push({ device_id: String(dev.id), attr: attrs.pan, value: _clamp255(dev.home_pan) });
    }
    if (attrs.tilt && dev.home_tilt != null) {
      updates.push({ device_id: String(dev.id), attr: attrs.tilt, value: _clamp255(dev.home_tilt) });
    }
    if (updates.length) {
      try { window.queueDeviceAttrs(updates); } catch (e) {}
    }
  }

  // Finds the pan / tilt attribute keys of a fixture (group key first, then the
  // historic flat alias).
  function _positionAttrKeys(dev) {
    const out = { pan: null, tilt: null };
    const fi = (typeof fixtures === "object" && fixtures) ? (fixtures[dev.fixture] || {}) : {};
    const defs = (typeof getFixtureAttrDefinitions === "function")
      ? getFixtureAttrDefinitions(fi, { includeLegacy: true })
      : {};
    for (const def of Object.values(defs)) {
      const role = String(def?.role || def?.key || "").toLowerCase();
      if (!out.pan && role.endsWith("pan")) out.pan = def.key;
      if (!out.tilt && role.endsWith("tilt")) out.tilt = def.key;
    }
    return out;
  }

  function persist() {
    if (typeof invalidateDeviceAttrCache === "function") invalidateDeviceAttrCache();
    if (typeof scheduleRigSync === "function") scheduleRigSync();
  }

  function buildRows() {
    const tbody = $c("calib-tbody");
    const empty = $c("calib-empty");
    if (!tbody) return;
    tbody.innerHTML = "";
    const items = movementDevices();
    if (empty) empty.hidden = items.length > 0;
    for (const { dev, ch } of items) {
      const tr = document.createElement("tr");
      tr.dataset.deviceId = String(dev.id);

      const name = document.createElement("td");
      name.className = "calib-name";
      name.textContent = dev.cname || `Device ${dev.id}`;
      tr.appendChild(name);

      const mkNum = (kind) => {
        const td = document.createElement("td");
        const inp = document.createElement("input");
        inp.type = "number"; inp.min = 0; inp.max = 255; inp.className = "calib-num";
        const cur = kind === "pan" ? dev.home_pan : dev.home_tilt;
        inp.value = (cur == null) ? "" : cur;
        inp.placeholder = "128";
        inp.disabled = (kind === "pan" ? ch.pan === null : ch.tilt === null);
        inp.addEventListener("input", () => {
          const v = _clamp255(inp.value);
          if (kind === "pan") dev.home_pan = v; else dev.home_tilt = v;
          _lastEditedId = String(dev.id);
          liveAim(dev, ch);
        });
        inp.addEventListener("change", persist);
        td.appendChild(inp);
        return td;
      };
      tr.appendChild(mkNum("pan"));
      tr.appendChild(mkNum("tilt"));

      const mkInv = (kind) => {
        const td = document.createElement("td");
        td.className = "calib-inv";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = kind === "pan" ? !!dev.invert_pan : !!dev.invert_tilt;
        cb.addEventListener("change", () => {
          if (kind === "pan") dev.invert_pan = cb.checked; else dev.invert_tilt = cb.checked;
          _lastEditedId = String(dev.id);
          persist();
        });
        td.appendChild(cb);
        return td;
      };
      tr.appendChild(mkInv("pan"));
      tr.appendChild(mkInv("tilt"));

      tbody.appendChild(tr);
    }
  }

  // Read a device's current live pan/tilt from the last engine broadcast.
  function currentValue(uni, absCh) {
    try {
      const frame = (typeof lastDmxFrames !== "undefined") ? lastDmxFrames[uni] : null;
      if (frame && absCh != null && absCh >= 0 && absCh < frame.length) return frame[absCh];
    } catch (e) {}
    return null;
  }

  function captureSelected() {
    const sel = (typeof selectedDeviceOrder !== "undefined" && selectedDeviceOrder.length)
      ? selectedDeviceOrder : Object.keys(rigDevices || {});
    let n = 0;
    for (const id of sel) {
      const dev = rigDevices[id];
      if (!dev) continue;
      const ch = movementChannels(dev);
      if (ch.pan === null && ch.tilt === null) continue;
      const uni = parseInt(dev.universe, 10) || 0;
      if (ch.pan !== null) { const v = currentValue(uni, ch.pan); if (v != null) dev.home_pan = v; }
      if (ch.tilt !== null) { const v = currentValue(uni, ch.tilt); if (v != null) dev.home_tilt = v; }
      n++;
    }
    persist();
    buildRows();
    if (typeof toast === "function") toast(_ct("calib.captured", "Captured home for {n} fixture(s)").replace("{n}", n), "success");
  }

  function applyToSelection() {
    if (!_lastEditedId || !rigDevices[_lastEditedId]) {
      if (typeof toast === "function") toast(_ct("calib.editFirst", "Edit a row first, then apply to selection."), "info");
      return;
    }
    const src = rigDevices[_lastEditedId];
    const sel = (typeof selectedDeviceOrder !== "undefined" && selectedDeviceOrder.length)
      ? selectedDeviceOrder : [];
    if (!sel.length) {
      if (typeof toast === "function") toast(_ct("calib.selectFirst", "Select fixtures in the rig first."), "info");
      return;
    }
    let n = 0;
    for (const id of sel) {
      const dev = rigDevices[id];
      if (!dev || id === _lastEditedId) continue;
      const ch = movementChannels(dev);
      if (ch.pan === null && ch.tilt === null) continue;
      dev.home_pan = src.home_pan;
      dev.home_tilt = src.home_tilt;
      dev.invert_pan = src.invert_pan;
      dev.invert_tilt = src.invert_tilt;
      n++;
    }
    persist();
    buildRows();
    if (typeof toast === "function") toast(_ct("calib.applied", "Applied to {n} fixture(s)").replace("{n}", n), "success");
  }

  document.addEventListener("DOMContentLoaded", () => {
    const panel = $c("calib-panel");
    if (panel) {
      // Rebuild the list each time the panel is opened (rig may have changed).
      panel.addEventListener("toggle", () => { if (panel.open) buildRows(); });
    }
    $c("calib-capture-selected")?.addEventListener("click", captureSelected);
    $c("calib-apply-selected")?.addEventListener("click", applyToSelection);
  });

  // Let other modules refresh the table after a rig/project load.
  window.refreshCalibrationPanel = () => {
    const panel = $c("calib-panel");
    if (panel && panel.open) buildRows();
  };
})();
