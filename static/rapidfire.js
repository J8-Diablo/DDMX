// static/rapidfire.js
// Rapid Fire — a grid of launch pads, one per cue list of the active project.
// One click fires the list; each pad carries its own play/pause + stop.
//
// Pads always run as a CUE LIST (sequential, from step 1), never through the
// timeline pipeline, whatever the file carries. The panel-level "Loop" option
// repeats the list until it is stopped.
//
// Firing a pad reuses the normal paths: stop current playback → load the cue
// file → start backend playback. So the rig, the virtual groups and the editor
// all stay in sync with what is playing, exactly as if the operator had picked
// the file in the dropdown and pressed Play.
(function () {
  // { filename: { steps, durationMs, loop, loopCount, error } }
  const metaCache = new Map();
  let padFiles = [];
  let firing = false;
  let renderChain = Promise.resolve();

  function tr(key, fallback) {
    return (typeof window.t === "function") ? window.t(key, fallback) : fallback;
  }

  function cueEditorSettings() {
    return (typeof window.getCueEditorSettings === "function") ? window.getCueEditorSettings() : null;
  }

  function isRapidFireMode() {
    return String(cueEditorSettings()?.view_mode || "") === "rapidfire";
  }

  // Loop is a panel-level option, persisted with the other cue-editor settings.
  function isLoopEnabled() {
    return Boolean(cueEditorSettings()?.rapidfire_loop);
  }

  function setLoopEnabled(enabled) {
    const settings = cueEditorSettings();
    if (!settings || typeof window.setCueEditorSettings !== "function") return;
    window.setCueEditorSettings({ ...settings, rapidfire_loop: Boolean(enabled) }, { persist: true });
    syncLoopToggle();
  }

  function syncLoopToggle() {
    const toggle = document.getElementById("rapidfire-loop");
    if (toggle) toggle.checked = isLoopEnabled();
  }

  function padRoot() {
    return document.getElementById("rapidfire-pads");
  }

  function firstNumber(value) {
    const match = String(value ?? "").match(/-?\d+(\.\d+)?/);
    return match ? Math.max(0, parseFloat(match[0])) : 0;
  }

  // Pads always run the file as a cue list, so the estimate is the sequential
  // one: the sum of every step's sleep + fade (both can be spread patterns like
  // "500 > 5000", hence the leading number only — it is a rough figure).
  function analyzeCueFile(data) {
    const sequence = Array.isArray(data?.sequence) ? data.sequence : [];
    let approx = 0;
    for (const step of sequence) {
      if (!step || typeof step !== "object") continue;
      approx += firstNumber(step.sleep) + firstNumber(step.duration);
    }
    return {
      steps: sequence.length,
      durationMs: approx,
      loop: Boolean(data?.loop),
      loopCount: Number.isFinite(Number(data?.loop_count)) ? Number(data.loop_count) : null,
      error: null,
    };
  }

  function formatDuration(ms) {
    const value = Math.max(0, Math.round(Number(ms) || 0));
    if (value < 1000) return `${value}ms`;
    const total = value / 1000;
    const minutes = Math.floor(total / 60);
    const seconds = Math.round(total % 60);
    if (minutes <= 0) return `${seconds}s`;
    return `${minutes}m${String(seconds).padStart(2, "0")}`;
  }

  function displayName(filename) {
    return String(filename || "").replace(/\.json$/i, "");
  }

  async function listProjectCueFiles() {
    // Exactly the same scoping rule as the cue dropdown, resolver included, so
    // the grid can never show a different set than the list.
    try {
      if (typeof window.resolveProjectCueFiles === "function") {
        return await window.resolveProjectCueFiles();
      }
      return [];
    } catch (err) {
      console.warn("[RAPIDFIRE] cue file list failed:", err);
      return [];
    }
  }

  async function loadMeta(files) {
    const missing = files.filter((f) => !metaCache.has(f));
    if (!missing.length) return;
    await Promise.all(missing.map(async (file) => {
      try {
        const res = await fetch(`/api/cues/${encodeURIComponent(file)}`, { cache: "no-store" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        metaCache.set(file, analyzeCueFile(await res.json()));
      } catch (err) {
        console.warn(`[RAPIDFIRE] could not read ${file}:`, err);
        metaCache.set(file, {
          steps: 0, durationMs: 0, loop: false, loopCount: null,
          error: String(err?.message || err),
        });
      }
    }));
  }

  // ---------------------------------------------------------------- playback

  function padState(file) {
    if (currentCueFilename !== file) return "idle";
    if (backendPlaybackStarting) return "starting";
    if (!playbackActive) return "idle";
    return playbackPaused ? "paused" : "playing";
  }

  async function firePad(file) {
    if (firing) return;
    const meta = metaCache.get(file);
    if (!meta || meta.error) {
      toast(tr("cues.rapidFireUnreadable", "This cue list could not be read."), "error");
      return;
    }
    if (!meta.steps) {
      toast(tr("cues.rapidFireEmptyList", "This cue list is empty."), "warning");
      return;
    }

    firing = true;
    refreshPadStates();
    try {
      if (playbackActive || backendPlaybackStarting) {
        await stopRun(true);
      }
      if (currentCueFilename !== file) {
        await loadCueFile(file);
      }
      const sequence = (cuesObj && Array.isArray(cuesObj.sequence)) ? cuesObj.sequence : [];
      if (!sequence.length) {
        toast(tr("cues.rapidFireEmptyList", "This cue list is empty."), "warning");
        return;
      }
      if (typeof startEffectRenderLoop === "function") startEffectRenderLoop();
      // Pads always run as a cue list, from step 1 — never through the timeline
      // pipeline, whatever the file carries and whatever view is active.
      // Loop: the panel toggle repeats forever; otherwise the cue list's own
      // loop / loop_count applies.
      const forced = isLoopEnabled();
      await runBackendSequence(sequence, 0, {
        timeline: false,
        loop: forced || Boolean(meta.loop),
        loopCount: forced ? 0 : (meta.loop ? (meta.loopCount || 0) : 0),
      });
      const suffix = (forced || meta.loop) ? ` (${tr("cues.rapidFireLoop", "loop")})` : "";
      toast(`${tr("cues.rapidFireFired", "Fired")} ${displayName(file)}${suffix}`, "info");
    } catch (err) {
      console.error("[RAPIDFIRE]", err);
      toast(tr("cues.rapidFireFailed", "Rapid Fire launch failed"), "error");
    } finally {
      firing = false;
      refreshPadStates();
    }
  }

  async function togglePad(file) {
    const state = padState(file);
    if (state === "playing" || state === "paused") {
      try {
        await controlBackendPlayback(state === "playing" ? "pause" : "resume");
      } catch (err) {
        console.warn("[RAPIDFIRE] pause/resume failed:", err);
        toast(tr("cues.rapidFireControlFailed", "Playback control failed"), "error");
      }
      refreshPadStates();
      return;
    }
    await firePad(file);
  }

  async function stopPlayback() {
    if (!playbackActive && !backendPlaybackStarting) return;
    try {
      await stopRun(false);
    } catch (err) {
      console.warn("[RAPIDFIRE] stop failed:", err);
    }
    refreshPadStates();
  }

  // ------------------------------------------------------------------ render

  function buildPad(file) {
    const meta = metaCache.get(file) || {};
    const pad = document.createElement("div");
    pad.className = "rapidfire-pad";
    pad.dataset.file = file;

    const fire = document.createElement("button");
    fire.type = "button";
    fire.className = "rapidfire-pad-fire";
    fire.dataset.role = "fire";

    const title = document.createElement("span");
    title.className = "rapidfire-pad-title";
    title.textContent = displayName(file);
    fire.appendChild(title);

    const sub = document.createElement("span");
    sub.className = "rapidfire-pad-sub";
    if (meta.error) {
      sub.textContent = tr("cues.rapidFireUnreadable", "This cue list could not be read.");
    } else {
      const bits = [`${meta.steps || 0} ${tr("cues.rapidFireCuesShort", "cues")}`];
      if (meta.durationMs > 0) bits.push(`~${formatDuration(meta.durationMs)}`);
      if (meta.loop) {
        bits.push(meta.loopCount
          ? `${tr("cues.rapidFireLoop", "loop")} x${meta.loopCount}`
          : tr("cues.rapidFireLoop", "loop"));
      }
      sub.textContent = bits.join(" · ");
    }
    fire.appendChild(sub);
    pad.appendChild(fire);

    const controls = document.createElement("div");
    controls.className = "rapidfire-pad-controls";

    const play = document.createElement("button");
    play.type = "button";
    play.className = "rapidfire-pad-btn rapidfire-pad-play";
    play.dataset.role = "play";
    controls.appendChild(play);

    const stop = document.createElement("button");
    stop.type = "button";
    stop.className = "rapidfire-pad-btn rapidfire-pad-stop";
    stop.dataset.role = "stop";
    stop.textContent = tr("cues.rapidFireStopShort", "Stop");
    controls.appendChild(stop);

    pad.appendChild(controls);
    return pad;
  }

  function refreshPadStates() {
    // Called from updatePlaybackUI (up to 10x/s during playback): do nothing
    // when the grid isn't the visible view.
    if (!isRapidFireMode()) return;
    const root = padRoot();
    if (!root) return;
    const anyActive = Boolean(playbackActive || backendPlaybackStarting);

    root.querySelectorAll(".rapidfire-pad").forEach((pad) => {
      const file = pad.dataset.file;
      const meta = metaCache.get(file) || {};
      const state = padState(file);
      const play = pad.querySelector('[data-role="play"]');
      const stop = pad.querySelector('[data-role="stop"]');
      const fire = pad.querySelector('[data-role="fire"]');

      pad.classList.toggle("is-playing", state === "playing");
      pad.classList.toggle("is-paused", state === "paused");
      pad.classList.toggle("is-starting", state === "starting");
      pad.classList.toggle("is-loaded", currentCueFilename === file);
      pad.classList.toggle("is-broken", Boolean(meta.error) || !meta.steps);

      const unusable = Boolean(meta.error) || !meta.steps;
      if (play) {
        if (state === "playing") play.textContent = tr("cues.rapidFirePause", "Pause");
        else if (state === "paused") play.textContent = tr("cues.rapidFireResume", "Resume");
        else if (state === "starting") play.textContent = tr("cues.rapidFireStarting", "Starting…");
        else play.textContent = tr("cues.rapidFirePlay", "Play");
        play.disabled = unusable || firing || state === "starting";
      }
      if (stop) {
        stop.disabled = !(state === "playing" || state === "paused");
      }
      if (fire) {
        fire.disabled = unusable;
        fire.title = unusable ? "" : displayName(file);
      }
    });

    const stopAll = document.getElementById("rapidfire-stop-all");
    if (stopAll) stopAll.disabled = !anyActive;
  }

  function renderStatus(count) {
    const el = document.getElementById("rapidfire-status");
    if (!el) return;
    if (!Array.isArray(window.projectCueFiles) || !window.projectCueFiles.length) {
      el.textContent = tr("cues.rapidFireNoProject", "Open a project to see its cue lists here.");
      return;
    }
    el.textContent = `${count} ${count === 1 ? tr("cues.rapidFireListOne", "cue list") : tr("cues.rapidFireListMany", "cue lists")}`;
  }

  async function doRender() {
    const root = padRoot();
    if (!root || !isRapidFireMode()) return;

    padFiles = await listProjectCueFiles();
    await loadMeta(padFiles);

    root.innerHTML = "";
    if (!padFiles.length) {
      const empty = document.createElement("div");
      empty.className = "rapidfire-empty muted";
      empty.textContent = (Array.isArray(window.projectCueFiles) && window.projectCueFiles.length)
        ? tr("cues.rapidFireEmpty", "No cue list in this project yet.")
        : tr("cues.rapidFireNoProject", "Open a project to see its cue lists here.");
      root.appendChild(empty);
    } else {
      const frag = document.createDocumentFragment();
      for (const file of padFiles) frag.appendChild(buildPad(file));
      root.appendChild(frag);
    }
    renderStatus(padFiles.length);
    syncLoopToggle();
    refreshPadStates();
  }

  // renderCueTable() calls this on every cue edit and on every view switch, so
  // renders are chained (never overlapped) and the metadata is cached per file.
  // `reload` drops the cache even when the grid is hidden, so switching into
  // Rapid Fire after a save always shows fresh content.
  function renderRapidFireGrid(options = {}) {
    if (options && options.reload) metaCache.clear();
    renderChain = renderChain
      .then(doRender)
      .catch((err) => console.warn("[RAPIDFIRE] render failed:", err));
    return renderChain;
  }

  // ----------------------------------------------------------------- binding

  function bind() {
    const root = padRoot();
    if (root && root.dataset.bound !== "1") {
      root.dataset.bound = "1";
      root.addEventListener("click", (ev) => {
        const target = ev.target instanceof HTMLElement ? ev.target : null;
        const actionEl = target?.closest("[data-role]");
        const pad = target?.closest(".rapidfire-pad");
        if (!pad || !actionEl) return;
        const file = pad.dataset.file;
        if (!file) return;
        const role = actionEl.dataset.role;
        if (role === "stop") {
          stopPlayback();
        } else {
          togglePad(file);
        }
      });
    }

    const refresh = document.getElementById("rapidfire-refresh");
    if (refresh && refresh.dataset.bound !== "1") {
      refresh.dataset.bound = "1";
      refresh.addEventListener("click", () => renderRapidFireGrid({ reload: true }));
    }

    const stopAll = document.getElementById("rapidfire-stop-all");
    if (stopAll && stopAll.dataset.bound !== "1") {
      stopAll.dataset.bound = "1";
      stopAll.addEventListener("click", () => stopPlayback());
    }

    const loopToggle = document.getElementById("rapidfire-loop");
    if (loopToggle && loopToggle.dataset.bound !== "1") {
      loopToggle.dataset.bound = "1";
      loopToggle.addEventListener("change", () => setLoopEnabled(loopToggle.checked));
    }
    syncLoopToggle();
  }

  window.isRapidFireMode = isRapidFireMode;
  window.renderRapidFireGrid = renderRapidFireGrid;
  window.refreshRapidFirePads = refreshPadStates;
  // Called when a cue file is written or the project changes: drop the cached
  // metadata so the next render picks up the new content.
  window.invalidateRapidFireCache = function invalidateRapidFireCache(file) {
    if (file) metaCache.delete(file);
    else metaCache.clear();
    if (isRapidFireMode()) renderRapidFireGrid();
  };

  document.addEventListener("DOMContentLoaded", () => {
    bind();
    if (isRapidFireMode()) renderRapidFireGrid();
  });

  // Pad labels are built in JS, so they need a rebuild when the catalogue lands
  // (the first render usually happens before the language file is fetched).
  document.addEventListener("i18n:applied", () => {
    if (isRapidFireMode()) renderRapidFireGrid();
  });
})();
