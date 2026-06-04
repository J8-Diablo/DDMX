// static/project.js
// "Project" = a rig (devices + calibration) plus its cue lists, bundled into a
// single portable .ddmxproj file. The rig is OWNED by the project: switching
// cue list within a project does not redefine the rig (see the guard in
// cues.js -> loadCueFile, keyed on window.projectRigLocked).
//
// Depends on globals/functions defined in core.js / rig.js / cues.js:
//   rigDevices, deviceLocalValues, deviceCurrentGroups, selectedDeviceOrder,
//   selectedDeviceSet, nextDeviceId, cuesObj, virtualGroups, currentCueFilename,
//   selectedCueIndex, selectedCueIndices,
//   buildDevicesDefFromRig(), initDeviceDefaults(), syncRigToBackend(),
//   drawRig(), renderCueTable(), refreshControllerFromSelection(),
//   refreshCueFileList(), loadCueFile().

const PROJECT_SCHEMA = "ddmx.project/v1";
const PROJECT_EXT = ".ddmxproj";

let currentProjectFile = null;     // e.g. "MyShow.ddmxproj" (null = no named project)
window.projectRigLocked = false;   // true while a project owns the rig
// Cue lists belonging to the current project. The cue dropdown is scoped to
// this (not the raw cue/ dir): a blank / absent project shows no cue lists.
window.projectCueFiles = [];

function $proj(id) { return document.getElementById(id); }
function _pt(key, fallback) { return (typeof window.t === "function") ? window.t(key, fallback) : fallback; }
function _stripExt(name) { return String(name || "").replace(/\.ddmxproj$/i, ""); }

function updateProjectLabel() {
  const el = $proj("project-current-label");
  if (el) el.textContent = currentProjectFile ? _stripExt(currentProjectFile) : "";
}

// ---- Gather current state into a project document ---------------------------

async function _projectFetchCueLists() {
  // Only gather the cue lists that belong to THIS project (scoped), not every
  // file in the shared cue/ directory.
  const out = {};
  const files = Array.isArray(window.projectCueFiles) ? window.projectCueFiles : [];
  for (const f of files) {
    try {
      const cr = await fetch(`/api/cues/${encodeURIComponent(f)}`);
      if (cr.ok) out[f] = await cr.json();
    } catch (e) { /* skip unreadable cue */ }
  }
  return out;
}

async function buildProjectData(name) {
  const rigDefs = (typeof buildDevicesDefFromRig === "function") ? buildDevicesDefFromRig() : {};
  const cueLists = await _projectFetchCueLists();
  return {
    schema: PROJECT_SCHEMA,
    name: name || _stripExt(currentProjectFile) || "Untitled",
    saved_at: Date.now(),
    rig: {
      devices: rigDefs,
      next_device_id: (typeof nextDeviceId === "number" ? nextDeviceId : 1),
    },
    cue_lists: cueLists,
    active_cue_list: currentCueFilename || null,
  };
}

// ---- Apply a project document onto the running app --------------------------

function _resetFrontendRigState() {
  rigDevices = {};
  deviceLocalValues = {};
  if (typeof deviceCurrentGroups !== "undefined") deviceCurrentGroups = {};
  selectedDeviceOrder = [];
  selectedDeviceSet = new Set();
}

