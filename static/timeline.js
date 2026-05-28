(function () {
  const DEFAULT_SETTINGS = {
    view_mode: "classic",
    timeline_priority_mode: "top",
    zoom_x: 120,
    zoom_y: 88,
  };
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
    return Math.max(0, Math.round(Number(value || 0) / TIMELINE_SNAP_MS) * TIMELINE_SNAP_MS);
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
    next.view_mode = String(next.view_mode || "classic").trim().toLowerCase() === "timeline" ? "timeline" : "classic";
    next.timeline_priority_mode = ["top", "bottom", "merge"].includes(String(next.timeline_priority_mode || "").trim().toLowerCase())
      ? String(next.timeline_priority_mode || "").trim().toLowerCase()
      : "top";
    next.zoom_x = clamp(safeNumber(next.zoom_x, DEFAULT_SETTINGS.zoom_x), 20, 480);
    next.zoom_y = clamp(safeNumber(next.zoom_y, DEFAULT_SETTINGS.zoom_y), 48, 240);
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

  function applyCueEditorLayout() {
    const body = cueEditorRoot();
    const mainGrid = document.querySelector(".main-grid");
    const controller = ensureControllerLayoutAnchors();
    if (!body || !mainGrid || !controller) return;

    const timelineMode = isTimelineEditorMode();
    body.classList.toggle("cue-editor-mode-timeline", timelineMode);

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

  function ensureStepTimeline(step, fallbackStartMs = 0) {
    if (!step || typeof step !== "object") return null;
    if (!step.timeline || typeof step.timeline !== "object") {
      step.timeline = {};
    }
    const meta = step.timeline;
    const durationMeta = parseDurationMeta(step.duration);
    meta.lane = Math.max(0, parseInt(meta.lane, 10) || 0);
    meta.start_ms = Math.max(0, parseInt(meta.start_ms, 10) || Math.max(0, fallbackStartMs));
    meta.length_ms = Math.max(MIN_BLOCK_MS, parseInt(meta.length_ms, 10) || Math.max(MIN_BLOCK_MS, durationMeta.total_ms || 500));
    meta.fade_start_ms = clamp(parseInt(meta.fade_start_ms, 10) || 0, 0, meta.length_ms);
    const fallbackFadeEnd = durationMeta.total_ms > 0 ? durationMeta.total_ms : Math.min(meta.length_ms, Math.max(0, durationMeta.base_ms || 0));
    meta.fade_end_ms = clamp(parseInt(meta.fade_end_ms, 10) || fallbackFadeEnd || 0, meta.fade_start_ms, meta.length_ms);
    meta.fade_operator = OPERATOR_OPTIONS.includes(String(meta.fade_operator || "").trim()) ? String(meta.fade_operator || "").trim() : durationMeta.operator;
    return meta;
  }

  function syncStepTimelineBounds(step) {
    const meta = ensureStepTimeline(step);
    if (!meta) return;
    meta.start_ms = Math.max(0, parseInt(meta.start_ms, 10) || 0);
    meta.length_ms = Math.max(MIN_BLOCK_MS, parseInt(meta.length_ms, 10) || MIN_BLOCK_MS);
    meta.fade_start_ms = clamp(parseInt(meta.fade_start_ms, 10) || 0, 0, meta.length_ms);
    meta.fade_end_ms = clamp(parseInt(meta.fade_end_ms, 10) || 0, meta.fade_start_ms, meta.length_ms);
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
          const sleepMs = Math.max(0, parseInt(groupStep?.sleep, 10) || 0);
          const durationMeta = parseDurationMeta(groupStep?.duration);
          const meta = ensureStepTimeline(groupStep, localCursorMs + sleepMs);
          if (!hasTimeline) {
            meta.start_ms = Math.max(0, localCursorMs + sleepMs);
            meta.length_ms = Math.max(MIN_BLOCK_MS, parseInt(meta.length_ms, 10) || Math.max(MIN_BLOCK_MS, durationMeta.total_ms || 500));
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
      const sleepMs = Math.max(0, parseInt(step.sleep, 10) || 0);
      const durationMeta = parseDurationMeta(step.duration);
      const meta = ensureStepTimeline(step, cursorMs + sleepMs);
      if (!hasTimeline) {
        meta.start_ms = Math.max(0, cursorMs + sleepMs);
        meta.length_ms = Math.max(MIN_BLOCK_MS, parseInt(meta.length_ms, 10) || Math.max(MIN_BLOCK_MS, durationMeta.total_ms || 500));
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
    if (rulerPlayhead) rulerPlayhead.style.left = `${leftPx}px`;
    if (canvasPlayhead) canvasPlayhead.style.left = `${leftPx}px`;
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
        typeof window.isBackendMode === "function" &&
        window.isBackendMode() &&
        typeof buildBackendCuePayloadFromCurrentState === "function" &&
        typeof sendBackendCuePayload === "function"
      ) {
        await sendBackendCuePayload(buildBackendCuePayloadFromCurrentState());
        if (typeof syncBackendLiveGroups === "function") {
          await syncBackendLiveGroups();
        }
      } else if (typeof sendToEngineWithEffects === "function") {
        await sendToEngineWithEffects(1.0);
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
      typeof window.isBackendMode === "function" &&
      window.isBackendMode() &&
      typeof buildBackendCuePayloadFromCurrentState === "function" &&
      typeof sendBackendCuePayload === "function"
    ) {
      await sendBackendCuePayload(buildBackendCuePayloadFromCurrentState());
      return;
    }

    if (typeof sendToEngineWithEffects === "function") {
      await sendToEngineWithEffects(1.0, groupMix);
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
    backendAppliedPlanIndex = -1;
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
        const meta = ensureStepTimeline(step);
        meta.fade_operator = operator;
        hideTimelineContextMenu();
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
      meta.length_ms = Math.max(MIN_BLOCK_MS, dragState.length_ms + deltaMs);
    } else if (dragState.mode === "fade") {
      const localCursorMs = clamp(
        Math.round((event.clientX - dragState.origin_client_x) / pxPerMs()) + (dragState.fade_start_ms + dragState.fade_end_ms) / 2,
        0,
        meta.length_ms
      );
      const startDistance = Math.abs(localCursorMs - dragState.fade_start_ms);
      const endDistance = Math.abs(localCursorMs - dragState.fade_end_ms);
      if (startDistance <= endDistance) {
        meta.fade_start_ms = clamp(localCursorMs, 0, meta.fade_end_ms);
      } else {
        meta.fade_end_ms = clamp(localCursorMs, meta.fade_start_ms, meta.length_ms);
      }
    }

    syncStepTimelineBounds(step);
    resolveTimelineLaneConflicts(dragState.source_index);
    if (typeof window.renderCueTable === "function") window.renderCueTable();
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
      rulerPlayhead.style.left = `${leftPx}px`;
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
    canvasPlayhead.style.left = `${leftPx}px`;
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

      const fadeStartPercent = occurrence.length_ms > 0 ? (occurrence.fade_start_ms / occurrence.length_ms) * 100 : 0;
      const fadeEndPercent = occurrence.length_ms > 0 ? (occurrence.fade_end_ms / occurrence.length_ms) * 100 : 0;
      const graphPath = `M0,34 L${fadeStartPercent},34 L${fadeEndPercent},6 L100,6`;

      block.innerHTML = `
        <div class="timeline-block-header">
          <div class="timeline-block-name">${escapeHtml(occurrence.cue_name)}</div>
          <div class="timeline-block-meta">${formatMs(occurrence.length_ms)}</div>
        </div>
        <div class="timeline-block-body">
          <div class="timeline-block-graph">
            <svg viewBox="0 0 100 40" preserveAspectRatio="none">
              <path d="${graphPath}" fill="none" stroke="rgba(147, 197, 253, 0.95)" stroke-width="2" />
            </svg>
            <div class="timeline-fade-handle" style="left:${fadeStartPercent}%"></div>
            <div class="timeline-fade-handle" style="left:${fadeEndPercent}%"></div>
          </div>
          <div class="timeline-block-footer">
            <span>${formatMs(occurrence.start_ms)}</span>
            <span>${occurrence.fade_operator || "Cut"}</span>
          </div>
        </div>
        <div class="timeline-block-resize"></div>
      `;

      block.addEventListener("click", () => {
        selectTimelineCue(occurrence.source_index);
        if (typeof window.renderCueTable === "function") window.renderCueTable();
      });
      block.addEventListener("contextmenu", (ev) => {
        ev.preventDefault();
        selectTimelineCue(occurrence.source_index);
        openTimelineContextMenu(ev, occurrence);
      });
      block.querySelector(".timeline-block-resize")?.addEventListener("mousedown", (ev) => {
        ev.preventDefault();
        startTimelinePointerDrag("resize", occurrence, ev);
      });
      block.querySelector(".timeline-block-graph")?.addEventListener("mousedown", (ev) => {
        ev.preventDefault();
        startTimelinePointerDrag("fade", occurrence, ev);
      });
      block.addEventListener("mousedown", (ev) => {
        if (ev.button !== 0) return;
        if (ev.target instanceof HTMLElement && ev.target.closest(".timeline-block-resize, .timeline-block-graph")) return;
        ev.preventDefault();
        startTimelinePointerDrag("move", occurrence, ev);
      });

      canvas.appendChild(block);
    }

    applyTimelineViewStateToDom();
    bindTimelineViewportInteractions();
    updateTimelineSummary(occurrences, laneCount, totalDurationMs);
    updateTimelineZoomInputs();
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
