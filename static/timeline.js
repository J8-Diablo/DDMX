(function () {
  const DEFAULT_SETTINGS = {
    view_mode: "classic",
    timeline_priority_mode: "top",
    zoom_x: 120,
    zoom_y: 88,
    // Rapid Fire: fire pads on repeat until stopped.
    rapidfire_loop: false,
  };
  // Cue panel views: classic table, timeline, and Rapid Fire launch pads.
  const VIEW_MODES = ["classic", "timeline", "rapidfire"];
  const MIN_BLOCK_MS = 100;
  const TIMELINE_PERSIST_DEBOUNCE_MS = 250;
  const TIMELINE_SNAP_MS = 25;
  const TIMELINE_MANUAL_PREVIEW_INTERVAL_MS = 80;
  const OPERATOR_OPTIONS = ["", "|", "<", ">", "<>", "><", "||", "?"];

  let cueEditorSettings = { ...DEFAULT_SETTINGS };
  let settingsPersistTimer = null;
  let occurrencesCache = [];
  let timelineCursorMs = 0;
  let timelineViewState = { scroll_left: 0, scroll_top: 0 };
  let timelineMetrics = { duration_ms: 2000, width_px: 1000, lane_count: 2, height_px: 176 };
  let dragState = null;
  let timelinePreviewTimer = null;
  let timelinePreviewPendingMs = null;
  let timelinePreviewInFlight = false;
  let timelineManualPreviewTimer = null;
  let timelineManualPreviewActive = false;
  let timelineBackendPreviewPromise = null;
  let timelineBackendPreviewPrimed = false;
  let timelineBackendScrubSessionActive = false;
  let timelinePreviewStateBackup = null;
  let controllerLayoutAnchor = null;
  let controllerOriginalParent = null;
  let controllerOriginalNext = null;

  function cueEditorRoot() {
    return document.body;
  }

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function snapMs(value) {
    // Snap to the grid WITHOUT clamping the sign: this runs on drag *deltas*
    // (which are negative when dragging a clip earlier in time), so clamping to
    // >= 0 here would silently forbid moving / growing a clip into the past.
    // Absolute-time callers clamp to [0, duration] themselves.
    return Math.round(Number(value || 0) / TIMELINE_SNAP_MS) * TIMELINE_SNAP_MS;
  }

  function safeNumber(value, fallback) {
    const num = Number(value);
    return Number.isFinite(num) ? num : fallback;
  }

  function escapeHtml(text) {
    return String(text ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function normalizeCueEditorSettings(raw) {
    const next = { ...DEFAULT_SETTINGS, ...(raw && typeof raw === "object" ? raw : {}) };
    const mode = String(next.view_mode || "classic").trim().toLowerCase();
    next.view_mode = VIEW_MODES.includes(mode) ? mode : "classic";
    next.timeline_priority_mode = ["top", "bottom", "merge"].includes(String(next.timeline_priority_mode || "").trim().toLowerCase())
      ? String(next.timeline_priority_mode || "").trim().toLowerCase()
      : "top";
    next.zoom_x = clamp(safeNumber(next.zoom_x, DEFAULT_SETTINGS.zoom_x), 20, 480);
    next.zoom_y = clamp(safeNumber(next.zoom_y, DEFAULT_SETTINGS.zoom_y), 48, 240);
    next.rapidfire_loop = Boolean(next.rapidfire_loop);
    return next;
  }

  function isTimelineEditorMode() {
    return cueEditorSettings.view_mode === "timeline";
  }

  function getCueEditorSettings() {
    return { ...cueEditorSettings };
  }

  function updateTimelineZoomInputs() {
    const zoomX = document.getElementById("timeline-zoom-x");
    const zoomY = document.getElementById("timeline-zoom-y");
    const zoomXValue = document.getElementById("timeline-zoom-x-value");
    const zoomYValue = document.getElementById("timeline-zoom-y-value");
    if (zoomX) zoomX.value = String(cueEditorSettings.zoom_x);
    if (zoomY) zoomY.value = String(cueEditorSettings.zoom_y);
    if (zoomXValue) zoomXValue.textContent = `${Math.round(cueEditorSettings.zoom_x)} px/s`;
    if (zoomYValue) zoomYValue.textContent = `${Math.round(cueEditorSettings.zoom_y)} px`;
  }

  function ensureControllerLayoutAnchors() {
    const controller = document.querySelector(".controller-panel");
    if (!controller) return null;
    if (!controllerOriginalParent) {
      controllerOriginalParent = controller.parentNode;
      controllerOriginalNext = controller.nextSibling;
    }
    if (!controllerLayoutAnchor) {
      controllerLayoutAnchor = document.createComment("controller-layout-anchor");
      if (controllerOriginalParent) {
        controllerOriginalParent.insertBefore(controllerLayoutAnchor, controllerOriginalNext);
      }
    }
    return controller;
  }

  function updateViewToggle() {
    const active = cueEditorSettings.view_mode;
    [["cue-view-classic", "classic"], ["cue-view-timeline", "timeline"], ["cue-view-rapidfire", "rapidfire"]]
      .forEach(([id, mode]) => {
        const btn = document.getElementById(id);
        if (btn) btn.classList.toggle("active", active === mode);
      });
  }

  function applyCueEditorLayout() {
    const body = cueEditorRoot();
    const mainGrid = document.querySelector(".main-grid");
    const controller = ensureControllerLayoutAnchors();
    updateViewToggle();
    if (!body || !mainGrid || !controller) return;

    const timelineMode = isTimelineEditorMode();
    body.classList.toggle("cue-editor-mode-timeline", timelineMode);
    body.classList.toggle("cue-editor-mode-rapidfire", cueEditorSettings.view_mode === "rapidfire");

    if (timelineMode) {
      if (controller.parentNode !== mainGrid) {
        mainGrid.appendChild(controller);
      }
    } else if (controllerOriginalParent) {
      if (controllerLayoutAnchor && controllerLayoutAnchor.parentNode === controllerOriginalParent) {
        controllerOriginalParent.insertBefore(controller, controllerLayoutAnchor);
      } else if (controllerOriginalNext && controllerOriginalNext.parentNode === controllerOriginalParent) {
        controllerOriginalParent.insertBefore(controller, controllerOriginalNext);
      } else {
        controllerOriginalParent.appendChild(controller);
      }
    }

    window.requestAnimationFrame(() => {
      if (typeof window.applyLayoutSplit === "function") window.applyLayoutSplit();
      if (typeof window.updateRigCanvasSize === "function") window.updateRigCanvasSize();
      if (typeof window.drawRig === "function") window.drawRig();
    });
  }

  function scheduleCueEditorSettingsPersist() {
    if (settingsPersistTimer != null) {
      window.clearTimeout(settingsPersistTimer);
    }
    settingsPersistTimer = window.setTimeout(async () => {
      settingsPersistTimer = null;
      try {
        await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ cue_editor: getCueEditorSettings() }),
        });
      } catch (err) {
        console.warn("[TIMELINE] cue editor settings save failed:", err);
      }
    }, TIMELINE_PERSIST_DEBOUNCE_MS);
  }

  function setCueEditorSettings(rawSettings, options = {}) {
    const previousMode = cueEditorSettings.view_mode;
    cueEditorSettings = normalizeCueEditorSettings(rawSettings);
    if (previousMode !== cueEditorSettings.view_mode) {
      resetTimelineInteractionState({ stop_scrub_session: previousMode === "timeline" || cueEditorSettings.view_mode === "timeline" });
    }
    applyCueEditorLayout();
    updateTimelineZoomInputs();
    if (typeof window.renderCueTable === "function") {
      window.renderCueTable();
    }
    if (options.persist) {
      scheduleCueEditorSettingsPersist();
    }
  }

  function parseDurationMeta(durationRaw) {
    const text = String(durationRaw || "0").trim();
    const operators = ["<>", "><", "||", "|", ">", "<", "?"];
    let operator = "";
    let baseMs = 0;
    let spreadMs = 0;

    for (const candidate of operators) {
      if (text.includes(candidate)) {
        operator = candidate;
        const parts = text.split(candidate);
        baseMs = Math.max(0, parseInt(parts[0], 10) || 0);
        spreadMs = Math.max(0, parseInt(parts[1], 10) || 0);
        return {
          operator,
          base_ms: baseMs,
          spread_ms: spreadMs,
          total_ms: baseMs + spreadMs,
        };
      }
    }

    baseMs = Math.max(0, parseInt(text, 10) || 0);
    return {
      operator: "",
      base_ms: baseMs,
      spread_ms: 0,
      total_ms: baseMs,
    };
  }

  function stepFade(step) {
    return typeof window.stepFadeField === "function" ? window.stepFadeField(step) : (step?.fade ?? "0");
  }

  function stepHold(step) {
    return typeof window.stepHoldMs === "function" ? window.stepHoldMs(step) : 0;
  }

  // A block IS the cue: it lasts fade + duration and its fade-in is the fade.
  // Those two come from the step and are re-derived every time, so editing in
  // one view can never leave the other describing something else. The timeline
  // owns only what the classic list has no word for: position and lane.
  function ensureStepTimeline(step, fallbackStartMs = 0) {
    if (!step || typeof step !== "object") return null;
    if (!step.timeline || typeof step.timeline !== "object") {
      step.timeline = {};
    }
    const meta = step.timeline;
    const fadeMeta = parseDurationMeta(stepFade(step));
    const holdMs = stepHold(step);

    meta.lane = Math.max(0, parseInt(meta.lane, 10) || 0);
    meta.start_ms = Math.max(0, parseInt(meta.start_ms, 10) || Math.max(0, fallbackStartMs));
    meta.length_ms = Math.max(MIN_BLOCK_MS, fadeMeta.total_ms + holdMs);
    meta.fade_in_ms = clamp(fadeMeta.total_ms, 0, meta.length_ms);
    meta.fade_operator = fadeMeta.operator;
    // The out fade has no equivalent in the classic list (there, the next cue's
    // crossfade takes the look away), so it stays a timeline-only refinement.
    meta.fade_out_ms = clamp(parseInt(meta.fade_out_ms, 10) || 0, 0, Math.max(0, meta.length_ms - meta.fade_in_ms));
    // Legacy single-ramp fields, kept coherent for the engine's older path.
    meta.fade_start_ms = 0;
    meta.fade_end_ms = meta.fade_in_ms;
    return meta;
  }

  // Editing a clip writes back into the step, which is where time lives.
  function applyBlockLengthToStep(step, lengthMs) {
    if (!step || typeof step !== "object") return;
    const fadeMeta = parseDurationMeta(stepFade(step));
    step.duration = Math.max(0, Math.round(lengthMs - fadeMeta.total_ms));
  }

  function applyBlockOperatorToStep(step, operator) {
    if (!step || typeof step !== "object") return;
    const fadeMeta = parseDurationMeta(stepFade(step));
    const op = OPERATOR_OPTIONS.includes(String(operator || "").trim()) ? String(operator).trim() : "";
    // Dropping the spread keeps the same total fade for everybody, so the clip
    // does not change length under the operator's feet.
    step.fade = op
      ? `${fadeMeta.base_ms} ${op} ${fadeMeta.spread_ms}`
      : String(fadeMeta.base_ms + fadeMeta.spread_ms);
  }

  function applyBlockFadeToStep(step, fadeInMs) {
    if (!step || typeof step !== "object") return;
    const fadeMeta = parseDurationMeta(stepFade(step));
    const base = Math.max(0, Math.round(fadeInMs - fadeMeta.spread_ms));
    // Keep the spread and its operator: only the fade time itself is dragged.
    step.fade = fadeMeta.operator
      ? `${base} ${fadeMeta.operator} ${fadeMeta.spread_ms}`
      : String(base);
  }

  function syncStepTimelineBounds(step) {
    // Everything but the position is derived, so re-deriving is the whole job.
    ensureStepTimeline(step);
  }

  // Continuous Timeline -> CueList sync: keep each step's classic sleep/duration
  // consistent with its timeline block so both views (and classic playback)
  // stay in agreement. Ordering is by block start time.
  // Timeline -> cue list. There is almost nothing left to translate: fade and
  // duration already live in the step, and the block is derived from them. Only
  // the dead time before the first block has no step to live in.
  //
  // What the classic list still cannot express -- overlapping cues, holes
  // between them -- is not silently flattened into a wait any more; it is
  // reported by cueListTimeIssues() and the user is asked what to do.
  function syncSequenceFromTimeline() {
    const seq = Array.isArray(cuesObj?.sequence) ? cuesObj.sequence : [];
    let firstStart = null;
    for (const step of seq) {
      const meta = ensureStepTimeline(step);
      if (!meta) continue;
      if (firstStart === null || meta.start_ms < firstStart) firstStart = meta.start_ms;
    }
    if (cuesObj) cuesObj.lead_in_ms = Math.max(0, Math.round(firstStart || 0));
  }

  function rebuildLinearTimelineFromSequence() {
    const seq = Array.isArray(cuesObj?.sequence) ? cuesObj.sequence : [];
    let cursorMs = 0;
    let index = 0;

    while (index < seq.length) {
      const step = seq[index];
      if (!step || typeof step !== "object") {
        index += 1;
        continue;
      }

      if (step.loopGroup) {
        const groupId = step.loopGroup;
        const groupStart = index;
        let groupEnd = index;
        while (groupEnd + 1 < seq.length && seq[groupEnd + 1]?.loopGroup === groupId) {
          groupEnd += 1;
        }

        let localCursorMs = cursorMs;
        let groupMinStartMs = Number.POSITIVE_INFINITY;
        let groupMaxEndMs = 0;
        for (let blockIndex = groupStart; blockIndex <= groupEnd; blockIndex += 1) {
          const groupStep = seq[blockIndex];
          const hasTimeline = groupStep.timeline && typeof groupStep.timeline === "object" && Number.isFinite(Number(groupStep.timeline.start_ms));
          const meta = ensureStepTimeline(groupStep, localCursorMs);
          if (!hasTimeline) {
            meta.start_ms = Math.max(0, localCursorMs);
          }
          syncStepTimelineBounds(groupStep);
          groupMinStartMs = Math.min(groupMinStartMs, meta.start_ms);
          groupMaxEndMs = Math.max(groupMaxEndMs, meta.start_ms + meta.length_ms);
          localCursorMs = Math.max(localCursorMs, meta.start_ms + meta.length_ms);
        }

        const groupSpanMs = Math.max(
          MIN_BLOCK_MS,
          groupMaxEndMs - (Number.isFinite(groupMinStartMs) ? groupMinStartMs : cursorMs)
        );
        const loopCount = Math.max(1, parseInt(step.loopCount, 10) || 1);
        cursorMs = (Number.isFinite(groupMinStartMs) ? groupMinStartMs : cursorMs) + groupSpanMs * loopCount;
        index = groupEnd + 1;
        continue;
      }

      const hasTimeline = step.timeline && typeof step.timeline === "object" && Number.isFinite(Number(step.timeline.start_ms));
      const meta = ensureStepTimeline(step, cursorMs);
      if (!hasTimeline) {
        // Never placed by hand: lay the cues back to back, which is exactly
        // what the classic list plays.
        meta.start_ms = Math.max(0, cursorMs);
      }
      syncStepTimelineBounds(step);
      cursorMs = Math.max(cursorMs, meta.start_ms + meta.length_ms);
      index += 1;
    }
  }

  function ensureTimelineData() {
    rebuildLinearTimelineFromSequence();
    for (const step of cuesObj?.sequence || []) {
      syncStepTimelineBounds(step);
    }
  }

  function getLoopGroupRange(sequence, startIndex) {
    const step = sequence[startIndex];
    if (!step?.loopGroup) return null;
    const groupId = step.loopGroup;
    let end = startIndex;
    while (end + 1 < sequence.length && sequence[end + 1]?.loopGroup === groupId) end += 1;
    return { groupId, start: startIndex, end };
  }

  function getLoopGroupSpan(sequence, start, end) {
    let minStart = Number.POSITIVE_INFINITY;
    let maxEnd = 0;
    for (let index = start; index <= end; index += 1) {
      const meta = ensureStepTimeline(sequence[index]);
      minStart = Math.min(minStart, meta.start_ms);
      maxEnd = Math.max(maxEnd, meta.start_ms + meta.length_ms);
    }
    return {
      min_start_ms: Number.isFinite(minStart) ? minStart : 0,
      span_ms: Math.max(MIN_BLOCK_MS, maxEnd - (Number.isFinite(minStart) ? minStart : 0)),
    };
  }

  function buildTimelineOccurrences() {
    ensureTimelineData();
    const sequence = Array.isArray(cuesObj?.sequence) ? cuesObj.sequence : [];
    const occurrences = [];
    let index = 0;
    let planIndex = 0;

    while (index < sequence.length) {
      const step = sequence[index];
      if (!step || typeof step !== "object") {
        index += 1;
        continue;
      }

      if (step.loopGroup) {
        const group = getLoopGroupRange(sequence, index);
        const { min_start_ms, span_ms } = getLoopGroupSpan(sequence, group.start, group.end);
        const loopCount = Math.max(1, parseInt(step.loopCount, 10) || 1);

        for (let iteration = 0; iteration < loopCount; iteration += 1) {
          for (let sourceIndex = group.start; sourceIndex <= group.end; sourceIndex += 1) {
            const sourceStep = sequence[sourceIndex];
            const meta = ensureStepTimeline(sourceStep);
            const absoluteStartMs = meta.start_ms + iteration * span_ms;
            occurrences.push({
              id: `${group.groupId}:${sourceIndex}:${iteration}`,
              plan_index: planIndex++,
              source_index: sourceIndex,
              iteration,
              group_id: group.groupId,
              group_span_ms: span_ms,
              group_origin_ms: min_start_ms,
              lane: meta.lane,
              start_ms: absoluteStartMs,
              length_ms: meta.length_ms,
              end_ms: absoluteStartMs + meta.length_ms,
              fade_start_ms: meta.fade_start_ms,
              fade_end_ms: meta.fade_end_ms,
              fade_in_ms: meta.fade_in_ms,
              fade_out_ms: meta.fade_out_ms,
              fade_operator: meta.fade_operator || "",
              cue_name: sourceStep.name || `Cue ${sourceIndex + 1}`,
              cue: sourceStep,
            });
          }
        }

        index = group.end + 1;
        continue;
      }

      const meta = ensureStepTimeline(step);
      occurrences.push({
        id: `cue:${index}`,
        plan_index: planIndex++,
        source_index: index,
        iteration: 0,
        group_id: "",
        group_span_ms: 0,
        group_origin_ms: meta.start_ms,
        lane: meta.lane,
        start_ms: meta.start_ms,
        length_ms: meta.length_ms,
        end_ms: meta.start_ms + meta.length_ms,
        fade_start_ms: meta.fade_start_ms,
        fade_end_ms: meta.fade_end_ms,
        fade_in_ms: meta.fade_in_ms,
        fade_out_ms: meta.fade_out_ms,
        fade_operator: meta.fade_operator || "",
        cue_name: step.name || `Cue ${index + 1}`,
        cue: step,
      });
      index += 1;
    }

    occurrences.sort((left, right) => {
      if (left.start_ms !== right.start_ms) return left.start_ms - right.start_ms;
      if (left.lane !== right.lane) return left.lane - right.lane;
      return left.plan_index - right.plan_index;
    });
    occurrencesCache = occurrences;
    return occurrences;
  }

  function resolveTimelineLaneConflicts(editedSourceIndex) {
    if (!Array.isArray(cuesObj?.sequence)) return;
    const editedStep = cuesObj.sequence[editedSourceIndex];
    if (!editedStep) return;
    const editedMeta = ensureStepTimeline(editedStep);
    const editedStart = editedMeta.start_ms;
    const editedEnd = editedMeta.start_ms + editedMeta.length_ms;

    for (let index = 0; index < cuesObj.sequence.length; index += 1) {
      if (index === editedSourceIndex) continue;
      const other = cuesObj.sequence[index];
      if (!other || typeof other !== "object") continue;
      const otherMeta = ensureStepTimeline(other);
      if (otherMeta.lane !== editedMeta.lane) continue;
      const otherStart = otherMeta.start_ms;
      const otherEnd = otherMeta.start_ms + otherMeta.length_ms;
      if (otherEnd <= editedStart || otherStart >= editedEnd) continue;

      if (otherStart < editedStart) {
        otherMeta.length_ms = Math.max(MIN_BLOCK_MS, editedStart - otherStart);
      } else {
        const remainingMs = otherEnd - editedEnd;
        otherMeta.start_ms = editedEnd;
        otherMeta.length_ms = Math.max(MIN_BLOCK_MS, remainingMs > 0 ? remainingMs : MIN_BLOCK_MS);
      }
      syncStepTimelineBounds(other);
    }
  }

  function selectTimelineCue(sourceIndex) {
    selectedCueIndex = sourceIndex;
    selectedCueIndices.clear();
    selectedCueIndices.add(sourceIndex);
    if (typeof window.fillCuePropsFromSelected === "function") {
      window.fillCuePropsFromSelected();
    }
  }

  function formatMs(ms) {
    const total = Math.max(0, Math.round(ms || 0));
    if (total >= 1000) {
      return `${(total / 1000).toFixed(total >= 10000 ? 0 : 2)}s`;
    }
    return `${total}ms`;
  }

  function getTimelineScrollElement() {
    return document.getElementById("timeline-scroll");
  }

  function getTimelineDurationMs() {
    return Math.max(0, Number(timelineMetrics.duration_ms || 0));
  }

  function clampTimelineCursor(ms) {
    return clamp(snapMs(ms), 0, getTimelineDurationMs());
  }

  function timelineClientXToMs(clientX) {
    const scroll = getTimelineScrollElement();
    if (!scroll) return 0;
    const rect = scroll.getBoundingClientRect();
    const relativePx = scroll.scrollLeft + (clientX - rect.left);
    return clampTimelineCursor(relativePx / Math.max(0.0001, pxPerMs()));
  }

  function syncTimelineViewStateFromDom() {
    const scroll = getTimelineScrollElement();
    if (!scroll) return;
    timelineViewState.scroll_left = Math.max(0, scroll.scrollLeft);
    timelineViewState.scroll_top = Math.max(0, scroll.scrollTop);
  }

  function applyTimelineViewStateToDom() {
    const scroll = getTimelineScrollElement();
    if (!scroll) return;
    const maxLeft = Math.max(0, scroll.scrollWidth - scroll.clientWidth);
    const maxTop = Math.max(0, scroll.scrollHeight - scroll.clientHeight);
    scroll.scrollLeft = clamp(timelineViewState.scroll_left, 0, maxLeft);
    scroll.scrollTop = clamp(timelineViewState.scroll_top, 0, maxTop);
  }

  function ensureTimelineCursorVisible(mode = "center") {
    const scroll = getTimelineScrollElement();
    if (!scroll) return;
    const cursorPx = timelineCursorMs * pxPerMs();
    const paddingPx = 80;

    let nextLeft = scroll.scrollLeft;
    if (mode === "center") {
      nextLeft = cursorPx - scroll.clientWidth / 2;
    } else {
      if (cursorPx < scroll.scrollLeft + paddingPx) {
        nextLeft = cursorPx - paddingPx;
      } else if (cursorPx > scroll.scrollLeft + scroll.clientWidth - paddingPx) {
        nextLeft = cursorPx - scroll.clientWidth + paddingPx;
      }
    }

    scroll.scrollLeft = clamp(nextLeft, 0, Math.max(0, scroll.scrollWidth - scroll.clientWidth));
    syncTimelineViewStateFromDom();
  }

  function updateTimelinePlayheadDom() {
    const rulerPlayhead = document.getElementById("timeline-playhead-ruler");
    const canvasPlayhead = document.getElementById("timeline-playhead-canvas");
    const label = document.getElementById("timeline-playhead-label");
    const leftPx = timelineCursorMs * pxPerMs();
    // Move via transform (compositor-only) so sweeping the playhead does NOT
    // repaint the 340 timeline blocks every frame.
    if (rulerPlayhead) rulerPlayhead.style.transform = `translateX(${leftPx}px)`;
    if (canvasPlayhead) canvasPlayhead.style.transform = `translateX(${leftPx}px)`;
    if (label) label.textContent = formatMs(timelineCursorMs);
    updateTimelineSummary(occurrencesCache, timelineMetrics.lane_count, timelineMetrics.duration_ms);
  }

  function snapshotTimelinePreviewState() {
    const values = {};
    const groups = {};
    for (const [devId, localMap] of Object.entries(deviceLocalValues || {})) {
      values[devId] = { ...(localMap || {}) };
    }
    for (const [devId, groupSet] of Object.entries(deviceCurrentGroups || {})) {
      groups[devId] = Array.from(groupSet || []);
    }
    return { values, groups };
  }

  function restoreTimelinePreviewState(snapshot) {
    if (!snapshot || typeof snapshot !== "object") return;
    deviceLocalValues = {};
    deviceCurrentGroups = {};
    for (const [devId, localMap] of Object.entries(snapshot.values || {})) {
      deviceLocalValues[devId] = { ...(localMap || {}) };
    }
    for (const devId of Object.keys(rigDevices || {})) {
      if (!deviceLocalValues[devId]) {
        deviceLocalValues[devId] = deviceLocalValues[devId] || {};
      }
      deviceCurrentGroups[devId] = new Set(snapshot.groups?.[devId] || []);
    }
    if (typeof renderActualEffectsPanel === "function") renderActualEffectsPanel();
    if (typeof drawRig === "function") drawRig();
    if (typeof syncRgbWidgetFromFirstDevice === "function") syncRgbWidgetFromFirstDevice();
    if (typeof syncPosWidgetFromFirstDevice === "function") syncPosWidgetFromFirstDevice();
  }

  function stopTimelineManualPreview() {
    timelineManualPreviewActive = false;
    if (timelineManualPreviewTimer != null) {
      window.clearInterval(timelineManualPreviewTimer);
      timelineManualPreviewTimer = null;
    }
  }

  function startTimelineManualPreview() {
    timelineManualPreviewActive = true;
    if (timelineManualPreviewTimer != null) return;
    timelineManualPreviewTimer = window.setInterval(() => {
      if (!timelineManualPreviewActive) return;
      if (!isTimelineEditorMode()) return;
      if (typeof window.isCuePlaybackActive === "function" && window.isCuePlaybackActive()) return;
      scheduleTimelineCursorPreview(timelineCursorMs);
    }, TIMELINE_MANUAL_PREVIEW_INTERVAL_MS);
  }

  async function pushCurrentStateAfterTimelineRestore() {
    try {
      if (
        typeof buildBackendCuePayloadFromCurrentState === "function" &&
        typeof sendBackendCuePayload === "function"
      ) {
        await sendBackendCuePayload(buildBackendCuePayloadFromCurrentState());
        if (typeof syncBackendLiveGroups === "function") {
          await syncBackendLiveGroups();
        }
      }
    } catch (err) {
      console.warn("[TIMELINE] failed to push restored state:", err);
    }
  }

  function detachTimelinePointerTracking() {
    window.removeEventListener("mousemove", handleTimelinePointerMove, true);
    window.removeEventListener("mouseup", stopTimelinePointerDrag, true);
    const scroll = getTimelineScrollElement();
    if (scroll) scroll.classList.remove("is-grabbing");
  }

  function resetTimelineInteractionState(options = {}) {
    stopTimelineManualPreview();
    if (timelinePreviewTimer != null) {
      window.clearTimeout(timelinePreviewTimer);
      timelinePreviewTimer = null;
    }
    timelinePreviewPendingMs = null;
    timelinePreviewInFlight = false;
    timelineBackendPreviewPromise = null;
    timelineBackendPreviewPrimed = false;
    detachTimelinePointerTracking();
    dragState = null;

    if (options.stop_scrub_session && timelineBackendScrubSessionActive) {
      timelineBackendScrubSessionActive = false;
      if (typeof window.stopCuePlayback === "function" && typeof window.isCuePlaybackActive === "function" && window.isCuePlaybackActive()) {
        window.stopCuePlayback(true).catch((err) => console.warn("[TIMELINE] failed to stop scrub session:", err));
      }
    } else if (!options.stop_scrub_session) {
      timelineBackendScrubSessionActive = false;
    }

    if (timelinePreviewStateBackup) {
      const snapshot = timelinePreviewStateBackup;
      timelinePreviewStateBackup = null;
      restoreTimelinePreviewState(snapshot);
      pushCurrentStateAfterTimelineRestore();
    }
  }

  function getTimelineFadeMix(occurrence, elapsedMs) {
    const localMs = Math.max(0, elapsedMs - Number(occurrence?.start_ms || 0));
    const fadeStart = Math.max(0, Number(occurrence?.fade_start_ms || 0));
    const fadeEnd = Math.max(fadeStart, Number(occurrence?.fade_end_ms || 0));
    if (fadeEnd <= fadeStart) return 1;
    if (localMs <= fadeStart) return 0;
    if (localMs >= fadeEnd) return 1;
    return clamp((localMs - fadeStart) / Math.max(1, fadeEnd - fadeStart), 0, 1);
  }

  function sortTimelineOccurrencesForRender(occurrences, priorityMode) {
    const mode = ["top", "bottom", "merge"].includes(String(priorityMode || "").trim().toLowerCase())
      ? String(priorityMode || "").trim().toLowerCase()
      : "top";
    if (mode === "bottom") {
      return [...occurrences].sort((left, right) => (
        left.lane - right.lane ||
        left.start_ms - right.start_ms ||
        left.plan_index - right.plan_index
      ));
    }
    if (mode === "top") {
      return [...occurrences].sort((left, right) => (
        right.lane - left.lane ||
        left.start_ms - right.start_ms ||
        left.plan_index - right.plan_index
      ));
    }
    return [...occurrences].sort((left, right) => (
      left.start_ms - right.start_ms ||
      left.lane - right.lane ||
      left.plan_index - right.plan_index
    ));
  }

  function getTimelineAttrKind(dev, absoluteChannel) {
    if (!dev || typeof getDeviceChannelInfo !== "function") return "other";
    const info = getDeviceChannelInfo(dev, absoluteChannel);
    if (!info) return "other";
    if (info.family === "dimmer") return "dimmer";
    if (info.family === "color") return "color";
    return "other";
  }

  function applyTimelineOccurrenceToPreview(occurrence, elapsedMs, groupMix, options = {}) {
    const step = occurrence?.cue;
    const devices = step?.devices || {};
    const deviceGroups = step?.device_groups || {};
    const prepOnly = options.prep_only === true;
    const onlyDeviceId = options.dev_id != null ? String(options.dev_id) : "";
    const fadeMix = prepOnly ? 1 : getTimelineFadeMix(occurrence, elapsedMs);

    for (const [devId, devSpec] of Object.entries(devices)) {
      if (onlyDeviceId && String(devId) !== onlyDeviceId) continue;
      const dev = rigDevices?.[devId];
      const channels = devSpec?.channels || {};
      if (!dev || !channels || typeof channels !== "object") continue;

      const fi = fixtures?.[dev.fixture] || {};
      const addrCount = Math.max(1, parseInt(fi?.addr_count, 10) || 1);
      deviceLocalValues[devId] ||= {};
      for (let localIndex = 0; localIndex < addrCount; localIndex += 1) {
        if (!Number.isFinite(deviceLocalValues[devId][localIndex])) {
          deviceLocalValues[devId][localIndex] = 0;
        }
      }

      for (const [chKey, rawValue] of Object.entries(channels)) {
        if (String(chKey).toLowerCase() === "universe") continue;
        const absoluteChannel = parseInt(chKey, 10);
        if (!Number.isFinite(absoluteChannel)) continue;
        const localIndex = absoluteChannel - (parseInt(dev.address, 10) || 0);
        if (localIndex < 0 || localIndex >= addrCount) continue;
        const targetValue = clamp(parseInt(rawValue, 10) || 0, 0, 255);
        const attrKind = getTimelineAttrKind(dev, absoluteChannel);

        if (prepOnly) {
          if (attrKind === "other") {
            deviceLocalValues[devId][localIndex] = targetValue;
          }
          continue;
        }

        if (attrKind === "other") {
          deviceLocalValues[devId][localIndex] = targetValue;
        } else {
          deviceLocalValues[devId][localIndex] = Math.round(targetValue * fadeMix);
        }
      }

      if (prepOnly) continue;

      const groups = Array.isArray(deviceGroups?.[devId]) ? deviceGroups[devId] : [];
      if (!groups.length) continue;
      deviceCurrentGroups[devId] ||= new Set();
      const mixForDevice = groupMix[devId] ||= {};
      for (const groupId of groups) {
        deviceCurrentGroups[devId].add(groupId);
        mixForDevice[groupId] = fadeMix;
      }
    }
  }

  async function applyTimelineCursorPreviewLocally(ms) {
    const occurrences = buildTimelineOccurrences();
    const elapsedMs = clampTimelineCursor(ms);
    const activeBlocks = occurrences.filter((occurrence) => occurrence.start_ms <= elapsedMs && elapsedMs < occurrence.end_ms);
    const renderBlocks = sortTimelineOccurrencesForRender(activeBlocks, cueEditorSettings.timeline_priority_mode);
    const activeDevices = new Set();
    const prepBlocksByDevice = {};

    for (const occurrence of activeBlocks) {
      for (const devId of Object.keys(occurrence?.cue?.devices || {})) {
        activeDevices.add(String(devId));
      }
    }

    for (const occurrence of occurrences) {
      if (occurrence.start_ms <= elapsedMs) continue;
      for (const devId of Object.keys(occurrence?.cue?.devices || {})) {
        const devKey = String(devId);
        if (activeDevices.has(devKey) || prepBlocksByDevice[devKey]) continue;
        prepBlocksByDevice[devKey] = occurrence;
      }
    }

    for (const [devId, dev] of Object.entries(rigDevices || {})) {
      const fi = fixtures?.[dev?.fixture] || {};
      const addrCount = Math.max(1, parseInt(fi?.addr_count, 10) || 1);
      deviceLocalValues[devId] = {};
      for (let localIndex = 0; localIndex < addrCount; localIndex += 1) {
        deviceLocalValues[devId][localIndex] = 0;
      }
      deviceCurrentGroups[devId] = new Set();
    }

    for (const [devId, occurrence] of Object.entries(prepBlocksByDevice)) {
      applyTimelineOccurrenceToPreview(occurrence, elapsedMs, {}, { prep_only: true, dev_id: devId });
    }

    const groupMix = {};
    for (const occurrence of renderBlocks) {
      applyTimelineOccurrenceToPreview(occurrence, elapsedMs, groupMix);
    }

    if (typeof drawRig === "function") drawRig();
    if (typeof syncRgbWidgetFromFirstDevice === "function") syncRgbWidgetFromFirstDevice();
    if (typeof syncPosWidgetFromFirstDevice === "function") syncPosWidgetFromFirstDevice();

    if (
      typeof buildBackendCuePayloadFromCurrentState === "function" &&
      typeof sendBackendCuePayload === "function"
    ) {
      await sendBackendCuePayload(buildBackendCuePayloadFromCurrentState());
    }
  }

  async function ensureTimelineBackendPreviewSession(startMs) {
    if (timelineBackendPreviewPrimed) {
      return true;
    }
    if (timelineBackendPreviewPromise) {
      await timelineBackendPreviewPromise;
      return true;
    }
    const request = buildTimelinePlaybackRequest(null);
    if (!request) return false;

    request.payload.start_ms = clampTimelineCursor(startMs);
    request.payload.paused = true;
    backendPlaybackPlan = request.ui_plan || [];
    backendLastCueToken = 0;
    backendPlaybackStarting = true;

    timelineBackendPreviewPromise = (async () => {
      const res = await fetch("/api/playback/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(request.payload),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || "timeline preview start failed");
      }
      return true;
    })();

    try {
      await timelineBackendPreviewPromise;
      timelineBackendPreviewPrimed = true;
      timelineBackendScrubSessionActive = true;
      window.backendPlaybackOwned = true;
      backendPlaybackStarting = false;
      return true;
    } catch (err) {
      backendPlaybackStarting = false;
      window.backendPlaybackOwned = false;
      timelineBackendPreviewPrimed = false;
      timelineBackendScrubSessionActive = false;
      throw err;
    } finally {
      timelineBackendPreviewPromise = null;
    }
  }

  async function applyTimelineCursorPreview(ms) {
    const targetMs = clampTimelineCursor(ms);
    if (!timelinePreviewStateBackup) {
      timelinePreviewStateBackup = snapshotTimelinePreviewState();
    }
    if (typeof window.isBackendMode === "function" && window.isBackendMode()) {
      if (timelineBackendScrubSessionActive && typeof stopRun === "function") {
        await stopRun(true);
        timelineBackendScrubSessionActive = false;
        timelineBackendPreviewPrimed = false;
      }
      try {
        const hasLiveBackendPlayback = Boolean(playbackActive && window.backendPlaybackOwned);
        if (hasLiveBackendPlayback && typeof controlBackendPlayback === "function") {
          await controlBackendPlayback("seek", 0, { seek_ms: targetMs });
          return;
        }
      } catch (err) {
        console.warn("[TIMELINE] backend cursor preview failed, fallback to local preview:", err);
      }
    }
    await applyTimelineCursorPreviewLocally(targetMs);
  }

  function flushTimelineCursorPreview() {
    if (timelinePreviewInFlight) return;
    if (!Number.isFinite(Number(timelinePreviewPendingMs))) return;
    const targetMs = clampTimelineCursor(timelinePreviewPendingMs);
    timelinePreviewPendingMs = null;
    timelinePreviewInFlight = true;
    Promise.resolve(applyTimelineCursorPreview(targetMs))
      .catch((err) => console.warn("[TIMELINE] cursor preview failed:", err))
      .finally(() => {
        timelinePreviewInFlight = false;
        if (Number.isFinite(Number(timelinePreviewPendingMs))) {
          scheduleTimelineCursorPreview(timelinePreviewPendingMs);
        }
      });
  }

  function scheduleTimelineCursorPreview(ms, options = {}) {
    timelinePreviewPendingMs = clampTimelineCursor(ms);
    if (options.immediate) {
      if (timelinePreviewTimer != null) {
        window.clearTimeout(timelinePreviewTimer);
        timelinePreviewTimer = null;
      }
      flushTimelineCursorPreview();
      return;
    }
    if (timelinePreviewTimer != null) return;
    timelinePreviewTimer = window.setTimeout(() => {
      timelinePreviewTimer = null;
      flushTimelineCursorPreview();
    }, 50);
  }

  function syncTimelinePlaybackCursor(playbackState) {
    stopTimelineManualPreview();
    if (!isTimelineEditorMode()) {
      timelineBackendPreviewPrimed = false;
      timelineBackendScrubSessionActive = false;
      return;
    }
    if (!playbackState || typeof playbackState !== "object") return;
    if (!Boolean(playbackState.active)) {
      timelineBackendPreviewPrimed = false;
      timelineBackendScrubSessionActive = false;
      return;
    }
    timelineBackendPreviewPrimed = true;
    let elapsedMs = Number(playbackState.timeline_elapsed_ms);
    if (!Number.isFinite(elapsedMs)) return;
    const serverTimeMs = Number(playbackState.server_time_ms);
    if (!Boolean(playbackState.paused) && Number.isFinite(serverTimeMs)) {
      elapsedMs += Math.max(0, Date.now() - serverTimeMs);
    }
    if (dragState?.mode === "cursor") return;
    setTimelineCursorMs(elapsedMs, {
      render: false,
      ensure_visible: playbackState.paused ? false : "visible",
      preview: false,
      source: "backend",
    });
  }

  function setTimelineCursorMs(ms, options = {}) {
    timelineCursorMs = clampTimelineCursor(ms);
    if (options.render === false) {
      updateTimelinePlayheadDom();
    } else if (typeof window.renderTimelineEditor === "function") {
      window.renderTimelineEditor();
    }
    if (options.ensure_visible) {
      ensureTimelineCursorVisible(options.ensure_visible === true ? "center" : options.ensure_visible);
    }
    if (options.preview) {
      startTimelineManualPreview();
      scheduleTimelineCursorPreview(timelineCursorMs, { immediate: options.preview === "immediate" });
    }
  }

  function startTimelineViewportDrag(event) {
    const scroll = getTimelineScrollElement();
    if (!scroll) return;
    hideTimelineContextMenu();
    dragState = {
      mode: "pan",
      origin_client_x: event.clientX,
      origin_client_y: event.clientY,
      scroll_left: scroll.scrollLeft,
      scroll_top: scroll.scrollTop,
      moved: false,
      set_cursor_on_click: true,
      last_client_x: event.clientX,
    };
    scroll.classList.add("is-grabbing");
    window.addEventListener("mousemove", handleTimelinePointerMove, true);
    window.addEventListener("mouseup", stopTimelinePointerDrag, true);
  }

  function startTimelineCursorDrag(event) {
    hideTimelineContextMenu();
    dragState = {
      mode: "cursor",
      origin_client_x: event.clientX,
      origin_client_y: event.clientY,
      cursor_ms: timelineCursorMs,
    };
    window.addEventListener("mousemove", handleTimelinePointerMove, true);
    window.addEventListener("mouseup", stopTimelinePointerDrag, true);
  }

  function bindTimelineViewportInteractions() {
    const scroll = getTimelineScrollElement();
    const canvas = document.getElementById("timeline-canvas");
    const ruler = document.getElementById("timeline-ruler");
    const canvasPlayhead = document.getElementById("timeline-playhead-canvas");
    const rulerPlayhead = document.getElementById("timeline-playhead-ruler");
    if (!scroll || !canvas || !ruler) return;

    if (scroll.dataset.timelineScrollBound !== "1") {
      scroll.dataset.timelineScrollBound = "1";
      scroll.addEventListener("scroll", () => {
        syncTimelineViewStateFromDom();
      }, { passive: true });
    }

    const bindPanTarget = (element) => {
      if (!(element instanceof HTMLElement) || element.dataset.timelinePanBound === "1") return;
      element.dataset.timelinePanBound = "1";
      element.addEventListener("mousedown", (event) => {
        if (event.button !== 0) return;
        const target = event.target;
        if (target instanceof HTMLElement && target.closest(".timeline-block, .timeline-playhead, .timeline-playhead-handle")) {
          return;
        }
        event.preventDefault();
        startTimelineViewportDrag(event);
      });
    };

    bindPanTarget(canvas);
    bindPanTarget(ruler);

    const bindCursorTarget = (element) => {
      if (!(element instanceof HTMLElement) || element.dataset.timelineCursorBound === "1") return;
      element.dataset.timelineCursorBound = "1";
      element.addEventListener("mousedown", (event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        event.stopPropagation();
        startTimelineCursorDrag(event);
      });
    };

    bindCursorTarget(canvasPlayhead);
    bindCursorTarget(rulerPlayhead);
  }

  function majorStepPx() {
    return cueEditorSettings.zoom_x;
  }

  function pxPerMs() {
    return cueEditorSettings.zoom_x / 1000;
  }

  function laneHeightPx() {
    return cueEditorSettings.zoom_y;
  }

  function buildTimelineRuler(totalWidthPx, totalDurationMs) {
    const ruler = document.getElementById("timeline-ruler");
    if (!ruler) return;
    ruler.innerHTML = "";
    ruler.style.width = `${Math.max(totalWidthPx, 100)}px`;
    ruler.style.setProperty("--timeline-major-step", `${Math.max(20, majorStepPx())}px`);

    const stepMs = 1000;
    for (let ms = 0; ms <= totalDurationMs + stepMs; ms += stepMs) {
      const mark = document.createElement("div");
      mark.className = "timeline-ruler-mark";
      mark.style.left = `${ms * pxPerMs()}px`;
      mark.textContent = `${(ms / 1000).toFixed(0)}s`;
      ruler.appendChild(mark);
    }
  }

  function hideTimelineContextMenu() {
    const menu = document.getElementById("timeline-context-menu");
    if (!menu) return;
    menu.classList.add("hidden");
    menu.innerHTML = "";
  }

  function openTimelineContextMenu(event, occurrence) {
    const menu = document.getElementById("timeline-context-menu");
    if (!menu) return;
    menu.innerHTML = "";
    menu.classList.remove("hidden");
    menu.style.left = `${event.clientX}px`;
    menu.style.top = `${event.clientY}px`;

    for (const operator of OPERATOR_OPTIONS) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = operator === occurrence.fade_operator ? "secondary" : "";
      btn.textContent = operator || "Cut";
      btn.addEventListener("click", () => {
        const step = cuesObj.sequence[occurrence.source_index];
        // The spread type lives in the step's fade, like the fade time itself:
        // written on the block it would vanish on the next derive.
        applyBlockOperatorToStep(step, operator);
        syncStepTimelineBounds(step);
        hideTimelineContextMenu();
        renderTimelineEditor();
        if (typeof window.renderCueTable === "function") window.renderCueTable();
      });
      menu.appendChild(btn);
    }
  }

  function startTimelinePointerDrag(mode, occurrence, event) {
    const step = cuesObj.sequence[occurrence.source_index];
    const meta = ensureStepTimeline(step);
    selectTimelineCue(occurrence.source_index);
    hideTimelineContextMenu();

    dragState = {
      mode,
      source_index: occurrence.source_index,
      iteration: occurrence.iteration,
      group_span_ms: occurrence.group_span_ms,
      lane: meta.lane,
      start_ms: meta.start_ms,
      length_ms: meta.length_ms,
      fade_start_ms: meta.fade_start_ms,
      fade_end_ms: meta.fade_end_ms,
      fade_in_ms: meta.fade_in_ms,
      fade_out_ms: meta.fade_out_ms,
      origin_client_x: event.clientX,
      origin_client_y: event.clientY,
    };
    window.addEventListener("mousemove", handleTimelinePointerMove, true);
    window.addEventListener("mouseup", stopTimelinePointerDrag, true);
  }

  function handleTimelinePointerMove(event) {
    if (!dragState) return;
    if (dragState.mode === "pan") {
      const scroll = getTimelineScrollElement();
      if (!scroll) return;
      const dx = event.clientX - dragState.origin_client_x;
      const dy = event.clientY - dragState.origin_client_y;
      if (Math.abs(dx) > 3 || Math.abs(dy) > 3) {
        dragState.moved = true;
      }
      dragState.last_client_x = event.clientX;
      scroll.scrollLeft = Math.max(0, dragState.scroll_left - dx);
      scroll.scrollTop = Math.max(0, dragState.scroll_top - dy);
      syncTimelineViewStateFromDom();
      return;
    }
    if (dragState.mode === "cursor") {
      setTimelineCursorMs(dragState.cursor_ms + ((event.clientX - dragState.origin_client_x) / pxPerMs()), {
        render: false,
        preview: true,
      });
      return;
    }
    const step = cuesObj.sequence[dragState.source_index];
    const meta = ensureStepTimeline(step);
    const deltaMs = snapMs((event.clientX - dragState.origin_client_x) / pxPerMs());
    const deltaLane = Math.round((event.clientY - dragState.origin_client_y) / laneHeightPx());

    if (dragState.mode === "move") {
      meta.start_ms = Math.max(0, dragState.start_ms + deltaMs);
      meta.lane = Math.max(0, dragState.lane + deltaLane);
    } else if (dragState.mode === "resize") {
      applyBlockLengthToStep(step, Math.max(MIN_BLOCK_MS, dragState.length_ms + deltaMs));
    } else if (dragState.mode === "resize-left") {
      // Drag the left edge: move start, keep the right edge anchored.
      const maxDelta = dragState.length_ms - MIN_BLOCK_MS;
      const d = Math.min(maxDelta, Math.max(-dragState.start_ms, deltaMs));
      meta.start_ms = Math.max(0, dragState.start_ms + d);
      applyBlockLengthToStep(step, Math.max(MIN_BLOCK_MS, dragState.length_ms - d));
    } else if (dragState.mode === "fade-in") {
      applyBlockFadeToStep(step, clamp(dragState.fade_in_ms + deltaMs, 0, meta.length_ms - meta.fade_out_ms));
    } else if (dragState.mode === "fade-out") {
      // Out handle sits at (length - fade_out); dragging it left grows fade-out.
      meta.fade_out_ms = clamp(dragState.fade_out_ms - deltaMs, 0, meta.length_ms - meta.fade_in_ms);
    }

    syncStepTimelineBounds(step);
    resolveTimelineLaneConflicts(dragState.source_index);
    syncSequenceFromTimeline();
    if (typeof window.renderCueTable === "function") window.renderCueTable();
    renderTimelinePropertiesPanel();
  }

  function stopTimelinePointerDrag() {
    if (!dragState) return;
    const prevState = dragState;
    if (prevState.mode === "pan") {
      const scroll = getTimelineScrollElement();
      if (scroll) scroll.classList.remove("is-grabbing");
      if (!prevState.moved && prevState.set_cursor_on_click) {
        setTimelineCursorMs(timelineClientXToMs(prevState.last_client_x || prevState.origin_client_x), {
          render: false,
          preview: true,
        });
      }
    }
    dragState = null;
    detachTimelinePointerTracking();
  }

  function occurrenceToPlaybackPlan(occurrence) {
    return {
      ...occurrence.cue,
      playback_index: occurrence.source_index,
      plan_index: occurrence.plan_index,
      __timeline: true,
      timeline_start_ms: occurrence.start_ms,
      timeline_length_ms: occurrence.length_ms,
      timeline_lane: occurrence.lane,
    };
  }

  function updateTimelineSummary(occurrences, laneCount, totalDurationMs) {
    const summary = document.getElementById("timeline-summary");
    if (!summary) return;
    summary.textContent = `${occurrences.length} blocks - ${laneCount} lanes - ${formatMs(totalDurationMs)} - cursor ${formatMs(timelineCursorMs)}`;
  }

  function renderTimelinePropertiesPanel() {
    const panel = document.getElementById("timeline-props");
    if (!panel) return;
    const tl = (k, f) => (typeof window.t === "function" ? window.t(k, f) : f);
    const idx = selectedCueIndex;
    const step = (Number.isInteger(idx) && idx >= 0) ? cuesObj?.sequence?.[idx] : null;
    if (!step) {
      panel.innerHTML = `<div class="timeline-props-empty muted">${escapeHtml(tl("timeline.propsEmpty", "Select a clip to edit its properties."))}</div>`;
      return;
    }
    const meta = ensureStepTimeline(step);
    const opOptions = OPERATOR_OPTIONS
      .map((op) => `<option value="${op}" ${op === (meta.fade_operator || "") ? "selected" : ""}>${op === "" ? tl("timeline.fadeCut", "Cut") : op}</option>`)
      .join("");
    panel.innerHTML = `
      <div class="tl-props-head">${escapeHtml(tl("timeline.propsTitle", "Clip properties"))}</div>
      <div class="tl-props-grid">
        <label class="tl-prop tl-prop-wide"><span>${tl("timeline.propName", "Name")}</span><input id="tlp-name" type="text" value="${escapeHtml(step.name || "")}"></label>
        <label class="tl-prop"><span>${tl("timeline.propStart", "Start (ms)")}</span><input id="tlp-start" type="number" min="0" step="25" value="${Math.round(meta.start_ms)}"></label>
        <label class="tl-prop"><span>${tl("timeline.propLength", "Length (ms)")}</span><input id="tlp-length" type="number" min="100" step="25" value="${Math.round(meta.length_ms)}"></label>
        <label class="tl-prop"><span>${tl("timeline.propLane", "Lane")}</span><input id="tlp-lane" type="number" min="0" step="1" value="${meta.lane}"></label>
        <label class="tl-prop"><span>${tl("timeline.propFadeIn", "Fade in (ms)")}</span><input id="tlp-fadein" type="number" min="0" step="25" value="${Math.round(meta.fade_in_ms)}"></label>
        <label class="tl-prop"><span>${tl("timeline.propFadeOut", "Fade out (ms)")}</span><input id="tlp-fadeout" type="number" min="0" step="25" value="${Math.round(meta.fade_out_ms)}"></label>
        <label class="tl-prop"><span>${tl("timeline.propOperator", "Fade type")}</span><select id="tlp-op">${opOptions}</select></label>
      </div>
    `;
    const apply = () => {
      const num = (id, def) => { const v = parseInt(document.getElementById(id)?.value, 10); return Number.isFinite(v) ? v : def; };
      const nameEl = document.getElementById("tlp-name");
      if (nameEl) step.name = nameEl.value;
      meta.start_ms = Math.max(0, num("tlp-start", meta.start_ms));
      meta.lane = Math.max(0, num("tlp-lane", meta.lane));
      // Fade, spread type and length belong to the STEP -- writing them into
      // the block would be undone the next time it is derived. Order matters:
      // the length is measured from the fade.
      applyBlockOperatorToStep(step, document.getElementById("tlp-op")?.value ?? "");
      applyBlockFadeToStep(step, Math.max(0, num("tlp-fadein", meta.fade_in_ms)));
      applyBlockLengthToStep(step, Math.max(MIN_BLOCK_MS, num("tlp-length", meta.length_ms)));
      syncStepTimelineBounds(step);
      meta.fade_out_ms = clamp(num("tlp-fadeout", meta.fade_out_ms), 0, Math.max(0, meta.length_ms - meta.fade_in_ms));
      resolveTimelineLaneConflicts(idx);
      syncSequenceFromTimeline();
      renderTimelineEditor();
      if (typeof window.renderCueTable === "function") window.renderCueTable();
    };
    panel.querySelectorAll("input, select").forEach((el) => el.addEventListener("change", apply));
  }

  function renderTimelineEditor() {
    const timelineEditor = document.getElementById("timeline-editor");
    const tableContainer = document.querySelector(".cue-table-container");
    if (!timelineEditor || !tableContainer) return;

    const timelineMode = isTimelineEditorMode();
    timelineEditor.classList.toggle("hidden", !timelineMode);
    tableContainer.classList.toggle("hidden", timelineMode);
    if (!timelineMode) {
      hideTimelineContextMenu();
      return;
    }

    const canvas = document.getElementById("timeline-canvas");
    if (!canvas) return;

    const occurrences = buildTimelineOccurrences();
    const maxLane = occurrences.reduce((max, occurrence) => Math.max(max, occurrence.lane), 0);
    const laneCount = Math.max(2, maxLane + 2);
    const totalDurationMs = Math.max(2000, ...occurrences.map((occurrence) => occurrence.end_ms));
    const totalWidthPx = Math.max(1000, Math.ceil(totalDurationMs * pxPerMs()) + 220);
    const totalHeightPx = laneCount * laneHeightPx();
    timelineMetrics = {
      duration_ms: totalDurationMs,
      width_px: totalWidthPx,
      lane_count: laneCount,
      height_px: totalHeightPx,
    };
    timelineCursorMs = clampTimelineCursor(timelineCursorMs);
    syncTimelineViewStateFromDom();

    canvas.innerHTML = "";
    canvas.style.width = `${totalWidthPx}px`;
    canvas.style.height = `${totalHeightPx}px`;
    canvas.style.setProperty("--timeline-major-step", `${Math.max(20, majorStepPx())}px`);
    buildTimelineRuler(totalWidthPx, totalDurationMs);

    for (let lane = 0; lane < laneCount; lane += 1) {
      const laneEl = document.createElement("div");
      laneEl.className = "timeline-lane";
      laneEl.style.top = `${lane * laneHeightPx()}px`;
      laneEl.style.height = `${laneHeightPx()}px`;

      const label = document.createElement("div");
      label.className = "timeline-lane-label";
      label.textContent = `Lane ${lane + 1}`;
      laneEl.appendChild(label);
      canvas.appendChild(laneEl);
    }

    const leftPx = timelineCursorMs * pxPerMs();
    const ruler = document.getElementById("timeline-ruler");
    if (ruler) {
      const rulerPlayhead = document.createElement("div");
      rulerPlayhead.id = "timeline-playhead-ruler";
      rulerPlayhead.className = "timeline-playhead timeline-playhead-ruler";
      rulerPlayhead.style.left = "0";
      rulerPlayhead.style.transform = `translateX(${leftPx}px)`;
      rulerPlayhead.innerHTML = `
        <div class="timeline-playhead-handle">
          <span id="timeline-playhead-label">${formatMs(timelineCursorMs)}</span>
        </div>
      `;
      ruler.appendChild(rulerPlayhead);
    }

    const canvasPlayhead = document.createElement("div");
    canvasPlayhead.id = "timeline-playhead-canvas";
    canvasPlayhead.className = "timeline-playhead timeline-playhead-canvas";
    canvasPlayhead.style.left = "0";
    canvasPlayhead.style.transform = `translateX(${leftPx}px)`;
    canvas.appendChild(canvasPlayhead);

    for (const occurrence of occurrences) {
      const block = document.createElement("div");
      block.className = "timeline-block";
      if (occurrence.source_index === selectedCueIndex) block.classList.add("selected");
      if (occurrence.source_index === playbackCueIndex) block.classList.add("playing");
      if (occurrence.group_id) block.classList.add("loop-linked");
      block.dataset.sourceIndex = String(occurrence.source_index);
      block.dataset.planIndex = String(occurrence.plan_index);
      block.style.left = `${occurrence.start_ms * pxPerMs()}px`;
      block.style.top = `${occurrence.lane * laneHeightPx() + 8}px`;
      block.style.width = `${Math.max(56, occurrence.length_ms * pxPerMs())}px`;
      block.style.height = `${Math.max(54, laneHeightPx() - 16)}px`;

      const len = Math.max(1, occurrence.length_ms);
      const fiPct = clamp((occurrence.fade_in_ms / len) * 100, 0, 100);
      const foPct = clamp((occurrence.fade_out_ms / len) * 100, 0, 100);
      const fiX = fiPct;            // x where fade-in reaches full
      const foX = 100 - foPct;      // x where fade-out begins
      // Level envelope (Premiere fade): rises over fade-in, holds, drops over fade-out.
      const envArea = `M0,100 L${fiX},2 L${foX},2 L100,100 Z`;
      const envLine = `0,100 ${fiX},2 ${foX},2 100,100`;
      // Colour the clip by its lane for quick visual grouping.
      block.style.setProperty("--clip-hue", String((occurrence.lane * 47) % 360));

      block.innerHTML = `
        <div class="tl-clip-resize tl-resize-left"></div>
        <div class="tl-clip-resize tl-resize-right"></div>
        <div class="tl-clip-title">
          <span class="tl-clip-name">${escapeHtml(occurrence.cue_name)}</span>
          <span class="tl-clip-dur">${formatMs(occurrence.length_ms)}</span>
        </div>
        <svg class="tl-clip-fade" viewBox="0 0 100 100" preserveAspectRatio="none">
          <path class="tl-fade-area" d="${envArea}" />
          <polyline class="tl-fade-line" points="${envLine}" />
        </svg>
        <div class="tl-fade-handle tl-fade-in" style="left:${fiX}%" title="Fade in"></div>
        <div class="tl-fade-handle tl-fade-out" style="left:${foX}%" title="Fade out"></div>
        ${occurrence.fade_operator ? `<span class="tl-clip-op">${escapeHtml(occurrence.fade_operator)}</span>` : ""}
      `;

      block.addEventListener("click", () => {
        selectTimelineCue(occurrence.source_index);
        renderTimelinePropertiesPanel();
        if (typeof window.renderCueTable === "function") window.renderCueTable();
      });
      block.addEventListener("contextmenu", (ev) => {
        ev.preventDefault();
        selectTimelineCue(occurrence.source_index);
        openTimelineContextMenu(ev, occurrence);
      });
      block.querySelector(".tl-resize-right")?.addEventListener("mousedown", (ev) => {
        ev.preventDefault(); ev.stopPropagation();
        startTimelinePointerDrag("resize", occurrence, ev);
      });
      block.querySelector(".tl-resize-left")?.addEventListener("mousedown", (ev) => {
        ev.preventDefault(); ev.stopPropagation();
        startTimelinePointerDrag("resize-left", occurrence, ev);
      });
      block.querySelector(".tl-fade-in")?.addEventListener("mousedown", (ev) => {
        ev.preventDefault(); ev.stopPropagation();
        startTimelinePointerDrag("fade-in", occurrence, ev);
      });
      block.querySelector(".tl-fade-out")?.addEventListener("mousedown", (ev) => {
        ev.preventDefault(); ev.stopPropagation();
        startTimelinePointerDrag("fade-out", occurrence, ev);
      });
      block.addEventListener("mousedown", (ev) => {
        if (ev.button !== 0) return;
        if (ev.target instanceof HTMLElement && ev.target.closest(".tl-clip-resize, .tl-fade-handle")) return;
        ev.preventDefault();
        startTimelinePointerDrag("move", occurrence, ev);
      });

      canvas.appendChild(block);
    }

    applyTimelineViewStateToDom();
    bindTimelineViewportInteractions();
    updateTimelineSummary(occurrences, laneCount, totalDurationMs);
    updateTimelineZoomInputs();
    renderTimelinePropertiesPanel();
  }

  function buildTimelinePlaybackRequest(startCueIndex = 0) {
    const occurrences = buildTimelineOccurrences();
    if (!occurrences.length) return null;

    let startOccurrence = occurrences[0];
    let requestedStartMs = 0;
    if (Number.isFinite(Number(startCueIndex)) && Number(startCueIndex) >= 0) {
      const match = occurrences.find((occurrence) => occurrence.source_index === Number(startCueIndex));
      if (match) startOccurrence = match;
    } else {
      requestedStartMs = clampTimelineCursor(timelineCursorMs);
      const match = occurrences.find((occurrence) => occurrence.end_ms > requestedStartMs);
      if (match) startOccurrence = match;
    }

    const blocks = occurrences.map((occurrence) => {
      const cuePayload = typeof window.buildBackendCuePayload === "function"
        ? window.buildBackendCuePayload(occurrence.cue)
        : { devices: occurrence.cue?.devices || {}, duration: occurrence.cue?.duration || "0", device_order: occurrence.cue?.device_order || [] };
      return {
        plan_index: occurrence.plan_index,
        cue_index: occurrence.source_index,
        cue_name: occurrence.cue_name,
        lane: occurrence.lane,
        start_ms: occurrence.start_ms,
        length_ms: occurrence.length_ms,
        fade_start_ms: occurrence.fade_start_ms,
        fade_end_ms: occurrence.fade_end_ms,
        fade_in_ms: occurrence.fade_in_ms,
        fade_out_ms: occurrence.fade_out_ms,
        fade_operator: occurrence.fade_operator || "",
        cue_payload: cuePayload,
        device_order: cuePayload.device_order || [],
      };
    });

    return {
      payload: {
        mode: "timeline",
        timeline: blocks,
        start_ms: requestedStartMs > 0 ? requestedStartMs : startOccurrence.start_ms,
        priority_mode: cueEditorSettings.timeline_priority_mode,
        speed: typeof window.getSelectedPlaybackSpeed === "function" ? window.getSelectedPlaybackSpeed() : 1,
      },
      ui_plan: occurrences.map(occurrenceToPlaybackPlan),
      start_occurrence: startOccurrence,
    };
  }

  function bindTimelineControls() {
    const zoomX = document.getElementById("timeline-zoom-x");
    const zoomY = document.getElementById("timeline-zoom-y");
    if (zoomX && zoomX.dataset.bound !== "1") {
      zoomX.dataset.bound = "1";
      zoomX.addEventListener("input", () => {
        cueEditorSettings.zoom_x = clamp(safeNumber(zoomX.value, cueEditorSettings.zoom_x), 20, 480);
        updateTimelineZoomInputs();
        renderTimelineEditor();
      });
      zoomX.addEventListener("change", () => {
        cueEditorSettings.zoom_x = clamp(safeNumber(zoomX.value, cueEditorSettings.zoom_x), 20, 480);
        scheduleCueEditorSettingsPersist();
      });
    }
    if (zoomY && zoomY.dataset.bound !== "1") {
      zoomY.dataset.bound = "1";
      zoomY.addEventListener("input", () => {
        cueEditorSettings.zoom_y = clamp(safeNumber(zoomY.value, cueEditorSettings.zoom_y), 48, 240);
        updateTimelineZoomInputs();
        renderTimelineEditor();
      });
      zoomY.addEventListener("change", () => {
        cueEditorSettings.zoom_y = clamp(safeNumber(zoomY.value, cueEditorSettings.zoom_y), 48, 240);
        scheduleCueEditorSettingsPersist();
      });
    }
    // Direct Cue list / Timeline / Rapid Fire view toggle (bypasses the settings modal).
    [["cue-view-classic", "classic"], ["cue-view-timeline", "timeline"], ["cue-view-rapidfire", "rapidfire"]].forEach(([id, mode]) => {
      const btn = document.getElementById(id);
      if (btn && btn.dataset.bound !== "1") {
        btn.dataset.bound = "1";
        btn.addEventListener("click", () => {
          if (cueEditorSettings.view_mode === mode) return;
          setCueEditorSettings({ ...cueEditorSettings, view_mode: mode }, { persist: true });
        });
      }
    });
    updateViewToggle();
  }

  async function loadCueEditorSettings() {
    try {
      const res = await fetch("/api/settings", { cache: "no-store" });
      if (!res.ok) throw new Error("settings load failed");
      const data = await res.json();
      setCueEditorSettings(data.cue_editor || {}, { persist: false });
    } catch (err) {
      console.warn("[TIMELINE] cue editor settings load failed:", err);
      setCueEditorSettings(DEFAULT_SETTINGS, { persist: false });
    }
  }

  window.isTimelineEditorMode = isTimelineEditorMode;
  window.getCueEditorSettings = getCueEditorSettings;
  window.setCueEditorSettings = setCueEditorSettings;
  window.getTimelineCursorMs = () => timelineCursorMs;
  window.setTimelineCursorMs = setTimelineCursorMs;
  window.renderTimelineEditor = renderTimelineEditor;
  window.buildTimelinePlaybackRequest = buildTimelinePlaybackRequest;
  window.syncTimelinePlaybackCursor = syncTimelinePlaybackCursor;
  window.resetTimelineInteractionState = resetTimelineInteractionState;

  document.addEventListener("DOMContentLoaded", () => {
    bindTimelineControls();
    loadCueEditorSettings();
    document.addEventListener("click", (ev) => {
      const target = ev.target;
      if (target instanceof HTMLElement && target.closest("#timeline-context-menu, .timeline-block")) return;
      hideTimelineContextMenu();
    });
    document.addEventListener("mousedown", (ev) => {
      if (!timelineManualPreviewActive) return;
      const target = ev.target;
      if (target instanceof HTMLElement && target.closest("#timeline-editor")) return;
      resetTimelineInteractionState({ stop_scrub_session: true });
    }, true);
    window.addEventListener("resize", renderTimelineEditor);
  });
})();