async function applyProjectData(data, file) {
  // 1) Wipe backend + frontend rig so nothing lingers from the previous project.
  try { await fetch("/api/rig/reset", { method: "POST" }); } catch (e) {}
  _resetFrontendRigState();

  // 2) Restore rig devices (with calibration) from the project.
  const rig = (data && data.rig) || {};
  const defs = rig.devices || {};
  let maxId = 0;
  for (const [rawId, dev] of Object.entries(defs)) {
    const parsed = parseInt(dev.id ?? rawId, 10);
    const id = String(Number.isFinite(parsed) ? parsed : rawId);
    if (Number.isFinite(parsed)) maxId = Math.max(maxId, parsed);
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
    if (typeof deviceCurrentGroups !== "undefined") deviceCurrentGroups[id] = new Set();
    if (typeof initDeviceDefaults === "function") initDeviceDefaults(id, dev.fixture);
  }
  nextDeviceId = Number.isFinite(rig.next_device_id) ? rig.next_device_id : (maxId + 1);

  // 3) Restore the project's cue lists into the cue store + scope the dropdown.
  const cueLists = (data && data.cue_lists) || {};
  window.projectCueFiles = Object.keys(cueLists);
  for (const [fname, content] of Object.entries(cueLists)) {
    try {
      await fetch(`/api/cues/${encodeURIComponent(fname)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(content),
      });
    } catch (e) { console.warn("[project] cue write failed", fname, e); }
  }

  // 4) The project now owns the rig.
  window.projectRigLocked = true;
  currentProjectFile = file || null;

  // 5) Push the rig to the backend (replace semantics) and redraw.
  if (typeof syncRigToBackend === "function") { try { await syncRigToBackend(true); } catch (e) {} }
  if (typeof drawRig === "function") drawRig();
  if (typeof refreshControllerFromSelection === "function") refreshControllerFromSelection();

  // 6) Refresh the cue dropdown and load the active cue list.
  currentCueFilename = null;
  if (typeof refreshCueFileList === "function") await refreshCueFileList();
  const active = (data && data.active_cue_list) || null;
  if (active && typeof loadCueFile === "function") {
    const sel = $proj("cue-file-select");
    if (sel) sel.value = active;
    await loadCueFile(active);
  }
  if (typeof window.refreshCalibrationPanel === "function") window.refreshCalibrationPanel();
  updateProjectLabel();
}

// ---- Menu actions -----------------------------------------------------------

async function projectNewBlank() {
  const ask = (typeof window.confirmModal === "function")
    ? window.confirmModal(_pt("project.confirmNew", "Start a new blank project? Unsaved changes will be lost."))
    : Promise.resolve(window.confirm(_pt("project.confirmNew", "Start a new blank project? Unsaved changes will be lost.")));
  const ok = await ask;
  if (!ok) return;

  try { await fetch("/api/rig/reset", { method: "POST" }); } catch (e) {}
  _resetFrontendRigState();
  nextDeviceId = 1;
  cuesObj = { loop: false, loop_count: null, devices_def: {}, virtual_groups: {}, sequence: [] };
  virtualGroups = {};
  currentCueFilename = null;
  currentProjectFile = null;
  window.projectCueFiles = [];      // blank project owns no cue lists
  selectedCueIndex = null;
  if (selectedCueIndices && typeof selectedCueIndices.clear === "function") selectedCueIndices.clear();
  window.projectRigLocked = true; // a (blank) project is active; rig is project-owned

  if (typeof syncRigToBackend === "function") { try { await syncRigToBackend(true); } catch (e) {} }
  if (typeof drawRig === "function") drawRig();
  if (typeof renderCueTable === "function") renderCueTable();
  if (typeof refreshCueFileList === "function") { try { await refreshCueFileList(); } catch (e) {} }
  if (typeof refreshControllerFromSelection === "function") refreshControllerFromSelection();
  if (typeof window.refreshCalibrationPanel === "function") window.refreshCalibrationPanel();
  updateProjectLabel();
  if (typeof toast === "function") toast(_pt("project.newDone", "New blank project — IDs reset to 1"), "success");
}

async function projectSave(asNew) {
  let file = currentProjectFile;
  if (asNew || !file) {
    const def = file ? _stripExt(file) : "MyShow";
    let name = (typeof window.promptModal === "function")
      ? await window.promptModal(_pt("project.saveAsTitle", "Project name"), def, "MyShow")
      : window.prompt(_pt("project.saveAsTitle", "Project name"), def);
    if (!name) return;
    name = _stripExt(String(name).trim());
    if (!name) return;
    file = name + PROJECT_EXT;
  }
  const data = await buildProjectData(_stripExt(file));
  try {
    const r = await fetch(`/api/projects/${encodeURIComponent(file)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    const res = await r.json();
    if (res && res.file) file = res.file;
    currentProjectFile = file;
    window.projectRigLocked = true;
    updateProjectLabel();
    if (typeof toast === "function") toast(_pt("project.saved", "Project saved") + ": " + file, "success");
  } catch (e) {
    console.error(e);
    if (typeof toast === "function") toast(_pt("project.saveFailed", "Save failed"), "error");
  }
}

async function projectLoadByName(file) {
  try {
    const r = await fetch(`/api/projects/${encodeURIComponent(file)}`);
    if (!r.ok) throw new Error("not found");
    const data = await r.json();
    await applyProjectData(data, file);
    if (typeof toast === "function") toast(_pt("project.loaded", "Project loaded") + ": " + file, "success");
  } catch (e) {
    console.error(e);
    if (typeof toast === "function") toast(_pt("project.loadFailed", "Load failed"), "error");
  }
}

function projectImport() {
  const inp = $proj("project-import-input");
  if (inp) inp.click();
}

async function projectHandleImportFile(ev) {
  const file = ev.target.files && ev.target.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    const data = JSON.parse(text);
    let fname = _stripExt(file.name.replace(/\.json$/i, "")) + PROJECT_EXT;
    // Persist to the server store, then apply.
    await fetch(`/api/projects/${encodeURIComponent(fname)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    await applyProjectData(data, fname);
    if (typeof toast === "function") toast(_pt("project.imported", "Project imported"), "success");
  } catch (e) {
    console.error(e);
    if (typeof toast === "function") toast(_pt("project.importFailed", "Import failed"), "error");
  } finally {
    ev.target.value = "";
  }
}

async function projectExport() {
  // Export the CURRENT state (client-side blob, independent of last save).
  const name = currentProjectFile ? _stripExt(currentProjectFile) : "project";
  const data = await buildProjectData(name);
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = currentProjectFile || (name + PROJECT_EXT);
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// ---- Menu UI ----------------------------------------------------------------

function _projectMenuItem(file, isRecent) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "project-item project-file";
  b.textContent = _stripExt(file);
  if (file === currentProjectFile) b.classList.add("active");
  b.addEventListener("click", () => {
    closeProjectMenu();
    projectLoadByName(file);
  });
  return b;
}

async function _populateProjectMenu() {
  const loadList = $proj("project-load-list");
  const recentList = $proj("project-recent-list");
  if (loadList) loadList.innerHTML = `<div class="project-empty muted">…</div>`;
  if (recentList) recentList.innerHTML = "";
  try {
    const r = await fetch("/api/projects");
    const data = await r.json();
    const files = data.files || [];
    const recent = data.recent || [];
    if (loadList) {
      loadList.innerHTML = "";
      if (!files.length) {
        loadList.innerHTML = `<div class="project-empty muted">${_pt("project.none", "No projects yet")}</div>`;
      } else {
        files.forEach((f) => loadList.appendChild(_projectMenuItem(f, false)));
      }
    }
    if (recentList) {
      if (!recent.length) {
        recentList.innerHTML = `<div class="project-empty muted">—</div>`;
      } else {
        recent.forEach((f) => recentList.appendChild(_projectMenuItem(f, true)));
      }
    }
  } catch (e) {
    if (loadList) loadList.innerHTML = `<div class="project-empty muted">${_pt("project.loadFailed", "Load failed")}</div>`;
  }
}

function openProjectMenu() {
  const dd = $proj("project-menu-dropdown");
  if (!dd) return;
  dd.classList.remove("hidden");
  _populateProjectMenu();
  setTimeout(() => document.addEventListener("click", _projectOutsideClick), 0);
}

function closeProjectMenu() {
  const dd = $proj("project-menu-dropdown");
  if (dd) dd.classList.add("hidden");
  document.removeEventListener("click", _projectOutsideClick);
}

function _projectOutsideClick(ev) {
  const menu = $proj("project-menu");
  if (menu && !menu.contains(ev.target)) closeProjectMenu();
}

function toggleProjectMenu() {
  const dd = $proj("project-menu-dropdown");
  if (!dd) return;
  if (dd.classList.contains("hidden")) openProjectMenu();
  else closeProjectMenu();
}

document.addEventListener("DOMContentLoaded", () => {
  const btn = $proj("project-menu-btn");
  if (btn) btn.addEventListener("click", (e) => { e.stopPropagation(); toggleProjectMenu(); });

  const dd = $proj("project-menu-dropdown");
  if (dd) {
    dd.querySelectorAll("[data-project-action]").forEach((el) => {
      el.addEventListener("click", () => {
        const action = el.getAttribute("data-project-action");
        closeProjectMenu();
        if (action === "new") projectNewBlank();
        else if (action === "save") projectSave(false);
        else if (action === "saveas") projectSave(true);
        else if (action === "import") projectImport();
        else if (action === "export") projectExport();
      });
    });
  }

  const imp = $proj("project-import-input");
  if (imp) imp.addEventListener("change", projectHandleImportFile);

  // The app boots into an implicit BLANK project (no project open): empty rig
  // owned by the project, no cue lists. The user opens/imports a project or
  // builds one and saves it. The cue dropdown is populated by the boot
  // refreshCueFileList() from window.projectCueFiles (empty here).
  currentProjectFile = null;
  window.projectCueFiles = [];
  window.projectRigLocked = true;
  updateProjectLabel();
});

// Expose for other modules / debugging.
window.projectNewBlank = projectNewBlank;
window.projectSave = projectSave;
window.projectExport = projectExport;
window.applyProjectData = applyProjectData;
