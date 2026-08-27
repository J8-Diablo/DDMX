// static/sync_video.js
// Sync Video integration (UI controls + cue actions)

(function() {
  const STORAGE_KEY = "dmx_sync_video_config";
  const DEFAULT_CONFIG = {
    enabled: false,
    baseUrl: "http://127.0.0.1:3000",
    token: ""
  };

  const ACTION_ENDPOINTS = {
    play: "/api/play",
    pause: "/api/pause",
    seek: "/api/seek",
    load_video: "/api/load-video",
    load_layout: "/api/load-layout"
  };

  function t(key, fallback) {
    if (typeof window.t === "function") return window.t(key, fallback);
    return fallback || key;
  }

  function tfmt(key, fallback, params) {
    if (typeof window.tfmt === "function") return window.tfmt(key, fallback, params);
    const template = t(key, fallback);
    if (!params) return template;
    return template.replace(/\{(\w+)\}/g, (_, k) => (params[k] == null ? "" : String(params[k])));
  }

  function toast(message, type = "info") {
    if (typeof window.toast === "function") return window.toast(message, type);
    console.log(`[${type}] ${message}`);
  }

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/\"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function normalizeBaseUrl(url) {
    return String(url || "").trim().replace(/\/+$/, "");
  }

  function isGuiWebView() {
    try {
      const ua = navigator.userAgent || "";
      if (ua.includes("QtWebEngine") || ua.includes("QtWebKit")) return true;
      const params = new URLSearchParams(window.location.search || "");
      if (params.get("gui") === "1") return true;
    } catch (err) {
      return false;
    }
    return false;
  }

  function bindSwitchVisuals(root) {
    const scope = root || document;
    const switches = scope.querySelectorAll(".switch");
    switches.forEach((sw) => {
      if (!(sw instanceof HTMLElement)) return;
      const input = sw.querySelector("input[type='checkbox']");
      if (!(input instanceof HTMLInputElement)) return;

      const syncState = () => {
        sw.classList.toggle("is-checked", !!input.checked);
        sw.classList.toggle("is-disabled", !!input.disabled);
      };

      syncState();
      if (sw.dataset.switchBound === "1") return;
      sw.dataset.switchBound = "1";

      input.addEventListener("change", syncState);
      input.addEventListener("input", syncState);
      input.addEventListener("click", () => {
        window.requestAnimationFrame(syncState);
      });
    });
  }
  window.bindSwitchVisuals = bindSwitchVisuals;

  const SETTINGS_ADVANCED_STORAGE_KEY = "dmx_settings_advanced";

  function getAppMeta() {
    const meta = window.APP_META || {};
    return {
      name: String(meta.name || "DDMX"),
      version: String(meta.version || "0.0.0"),
      licenseCode: String(meta.licenseCode || "CC BY-NC-SA 4.0"),
    };
  }

  function isAdvancedSettingsEnabled() {
    try {
      const raw = window.localStorage?.getItem(SETTINGS_ADVANCED_STORAGE_KEY);
      if (raw == null) return false;
      return raw !== "0";
    } catch (err) {
      return false;
    }
  }

  function setAdvancedSettingsEnabled(enabled) {
    const next = !!enabled;
    try {
      window.localStorage?.setItem(SETTINGS_ADVANCED_STORAGE_KEY, next ? "1" : "0");
    } catch (err) {
      console.warn("[SETTINGS] advanced persist failed:", err);
    }
    return next;
  }

  function markAdvancedSettingsSections(root) {
    const scope = root || document;
    const sections = Array.from(scope.querySelectorAll(".dmx-settings-form .dmx-settings-section"));
    sections.forEach((section, index) => {
      if (section.dataset.advanced === "true" || section.dataset.advanced === "false") {
        return;
      }
      section.dataset.advanced = index >= 3 ? "true" : "false";
    });
  }

  function applyAdvancedSettingsVisibility(root, enabled) {
    const scope = root || document;
    scope.querySelectorAll(".dmx-settings-section[data-advanced='true']").forEach((section) => {
      section.classList.toggle("settings-hidden", !enabled);
    });
  }

  function buildSettingsFooterInfo(enabled) {
    const meta = getAppMeta();
    const wrap = document.createElement("div");
    wrap.className = "settings-footer-info";
    wrap.innerHTML = `
      <label class="settings-footer-toggle">
        <strong>Advanced</strong>
        <span class="switch">
          <input id="settings-advanced-toggle" type="checkbox" ${enabled ? "checked" : ""}>
          <span class="slider"></span>
        </span>
      </label>
      <div class="settings-footer-meta">
        <span class="settings-footer-version">${escapeHtml(meta.name)} ${escapeHtml(meta.version)}</span>
        <span class="settings-footer-license">${escapeHtml(meta.licenseCode)}</span>
      </div>
    `;
    return wrap;
  }

  function upgradeSettingsCheckboxes(root) {
    const scope = root || document;
    const ids = [
      "whats-new-startup-enabled",
      "auto-update-startup-enabled",
      "sync-video-enabled",
      "ctc-enabled",
      "ctc-capture-release",
      "rt-smooth-predict",
      "rt-smooth-disable",
    ];
    ids.forEach((id) => {
      const input = scope.querySelector(`#${id}`);
      if (!(input instanceof HTMLInputElement)) return;
      if (input.closest(".switch")) return;

      const slider = document.createElement("span");
      slider.className = "slider";
      const switchWrap = document.createElement("span");
      switchWrap.className = "switch";
      input.replaceWith(switchWrap);
      switchWrap.appendChild(input);
      switchWrap.appendChild(slider);
    });
  }

  function mountAdvancedSettingsToggle(root, footerActions) {
    const scope = root || document;
    markAdvancedSettingsSections(scope);
    upgradeSettingsCheckboxes(scope);
    bindSwitchVisuals(scope);
    applyAdvancedSettingsVisibility(scope, isAdvancedSettingsEnabled());

    const footer = footerActions || scope.querySelector(".dmx-modal-actions, .swal2-actions");
    if (!footer || footer.querySelector("#settings-advanced-toggle")) return;

    const control = buildSettingsFooterInfo(isAdvancedSettingsEnabled());
    footer.prepend(control);
    bindSwitchVisuals(footer);

    control.querySelector("#settings-advanced-toggle")?.addEventListener("change", (ev) => {
      const next = setAdvancedSettingsEnabled(!!ev.target?.checked);
      applyAdvancedSettingsVisibility(scope, next);
      bindSwitchVisuals(footer);
    });
  }

  function bindRuntimeRangeListeners() {
    bindSwitchVisuals(document);
    bindSegmentedChoice(document, "cue-editor-view-mode");

    const maxSend = document.getElementById("rt-max-send-hz");
    const maxSendValue = document.getElementById("rt-max-send-hz-value");
    if (maxSend && maxSendValue) {
      maxSend.addEventListener("input", () => {
        maxSendValue.textContent = `${maxSend.value} Hz`;
      });
    }

    const heartbeat = document.getElementById("rt-heartbeat-sec");
    const heartbeatValue = document.getElementById("rt-heartbeat-sec-value");
    if (heartbeat && heartbeatValue) {
      heartbeat.addEventListener("input", () => {
        heartbeatValue.textContent = `${Number(heartbeat.value).toFixed(2)} s`;
      });
    }

    const smooth = document.getElementById("rt-smooth-step");
    const smoothValue = document.getElementById("rt-smooth-step-value");
    if (smooth && smoothValue) {
      smooth.addEventListener("input", () => {
        smoothValue.textContent = `${smooth.value}`;
      });
    }

    const dead = document.getElementById("rt-deadband");
    const deadValue = document.getElementById("rt-deadband-value");
    if (dead && deadValue) {
      dead.addEventListener("input", () => {
        deadValue.textContent = `${dead.value}`;
      });
    }

    const quant = document.getElementById("rt-quantize");
    const quantValue = document.getElementById("rt-quantize-value");
    if (quant && quantValue) {
      quant.addEventListener("input", () => {
        quantValue.textContent = `${quant.value}`;
      });
    }

    const playbackUiFps = document.getElementById("rt-playback-ui-fps");
    const playbackUiFpsValue = document.getElementById("rt-playback-ui-fps-value");
    if (playbackUiFps && playbackUiFpsValue) {
      playbackUiFps.addEventListener("input", () => {
        playbackUiFpsValue.textContent = `${playbackUiFps.value} fps`;
      });
    }

    const playbackEngineHz = document.getElementById("rt-playback-engine-hz");
    const playbackEngineHzValue = document.getElementById("rt-playback-engine-hz-value");
    if (playbackEngineHz && playbackEngineHzValue) {
      playbackEngineHz.addEventListener("input", () => {
        playbackEngineHzValue.textContent = `${playbackEngineHz.value} Hz`;
      });
    }

    const cueZoomX = document.getElementById("cue-editor-zoom-x");
    const cueZoomXValue = document.getElementById("cue-editor-zoom-x-value");
    if (cueZoomX && cueZoomXValue) {
      cueZoomX.addEventListener("input", () => {
        cueZoomXValue.textContent = `${cueZoomX.value} px/s`;
      });
    }

    const cueZoomY = document.getElementById("cue-editor-zoom-y");
    const cueZoomYValue = document.getElementById("cue-editor-zoom-y-value");
    if (cueZoomY && cueZoomYValue) {
      cueZoomY.addEventListener("input", () => {
        cueZoomYValue.textContent = `${cueZoomY.value} px`;
      });
    }

    const autolightOverrideTimeout = document.getElementById("autolight-override-timeout");
    const autolightOverrideTimeoutValue = document.getElementById("autolight-override-timeout-value");
    if (autolightOverrideTimeout && autolightOverrideTimeoutValue) {
      autolightOverrideTimeout.addEventListener("input", () => {
        autolightOverrideTimeoutValue.textContent = `${autolightOverrideTimeout.value} ms`;
      });
    }

    const autolightConfidence = document.getElementById("autolight-confidence-threshold");
    const autolightConfidenceValue = document.getElementById("autolight-confidence-threshold-value");
    if (autolightConfidence && autolightConfidenceValue) {
      autolightConfidence.addEventListener("input", () => {
        autolightConfidenceValue.textContent = Number(autolightConfidence.value).toFixed(2);
      });
    }

    const autolightEnergy = document.getElementById("autolight-energy-sensitivity");
    const autolightEnergyValue = document.getElementById("autolight-energy-sensitivity-value");
    if (autolightEnergy && autolightEnergyValue) {
      autolightEnergy.addEventListener("input", () => {
        autolightEnergyValue.textContent = Number(autolightEnergy.value).toFixed(2);
      });
    }

    const autolightMovement = document.getElementById("autolight-movement-sensitivity");
    const autolightMovementValue = document.getElementById("autolight-movement-sensitivity-value");
    if (autolightMovement && autolightMovementValue) {
      autolightMovement.addEventListener("input", () => {
        autolightMovementValue.textContent = Number(autolightMovement.value).toFixed(2);
      });
    }

    const smoothDisable = document.getElementById("rt-smooth-disable");
    const smoothPredict = document.getElementById("rt-smooth-predict");
    const smoothStep = document.getElementById("rt-smooth-step");
    const smoothValueEl = document.getElementById("rt-smooth-step-value");

    const updateSmoothState = () => {
      if (!smoothDisable || !smoothStep) return;
      const off = smoothDisable.checked;
      smoothStep.disabled = off;
      if (smoothValueEl) smoothValueEl.classList.toggle("muted", off);
      if (smoothPredict) {
        smoothPredict.disabled = off;
        if (off) smoothPredict.checked = false;
      }
      bindSwitchVisuals(document);
    };

    if (smoothDisable) smoothDisable.addEventListener("change", updateSmoothState);
    updateSmoothState();
  }

  function bindSegmentedChoice(root, inputId) {
    const scope = root || document;
    const input = scope.querySelector(`#${inputId}`);
    if (!(input instanceof HTMLInputElement)) return;
    const group = input.closest(".dmx-segmented");
    if (!(group instanceof HTMLElement) || group.dataset.bound === "1") return;
    group.dataset.bound = "1";

    const buttons = Array.from(group.querySelectorAll(".dmx-segmented-btn"));
    const syncState = () => {
      const current = String(input.value || "").trim().toLowerCase();
      buttons.forEach((btn) => {
        const value = String(btn.getAttribute("data-value") || "").trim().toLowerCase();
        btn.classList.toggle("active", value === current);
      });
    };

    buttons.forEach((btn) => {
      btn.addEventListener("click", () => {
        input.value = String(btn.getAttribute("data-value") || "classic");
        syncState();
      });
    });

    syncState();
  }

  function readSettingsForm(showValidation) {
    const dmxTargetIp = document.getElementById("dmx-target-ip")?.value.trim();
    const enabled = document.getElementById("sync-video-enabled")?.checked || false;
    const baseUrl = normalizeBaseUrl(document.getElementById("sync-video-url")?.value || "");
    const token = document.getElementById("sync-video-token")?.value.trim() || "";
    const whatsNewShowOnStartup = document.getElementById("whats-new-startup-enabled")?.checked ?? true;
    const autoUpdateCheckOnStartup = document.getElementById("auto-update-startup-enabled")?.checked ?? true;
    const ctcEnabled = document.getElementById("ctc-enabled")?.checked || false;
    const ctcCaptureRelease = document.getElementById("ctc-capture-release")?.checked || false;
    const ctcKeybindInput = document.getElementById("ctc-keybind");
    const ctcKeybindRaw = ctcKeybindInput?.dataset?.keybind || ctcKeybindInput?.value || "F8";
    const ctcKeybind = typeof window.normalizeCtcKeybindValue === "function"
      ? window.normalizeCtcKeybindValue(ctcKeybindRaw)
      : String(ctcKeybindRaw || "F8").trim() || "F8";
    const cueEditorSettings = {
      view_mode: document.getElementById("cue-editor-view-mode")?.value || "classic",
      timeline_priority_mode: document.getElementById("cue-editor-priority-mode")?.value || "top",
      zoom_x: parseFloat(document.getElementById("cue-editor-zoom-x")?.value || "120"),
      zoom_y: parseFloat(document.getElementById("cue-editor-zoom-y")?.value || "88"),
    };
    const autolightSettings = {
      enabled: document.getElementById("autolight-enabled")?.checked || false,
      mode: document.getElementById("autolight-mode")?.value || "live",
      source_mode: document.getElementById("autolight-source-mode")?.value || "player_metadata_then_local",
      freeze_global: document.getElementById("autolight-freeze-global")?.checked || false,
      allow_guarded_channels: document.getElementById("autolight-allow-guarded")?.checked || false,
      snapshot_auto_capture: document.getElementById("autolight-snapshot-auto")?.checked || false,
      override_timeout_ms: parseInt(document.getElementById("autolight-override-timeout")?.value || "5000", 10),
      confidence_threshold: parseFloat(document.getElementById("autolight-confidence-threshold")?.value || "0.75"),
      energy_sensitivity: parseFloat(document.getElementById("autolight-energy-sensitivity")?.value || "1"),
      movement_sensitivity: parseFloat(document.getElementById("autolight-movement-sensitivity")?.value || "1"),
    };

    const runtimeSettings = {
      emit_hz: parseFloat(document.getElementById("rt-emit-hz")?.value || "500"),
      preview_hz: parseFloat(document.getElementById("rt-preview-hz")?.value || "30"),
      playback_clock_mode: document.getElementById("rt-playback-clock-mode")?.value || "timeline",
      playback_engine_hz: parseFloat(document.getElementById("rt-playback-engine-hz")?.value || "120"),
      playback_ui_fps: parseFloat(document.getElementById("rt-playback-ui-fps")?.value || "12"),
      artnet_diff: document.getElementById("rt-artnet-diff")?.checked || false,
      artnet_heartbeat_full: document.getElementById("rt-artnet-heartbeat-full")?.checked || false,
      dummy_enabled: document.getElementById("rt-dummy")?.checked || false,
      continuous: document.getElementById("rt-continuous")?.checked || false,
      ui_force_full_send: document.getElementById("rt-ui-force-full")?.checked || false,
      max_send_hz: parseFloat(document.getElementById("rt-max-send-hz")?.value || "40"),
      heartbeat_sec: parseFloat(document.getElementById("rt-heartbeat-sec")?.value || "0.1"),
      smooth_step: parseInt(document.getElementById("rt-smooth-step")?.value || "2", 10),
      smooth_predict: document.getElementById("rt-smooth-predict")?.checked || false,
      smooth_disable: document.getElementById("rt-smooth-disable")?.checked || false,
      deadband: parseInt(document.getElementById("rt-deadband")?.value || "0", 10),
      quantize: parseInt(document.getElementById("rt-quantize")?.value || "1", 10),
      log_ui: document.getElementById("rt-log-ui")?.checked || false,
      log_ui_full: document.getElementById("rt-log-ui-full")?.checked || false,
      log_dmx: document.getElementById("rt-log-dmx")?.checked || false,
      log_dmx_full: document.getElementById("rt-log-dmx-full")?.checked || false,
      log_artnet: document.getElementById("rt-log-artnet")?.checked || false,
      log_artnet_full: document.getElementById("rt-log-artnet-full")?.checked || false,
      profile_runner: document.getElementById("rt-profile-runner")?.checked || false,
    };

    if (!dmxTargetIp) {
      if (typeof showValidation === "function") {
        showValidation(t("settings.dmxIpRequired", "DMX target IP required"));
      }
      return null;
    }
    if (enabled && !baseUrl) {
      if (typeof showValidation === "function") {
        showValidation(t("settings.urlRequired", "URL required when Sync Video enabled"));
      }
      return null;
    }
    if (runtimeSettings.smooth_disable) {
      runtimeSettings.smooth_predict = false;
    }
    return {
      enabled,
      baseUrl,
      token,
      dmxTargetIp,
      whatsNewSettings: {
        show_on_startup: whatsNewShowOnStartup,
      },
      autoUpdateSettings: {
        check_on_startup: autoUpdateCheckOnStartup,
      },
      autolightSettings,
      cueEditorSettings,
      runtimeSettings,
      ctcSettings: {
        enabled: ctcEnabled,
        keybind: ctcKeybind,
        capture_release: ctcCaptureRelease,
      }
    };
  }

  async function applySettingsResult(result) {
    if (!result) return;
    const {
      enabled,
      baseUrl,
      token,
      dmxTargetIp,
      runtimeSettings,
      ctcSettings,
      whatsNewSettings,
      autoUpdateSettings,
      autolightSettings,
      cueEditorSettings,
    } = result;
    const ok = await saveDmxSettings(
      dmxTargetIp,
      { enabled, baseUrl, token },
      runtimeSettings,
      ctcSettings,
      whatsNewSettings,
      autoUpdateSettings,
      autolightSettings,
      cueEditorSettings
    );
    if (!ok) return;
    setConfig({ enabled, baseUrl, token });
    if (runtimeSettings && typeof window.setPlaybackUiFps === "function") {
      window.setPlaybackUiFps(runtimeSettings.playback_ui_fps);
    }
    if (ctcSettings && typeof window.setCtcSettings === "function") {
      window.setCtcSettings(ctcSettings);
    }
    if (cueEditorSettings && typeof window.setCueEditorSettings === "function") {
      window.setCueEditorSettings(cueEditorSettings);
    }
  }

  function formatCtcKeybind(code) {
    if (typeof window.formatCtcKeybindDisplay === "function") {
      return window.formatCtcKeybindDisplay(code);
    }
    return String(code || "F8").trim() || "F8";
  }

  function bindCtcKeybindInput(root) {
    const scope = root || document;
    const input = scope.querySelector("#ctc-keybind");
    if (!input || input.dataset.bound === "1") return;
    input.dataset.bound = "1";

    const assignKeybind = (raw) => {
      const normalized = typeof window.normalizeCtcKeybindValue === "function"
        ? window.normalizeCtcKeybindValue(raw)
        : String(raw || "F8").trim() || "F8";
      input.dataset.keybind = normalized;
      input.value = formatCtcKeybind(normalized);
    };

    if (!input.dataset.keybind) {
      assignKeybind(input.value || "F8");
    }

    input.addEventListener("keydown", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();

      if (ev.key === "Backspace" || ev.key === "Delete") {
        assignKeybind("F8");
        return;
      }

      assignKeybind(ev.code || ev.key || "F8");
    });

    input.addEventListener("focus", () => {
      input.select();
    });
  }

  function openSettingsModalGui(title, html) {
    const existing = document.getElementById("dmx-settings-overlay");
    if (existing) existing.remove();

    const overlay = document.createElement("div");
    overlay.id = "dmx-settings-overlay";
    overlay.className = "dmx-modal-overlay";
    overlay.innerHTML = `
      <div class="dmx-modal">
        <div class="dmx-modal-header">
          <div class="dmx-modal-title">${escapeHtml(title)}</div>
          <button class="dmx-modal-close" type="button" aria-label="Close">×</button>
        </div>
        <div class="dmx-modal-body"></div>
        <div class="dmx-modal-actions">
          <div class="dmx-modal-actions-right">
            <button class="secondary dmx-modal-cancel" type="button">${t("settings.cancel", "Cancel")}</button>
            <button class="primary dmx-modal-save" type="button">${t("settings.save", "Save")}</button>
          </div>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);

    const body = overlay.querySelector(".dmx-modal-body");
    if (body) body.innerHTML = html;

    bindRuntimeRangeListeners();
    bindCtcKeybindInput(overlay);
    mountAdvancedSettingsToggle(overlay, overlay.querySelector(".dmx-modal-actions"));
    bindAppUpdateControls(overlay);
    fetchAppUpdateStatus({ root: overlay });

    const close = () => {
      overlay.remove();
    };

    overlay.addEventListener("click", (ev) => {
      if (ev.target === overlay) close();
    });

    overlay.querySelector(".dmx-modal-close")?.addEventListener("click", close);
    overlay.querySelector(".dmx-modal-cancel")?.addEventListener("click", close);

    overlay.querySelector(".dmx-modal-save")?.addEventListener("click", async () => {
      const result = readSettingsForm((msg) => toast(msg, "error"));
      if (!result) return;
      await applySettingsResult(result);
      close();
    });
  }

  function loadConfig() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return { ...DEFAULT_CONFIG };
      const parsed = JSON.parse(raw);
      return {
        enabled: Boolean(parsed.enabled),
        baseUrl: parsed.baseUrl || DEFAULT_CONFIG.baseUrl,
        token: parsed.token || ""
      };
    } catch (err) {
      return { ...DEFAULT_CONFIG };
    }
  }

  function saveConfig(cfg) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(cfg));
    } catch (err) {
      // ignore storage issues
    }
  }

  let syncConfig = loadConfig();
  let dmxSettingsCache = null;
  let lastDisabledWarnTs = 0;
  const DISABLED_WARN_INTERVAL_MS = 60000;
  let appUpdateStatusCache = null;
  let appUpdateRequest = null;
  let autolightStatusCache = null;
  let autolightPollHandle = null;

  function updateSyncVideoSection() {
    const section = document.getElementById("sync-video-section");
    if (!section) return;
    section.classList.toggle("is-hidden", !syncConfig.enabled);

    const status = document.getElementById("sync-video-status");
    if (status) {
      if (syncConfig.enabled) {
        const base = normalizeBaseUrl(syncConfig.baseUrl);
        status.textContent = `${t("syncVideo.statusOn", "On")} ${base}`;
      } else {
        status.textContent = t("syncVideo.statusOff", "Off");
      }
    }
  }

  function setConfig(partial) {
    syncConfig = {
      ...syncConfig,
      ...partial
    };
    saveConfig(syncConfig);
    updateSyncVideoSection();
  }

  function formatEta(seconds) {
    const s = Math.max(0, Math.round(Number(seconds) || 0));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    const r = s % 60;
    return `${m}:${String(r).padStart(2, "0")}`;
  }

  function updateAutolightSection(statusLike) {
    const section = document.getElementById("autolight-section");
    if (!section) return;

    const status = (statusLike && typeof statusLike === "object")
      ? statusLike
      : ((dmxSettingsCache && dmxSettingsCache.autolight_status) || autolightStatusCache || {});
    autolightStatusCache = status;

    // AutoLight 2.0 DJ view (beat-grid + intent), if the new pipeline is live.
    try { updateAutolightDjView((status.render && status.render.dj) || {}); } catch (e) {}
    try { syncGuardrailControls((dmxSettingsCache && dmxSettingsCache.autolight) || {}); } catch (e) {}

    const badge = document.getElementById("autolight-status-badge");
    const summaryLine = document.getElementById("autolight-summary-line");
    const trackLine = document.getElementById("autolight-track-line");
    const sourceLine = document.getElementById("autolight-source-line");
    const freezeToggle = document.getElementById("autolight-freeze-toggle");
    const modeOff = document.getElementById("autolight-mode-off");
    const modeAssist = document.getElementById("autolight-mode-assist");
    const modeLive = document.getElementById("autolight-mode-live");

    const mode = String(status.mode || "off");
    const enabled = Boolean(status.enabled);
    const sourceState = String(status.source_state || "idle");
    const track = (status.track && typeof status.track === "object") ? status.track : {};
    const runningPlayers = Array.isArray(status.running_players) ? status.running_players : [];

    if (badge) {
      let badgeText = "Idle";
      if (!enabled || mode === "off") {
        badgeText = "Off";
      } else if (sourceState === "error") {
        badgeText = "Error";
      } else if (mode === "assist") {
        badgeText = "Assist";
      } else if (mode === "live") {
        badgeText = "Live";
      }
      badge.textContent = badgeText;
      badge.classList.remove("is-live", "is-assist", "is-off", "is-error");
      badge.classList.add(
        !enabled || mode === "off"
          ? "is-off"
          : sourceState === "error"
            ? "is-error"
            : mode === "assist"
              ? "is-assist"
              : "is-live"
      );
    }

    // Collapse the entire AutoLight panel when it's off — keeps the page
    // tidy until the user decides to re-enable it. The header + "Enable"
    // button stay visible so the panel never disappears entirely.
    const collapsed = !enabled || mode === "off";
    section.classList.toggle("is-collapsed", collapsed);
    const enableBtn = document.getElementById("autolight-quick-enable");
    if (enableBtn) {
      enableBtn.hidden = !collapsed;
    }

    if (summaryLine) {
      summaryLine.textContent = String(status.summary || "Auto-Light ready");
    }
    if (trackLine) {
      const title = String(track.title || "").trim();
      const artist = String(track.artist || "").trim();
      trackLine.textContent = title
        ? `Track: ${title}${artist ? ` - ${artist}` : ""}`
        : "Track metadata unavailable";
    }
    const phaseLine = document.getElementById("autolight-phase-line");
    if (phaseLine) {
      const struc = (status.render && status.render.director && status.render.director.structural) || {};
      if (struc.phase && struc.position_valid) {
        const pct = Math.round((Number(struc.song_progress) || 0) * 100);
        const next = struc.next_phase
          ? ` · next: ${struc.next_phase} in ${formatEta(Number(struc.next_phase_eta_s) || 0)}`
          : "";
        const tag = struc.source === "replay" ? " [learned]" : "";
        phaseLine.textContent = `Structure: ${struc.phase} (${pct}%)${next}${tag}`;
      } else if (struc.track_id && !struc.input_position_ms) {
        // Track recognised but the player isn't reporting timeline position.
        // Helps the user diagnose "why isn't structure detection working".
        phaseLine.textContent = "Structure: — (player reports no position)";
      } else if (struc.track_id && !struc.auto_locked) {
        phaseLine.textContent = "Structure: — (analysing BPM…)";
      } else {
        phaseLine.textContent = "Structure: —";
      }
    }
    if (sourceLine) {
      sourceLine.textContent = runningPlayers.length
        ? `Detected players: ${runningPlayers.join(", ")}`
        : "No media player detected";
    }
    if (freezeToggle) {
      freezeToggle.checked = Boolean(status.freeze_global);
    }
    // Render-mode (pipeline) tri-state: highlight the active button.
    const renderMode = String(
      (status.render && status.render.render_mode)
      || (dmxSettingsCache && dmxSettingsCache.autolight && dmxSettingsCache.autolight.render_mode)
      || "director"
    ).toLowerCase();
    const renderButtons = {
      director: document.getElementById("autolight-render-director"),
      effects: document.getElementById("autolight-render-effects"),
      off: document.getElementById("autolight-render-off"),
    };
    Object.entries(renderButtons).forEach(([key, btn]) => {
      if (!btn) return;
      btn.classList.toggle("is-active", key === renderMode);
    });

    const memoryToggle = document.getElementById("autolight-memory-toggle");
    if (memoryToggle) {
      const memCached = Boolean(
        (status.render && status.render.memory_persistence)
        ?? (dmxSettingsCache && dmxSettingsCache.autolight && dmxSettingsCache.autolight.memory_persistence)
        ?? false
      );
      if (memoryToggle.dataset.userInteracting !== "1" && memoryToggle.checked !== memCached) {
        memoryToggle.checked = memCached;
      }
    }

    const setModeButtonState = (button, active) => {
      if (!button) return;
      button.classList.toggle("is-active", active);
    };
    setModeButtonState(modeOff, !enabled || mode === "off");
    setModeButtonState(modeAssist, enabled && mode === "assist");
    setModeButtonState(modeLive, enabled && mode === "live");

    const audio = (status.audio && typeof status.audio === "object") ? status.audio : {};
    const render = (status.render && typeof status.render === "object") ? status.render : {};
    updateAutolightAudioVisuals(audio, render);

    const deviceSelect = document.getElementById("autolight-audio-device");
    if (deviceSelect) {
      const currentRaw = render.audio_device_index;
      const targetValue = currentRaw === null || currentRaw === undefined ? "default" : String(currentRaw);
      if (deviceSelect.dataset.userInteracting !== "1" && deviceSelect.value !== targetValue) {
        deviceSelect.value = targetValue;
      }
    }

    const diagLine = document.getElementById("autolight-diag-line");
    if (diagLine) {
      const seen = Number(render.devices_seen || 0);
      const controllable = Number(render.devices_controllable || 0);
      const wrote = Number(render.last_frame_wrote || 0);
      const frameMode = String(render.last_frame_mode || "off");
      const engineAttached = Boolean(render.engine_attached);
      const deviceName = String(render.audio_device_name || "—");
      const skippedByFade = Number(render.skipped_by_fade || 0);
      const parts = [];
      parts.push(`Rig: ${controllable}/${seen}`);
      parts.push(`writes: ${wrote}`);
      parts.push(`mode: ${frameMode}`);
      if (skippedByFade > 0) parts.push(`yield: ${skippedByFade}`);
      parts.push(`audio: ${deviceName || "—"}`);
      diagLine.textContent = parts.join(" | ");
      const problem =
        !engineAttached ||
        (enabled && mode === "live" && seen === 0) ||
        (enabled && mode === "live" && seen > 0 && controllable === 0) ||
        (enabled && mode === "live" && controllable > 0 && wrote === 0 && audio.active);
      diagLine.classList.toggle("is-warn", problem);
    }

    const sceneLine = document.getElementById("autolight-scene-line");
    if (sceneLine) {
      const structure = (status.structure && typeof status.structure === "object") ? status.structure : {};
      const scene = String(render.scene || "SILENT");
      const label = String(structure.label || scene.toLowerCase());
      const drop = Number(structure.drop_score || 0);
      const slope = Number(structure.build_up_slope || 0);
      const longRms = Number(structure.long_rms || 0);
      sceneLine.textContent = `Scene: ${scene} (${label}) · drop ${drop.toFixed(2)} · build ${slope >= 0 ? "+" : ""}${slope.toFixed(3)} · lvl ${longRms.toFixed(3)}`;
    }

    const topoLine = document.getElementById("autolight-topology-line");
    if (topoLine) {
      const topo = (render.topology && typeof render.topology === "object") ? render.topology : {};
      const pairs = Number(topo.mirror_pair_count || 0);
      const cluster = String(topo.cluster_summary || "—");
      const hasPos = Boolean(topo.has_positions);
      const suffix = hasPos ? "" : " (no positions)";
      topoLine.textContent = `Topology: ${pairs} mirror pair${pairs === 1 ? "" : "s"} · ${cluster}${suffix}`;
    }

    const bpm = Number(audio.bpm || 0);
    const src = String(audio.bpm_source || "auto");
    const method = String(audio.bpm_method || "median");
    const effectLine = document.getElementById("autolight-effect-line");
    if (effectLine) {
      const effect = (render.effect && typeof render.effect === "object") ? render.effect : {};
      const conf = Number(audio.bpm_confidence || 0);
      const barCount = Number(audio.bar_count || 0);
      const activeName = effect.active ? String(effect.active) : "—";
      const triggerCount = Number(effect.trigger_count || 0);
      const methodTag = src === "tap" ? "tap" : (method === "autocorr" ? "ac" : "med");
      const bpmLabel = bpm >= 50
        ? `${bpm.toFixed(0)} BPM (${methodTag} ${Math.round(conf * 100)}%)`
        : "— BPM";
      effectLine.textContent = `Effect: ${activeName} · ${bpmLabel} · bar ${barCount} · ${triggerCount} fired`;
      effectLine.classList.toggle("is-active", Boolean(effect.active));
    }

    // Scene-lock button state
    const al = (dmxSettingsCache && dmxSettingsCache.autolight) || {};
    const lock = al.scene_lock || {};
    const lockedScene = String(lock.scene || "").toUpperCase();
    document.querySelectorAll("[data-scene-lock]").forEach((btn) => {
      btn.classList.toggle("is-active", (btn.dataset.sceneLock || "") === lockedScene);
    });

    // Genre + tap tempo buttons
    const genreSelect = document.getElementById("autolight-genre-select");
    if (genreSelect && genreSelect.dataset.userInteracting !== "1") {
      const g = String(al.genre_preset || "auto");
      if (genreSelect.value !== g) genreSelect.value = g;
    }
    const tapBtn = document.getElementById("autolight-tap-tempo");
    if (tapBtn) {
      // Pulse on every beat when in tap mode
      if (audio.beat && tapBtn.dataset.lastBeatCount !== String(audio.beat_count)) {
        tapBtn.classList.add("is-pulsing");
        window.clearTimeout(tapBtn._beatTimer);
        tapBtn._beatTimer = window.setTimeout(() => tapBtn.classList.remove("is-pulsing"), 80);
        tapBtn.dataset.lastBeatCount = String(audio.beat_count);
      }
      tapBtn.textContent = src === "tap" ? `Tap ${bpm.toFixed(0)}` : "Tap tempo";
    }

    const musicLine = document.getElementById("autolight-music-line");
    if (musicLine) {
      const music = (status.music && typeof status.music === "object") ? status.music : {};
      if (music.has_analysis) {
        musicLine.textContent = `Music DB: ${music.source} · ${music.title} · ${music.sample_count} samples`;
        musicLine.classList.add("is-active");
        musicLine.classList.remove("is-warn");
      } else if (music.pending) {
        musicLine.textContent = `Music DB: fetching…`;
        musicLine.classList.remove("is-active", "is-warn");
      } else if (music.soundcloud_configured) {
        musicLine.textContent = `Music DB: none matched${music.last_error ? ` (${music.last_error})` : ""}`;
        musicLine.classList.remove("is-active");
        musicLine.classList.toggle("is-warn", Boolean(music.last_error));
      } else {
        musicLine.textContent = `Music DB: disabled (no SoundCloud client_id)`;
        musicLine.classList.remove("is-active", "is-warn");
      }
    }

    const scInput = document.getElementById("autolight-soundcloud-client-id");
    if (scInput && scInput.dataset.userInteracting !== "1") {
      const saved = (dmxSettingsCache && dmxSettingsCache.autolight && dmxSettingsCache.autolight.soundcloud_client_id) || "";
      if (scInput.value !== saved) scInput.value = saved;
    }
  }

  // ---------------------------------------------------------------------
  // Audio tuning + genre + tap tempo + scene lock + spectrum
  // ---------------------------------------------------------------------

  let autolightMoodCache = [];
  let autolightGenreCache = [];
  let autolightTuningDefaults = {};
  let autolightTuningPending = {};
  let autolightTapTimestamps = [];

  const TUNING_FIELD_DEFS = [
    { key: "active_rms_floor",     label: "Silence floor (RMS)",   min: 0.002, max: 0.08,  step: 0.001, fixed: 4 },
    { key: "long_rms_floor",       label: "Level-0 RMS floor",      min: 0.001, max: 0.08,  step: 0.001, fixed: 4 },
    { key: "beat_min_bass",        label: "Beat floor (bass)",     min: 0.0005, max: 0.03, step: 0.0005, fixed: 4 },
    { key: "beat_spike_ratio",     label: "Beat spike ratio",      min: 1.05,  max: 3.0,   step: 0.05,  fixed: 2 },
    { key: "beat_refractory_ms",   label: "Beat refractory (ms)",  min: 80,    max: 600,   step: 10,    fixed: 0 },
    { key: "bass_baseline_tau_s",  label: "Bass baseline τ (s)",   min: 0.1,   max: 3.0,   step: 0.1,   fixed: 1 },
    { key: "bass_band_lo",         label: "Bass band lo (Hz)",     min: 10,    max: 200,   step: 5,     fixed: 0 },
    { key: "bass_band_hi",         label: "Bass band hi (Hz)",     min: 100,   max: 600,   step: 10,    fixed: 0 },
    { key: "mid_band_lo",          label: "Mid band lo (Hz)",      min: 80,    max: 800,   step: 10,    fixed: 0 },
    { key: "mid_band_hi",          label: "Mid band hi (Hz)",      min: 800,   max: 4000,  step: 50,    fixed: 0 },
    { key: "treble_band_lo",       label: "Treble band lo (Hz)",   min: 1000,  max: 6000,  step: 100,   fixed: 0 },
    { key: "treble_band_hi",       label: "Treble band hi (Hz)",   min: 3000,  max: 18000, step: 200,   fixed: 0 },
    { key: "level_chorus_floor",   label: "Chorus RMS floor",      min: 0.005, max: 0.15,  step: 0.001, fixed: 3 },
    { key: "level_high_floor",     label: "High RMS floor",        min: 0.02,  max: 0.20,  step: 0.001, fixed: 3 },
    { key: "drop_score_min",       label: "Drop spike ratio",      min: 1.1,   max: 4.0,   step: 0.05,  fixed: 2 },
    { key: "drop_rms_min",         label: "Drop RMS min",          min: 0.005, max: 0.15,  step: 0.001, fixed: 3 },
    { key: "bpm_window_beats",     label: "BPM window (beats)",    min: 3,     max: 30,    step: 1,     fixed: 0 },
    { key: "bpm_min",              label: "BPM search min",        min: 30,    max: 120,   step: 1,     fixed: 0 },
    { key: "bpm_max",              label: "BPM search max",        min: 120,   max: 300,   step: 1,     fixed: 0 },
  ];

  // Reflect saved guardrail settings into the mini-panel controls. Skips any
  // control the user is currently dragging/focusing so we never fight input.
  function syncGuardrailControls(s) {
    s = s || {};
    const focused = document.activeElement;
    const setRange = (id, valId, ratio) => {
      const el = document.getElementById(id);
      if (!el || el === focused) return;
      const pct = Math.round((Number(ratio)) * 100);
      el.value = pct;
      const lbl = document.getElementById(valId);
      if (lbl) lbl.textContent = `${pct}%`;
    };
    const setCheck = (id, v) => {
      const el = document.getElementById(id);
      if (el && el !== focused) el.checked = Boolean(v);
    };
    if (s.intensity_ceiling != null) setRange("dj-ceiling", "dj-ceiling-val", s.intensity_ceiling);
    if (s.contrast != null) setRange("dj-contrast", "dj-contrast-val", s.contrast);
    setCheck("dj-small-venue", s.small_venue);
    if (s.allow_strobe != null) setCheck("dj-allow-strobe", s.allow_strobe);
    if (s.metadata_enabled != null) setCheck("dj-metadata", s.metadata_enabled);
  }

  // AutoLight 2.0 "DJ view": render the beat-grid + intent readout from the
  // `render.dj` diagnostics block. Degrades quietly when the field is absent
  // (legacy pipeline / no audio).
  let _djLastBeat = -1;
  function updateAutolightDjView(dj) {
    const wrap = document.getElementById("autolight-dj");
    if (!wrap) return;
    dj = dj || {};
    const grid = dj.grid || {};
    const has = Object.keys(grid).length > 0 || dj.intent;
    wrap.classList.toggle("is-empty", !has);

    const setText = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    const bpm = Number(grid.bpm || 0);
    setText("dj-bpm", bpm > 0 ? bpm.toFixed(1) : "--");
    setText("dj-bar", grid.bar_index != null ? grid.bar_index : "–");
    setText("dj-phrase", grid.phrase_index != null ? grid.phrase_index : "–");
    setText("dj-phrase-len", grid.phrase_len || 16);

    const lock = document.getElementById("dj-lock");
    if (lock) {
      const locked = Boolean(grid.locked);
      const conf = Math.round((Number(grid.confidence) || 0) * 100);
      lock.textContent = locked ? `locked ${conf}%` : "unlocked";
      lock.classList.toggle("is-locked", locked);
    }

    const intentEl = document.getElementById("dj-intent");
    if (intentEl) {
      const intent = String(dj.intent || "—");
      intentEl.textContent = intent.toUpperCase();
      intentEl.className = "dj-intent dj-intent-" + intent;
    }

    const drop = document.getElementById("dj-drop");
    if (drop) {
      const btd = Number(dj.bars_to_drop);
      drop.textContent = (dj.intent === "build" && btd >= 0)
        ? `drop in ${btd} bar${btd === 1 ? "" : "s"}`
        : "";
    }

    const buildFill = document.getElementById("dj-build-fill");
    if (buildFill) {
      buildFill.style.width = `${Math.round((Number(dj.build_progress) || 0) * 100)}%`;
    }

    // Flash the beat dot on each new beat.
    const beat = document.getElementById("dj-beat");
    if (beat && grid.beat_index != null && grid.beat_index !== _djLastBeat) {
      _djLastBeat = grid.beat_index;
      beat.classList.toggle("is-downbeat", Boolean(grid.is_downbeat));
      beat.classList.remove("pulse");
      void beat.offsetWidth;  // restart CSS animation
      beat.classList.add("pulse");
    }
  }

  // Audio-reactive visuals (meters + beat indicator + drum pills + spectrum).
  // Called from both the slow full-status poll and the fast audio-only poll.
  // `render` is optional — fast path passes null and we skip the global_pulse
  // glow fallback (the audio.beat_count signal is what really drives pulses).
  function updateAutolightAudioVisuals(audio, render) {
    audio = audio || {};
    const bassFill = document.getElementById("autolight-meter-bass");
    const midFill = document.getElementById("autolight-meter-mid");
    const trebleFill = document.getElementById("autolight-meter-treble");
    const beatEl = document.getElementById("autolight-beat-indicator");
    const clampPct = (v) => Math.max(0, Math.min(100, v * 100));
    // Prefer adaptive p95 normalization when the analyzer publishes it; fall
    // back to the legacy hardcoded scaling for older payloads.
    const useNorm = (typeof audio.bass_norm === "number"
                  || typeof audio.mid_norm === "number"
                  || typeof audio.treble_norm === "number");
    if (bassFill) {
      const v = useNorm ? Number(audio.bass_norm || 0) : (Number(audio.bass || 0) / 0.05);
      bassFill.style.width = `${clampPct(v)}%`;
    }
    if (midFill) {
      const v = useNorm ? Number(audio.mid_norm || 0) : (Number(audio.mid || 0) / 0.04);
      midFill.style.width = `${clampPct(v)}%`;
    }
    if (trebleFill) {
      const v = useNorm ? Number(audio.treble_norm || 0) : (Number(audio.treble || 0) / 0.02);
      trebleFill.style.width = `${clampPct(v)}%`;
    }
    if (beatEl) {
      const lastCount = Number(beatEl.dataset.beatCount || 0);
      const nowCount = Number(audio.beat_count || 0);
      const pulse = render ? Number(render.global_pulse || 0) : 0;
      if (nowCount !== lastCount) {
        beatEl.classList.add("is-beat");
        beatEl.dataset.beatCount = String(nowCount);
        window.clearTimeout(beatEl._beatTimer);
        beatEl._beatTimer = window.setTimeout(() => {
          beatEl.classList.remove("is-beat");
        }, 140);
      } else if (pulse > 0.15) {
        beatEl.classList.add("is-beat");
      } else if (!render) {
        // Fast path: don't toggle off — let the timer expiry handle it.
      } else {
        beatEl.classList.remove("is-beat");
      }
    }
    // Drum pills: pulse on each new kick/snare/hat detection from the analyzer.
    pulseDrumPill("kick",  Number(audio.kick_count  || 0), 140);
    pulseDrumPill("snare", Number(audio.snare_count || 0), 110);
    pulseDrumPill("hat",   Number(audio.hat_count   || 0),  60);
    renderAutolightSpectrum(audio.spectrum);
  }

  function pulseDrumPill(kind, nowCount, durationMs) {
    const el = document.getElementById(`autolight-drum-${kind}`);
    if (!el) return;
    const cnt = document.getElementById(`autolight-drum-${kind}-count`);
    if (cnt && cnt.textContent !== String(nowCount)) cnt.textContent = String(nowCount);
    const last = Number(el.dataset.lastCount || 0);
    if (nowCount !== last) {
      el.classList.add("is-hit");
      el.dataset.lastCount = String(nowCount);
      window.clearTimeout(el._hitTimer);
      el._hitTimer = window.setTimeout(() => el.classList.remove("is-hit"), durationMs);
    }
  }

  let _autolightAudioInFlight = false;
  async function fetchAutolightAudio() {
    if (_autolightAudioInFlight) return;
    _autolightAudioInFlight = true;
    try {
      const res = await fetch("/api/autolight/audio", { cache: "no-store" });
      if (!res.ok) return;
      const audio = await res.json();
      updateAutolightAudioVisuals(audio, null);
    } catch (_err) {
      // ignore — next tick will retry
    } finally {
      _autolightAudioInFlight = false;
    }
  }

  function renderAutolightSpectrum(bands) {
    const canvas = document.getElementById("autolight-spectrum");
    if (!canvas || !Array.isArray(bands) || !bands.length) return;
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    if (canvas.width !== Math.round(rect.width * dpr) || canvas.height !== Math.round(rect.height * dpr)) {
      canvas.width = Math.round(rect.width * dpr);
      canvas.height = Math.round(rect.height * dpr);
    }
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const w = canvas.width;
    const h = canvas.height;
    const n = bands.length;
    const gap = Math.max(1, Math.floor(w / n * 0.15));
    const bw = Math.max(1, Math.floor((w - gap * (n - 1)) / n));
    for (let i = 0; i < n; i++) {
      const v = Math.max(0, Math.min(1, Number(bands[i]) || 0));
      const bh = Math.max(1, Math.floor(v * (h - 2)));
      const x = i * (bw + gap);
      const y = h - bh;
      const hue = 200 - (i / n) * 120;  // blue→orange low→high freq
      ctx.fillStyle = `hsl(${hue}, 85%, ${30 + v * 35}%)`;
      ctx.fillRect(x, y, bw, bh);
    }
  }

  async function fetchAutolightGenres() {
    try {
      const res = await fetch("/api/autolight/genres", { cache: "no-store" });
      if (!res.ok) throw new Error();
      const data = await res.json();
      autolightGenreCache = Array.isArray(data.items) ? data.items : [];
      const select = document.getElementById("autolight-genre-select");
      if (select) {
        const current = String(data.current || "auto");
        select.innerHTML = "";
        autolightGenreCache.forEach((g) => {
          const opt = document.createElement("option");
          opt.value = g;
          opt.textContent = g.charAt(0).toUpperCase() + g.slice(1);
          select.appendChild(opt);
        });
        select.value = current;
      }
    } catch (err) { /* silent */ }
  }

  async function postAutolightGenre(name) {
    try {
      const res = await fetch("/api/autolight/genre-preset", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast(data.error || "Genre preset failed", "error");
        return;
      }
      if (dmxSettingsCache && typeof dmxSettingsCache === "object" && data.autolight) {
        dmxSettingsCache.autolight = data.autolight;
      }
      toast(`Genre preset: ${name}`, "success");
    } catch (err) { /* silent */ }
  }

  async function postAutolightTapTempo(bpm) {
    try {
      const res = await fetch("/api/autolight/tap-tempo", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ bpm }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast(data.error || "Tap tempo failed", "error");
        return;
      }
      if (dmxSettingsCache && typeof dmxSettingsCache === "object" && data.autolight) {
        dmxSettingsCache.autolight = data.autolight;
      }
      toast(bpm ? `Tap tempo: ${Math.round(bpm)} BPM` : "Auto BPM restored", "success");
    } catch (err) { /* silent */ }
  }

  async function postAutolightSceneLock(scene, durationS) {
    try {
      const res = await fetch("/api/autolight/scene-lock", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scene: scene || "", duration_s: durationS || 30 }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast(data.error || "Scene lock failed", "error");
        return;
      }
      if (dmxSettingsCache && typeof dmxSettingsCache === "object" && data.autolight) {
        dmxSettingsCache.autolight = data.autolight;
      }
      toast(scene ? `Scene locked to ${scene}` : "Scene released", "success");
    } catch (err) { /* silent */ }
  }

  async function postAutolightCalibrate(duration) {
    try {
      const res = await fetch("/api/autolight/calibrate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ duration_s: duration || 30 }),
      });
      if (res.ok) toast(`Calibrating ${duration || 30}s…`, "success");
    } catch (err) { /* silent */ }
  }

  function recordTapTempo() {
    const now = performance.now();
    // Discard taps older than 3 s — restart the sequence.
    autolightTapTimestamps = autolightTapTimestamps.filter((t) => now - t < 3000);
    autolightTapTimestamps.push(now);
    if (autolightTapTimestamps.length < 2) {
      toast("Tap again in rhythm", "info");
      return;
    }
    const intervals = [];
    for (let i = 1; i < autolightTapTimestamps.length; i++) {
      intervals.push(autolightTapTimestamps[i] - autolightTapTimestamps[i - 1]);
    }
    intervals.sort((a, b) => a - b);
    const median = intervals[Math.floor(intervals.length / 2)];
    if (median < 200 || median > 1500) {
      toast(`Inter-tap ${median.toFixed(0)}ms out of range`, "error");
      return;
    }
    const bpm = 60000 / median;
    if (autolightTapTimestamps.length >= 4) {
      postAutolightTapTempo(bpm);
      autolightTapTimestamps = [];
    } else {
      toast(`Tap ${autolightTapTimestamps.length}/4 — ${bpm.toFixed(0)} BPM so far`, "info");
    }
  }

  // ---------------------------------------------------------------------
  // Audio tuning modal
  // ---------------------------------------------------------------------

  async function openAutolightTuningModal() {
    const modal = document.getElementById("autolight-tuning-modal");
    if (!modal) return;
    autolightTuningPending = {};
    try {
      const res = await fetch("/api/autolight/audio-tuning", { cache: "no-store" });
      const data = await res.json();
      autolightTuningDefaults = data.defaults || {};
      renderAutolightTuningForm(data.tuning || {});
    } catch (err) {
      renderAutolightTuningForm({});
    }
    modal.classList.add("is-open");
    modal.setAttribute("aria-hidden", "false");
  }

  function closeAutolightTuningModal() {
    const modal = document.getElementById("autolight-tuning-modal");
    if (!modal) return;
    modal.classList.remove("is-open");
    modal.setAttribute("aria-hidden", "true");
  }

  function renderAutolightTuningForm(tuning) {
    const form = document.getElementById("autolight-tuning-form");
    if (!form) return;
    form.innerHTML = "";
    TUNING_FIELD_DEFS.forEach((def) => {
      const wrap = document.createElement("div");
      wrap.className = "tuning-field";
      const value = Number(tuning[def.key] ?? autolightTuningDefaults[def.key] ?? 0);
      const label = document.createElement("label");
      const name = document.createElement("span");
      name.textContent = def.label;
      const disp = document.createElement("span");
      disp.className = "tuning-value";
      disp.textContent = value.toFixed(def.fixed);
      label.appendChild(name);
      label.appendChild(disp);
      wrap.appendChild(label);
      const input = document.createElement("input");
      input.type = "range";
      input.min = String(def.min);
      input.max = String(def.max);
      input.step = String(def.step);
      input.value = String(value);
      input.addEventListener("input", () => {
        const v = Number(input.value);
        disp.textContent = v.toFixed(def.fixed);
        autolightTuningPending[def.key] = v;
      });
      wrap.appendChild(input);
      form.appendChild(wrap);
    });
  }

  async function saveAutolightTuning() {
    try {
      const res = await fetch("/api/autolight/audio-tuning", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tuning: autolightTuningPending }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast(data.error || "Tuning save failed", "error");
        return;
      }
      if (dmxSettingsCache && typeof dmxSettingsCache === "object" && data.autolight) {
        dmxSettingsCache.autolight = data.autolight;
      }
      toast("Audio tuning saved", "success");
      closeAutolightTuningModal();
    } catch (err) {
      toast("Tuning save failed", "error");
    }
  }

  async function resetAutolightTuning() {
    try {
      const res = await fetch("/api/autolight/audio-tuning", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tuning: autolightTuningDefaults }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok) {
        autolightTuningPending = {};
        renderAutolightTuningForm(data.tuning || autolightTuningDefaults);
        toast("Tuning reset", "success");
      }
    } catch (err) { /* silent */ }
  }

  // ---------------------------------------------------------------------
  // Rig topology modal
  // ---------------------------------------------------------------------

  let autolightTopologyData = [];
  let autolightTopologySort = { key: "device_id", asc: true };

  function openAutolightTopologyModal() {
    const modal = document.getElementById("autolight-topology-modal");
    if (!modal) return;
    modal.classList.add("is-open");
    modal.setAttribute("aria-hidden", "false");
    refreshAutolightTopology();
  }

  function closeAutolightTopologyModal() {
    const modal = document.getElementById("autolight-topology-modal");
    if (!modal) return;
    modal.classList.remove("is-open");
    modal.setAttribute("aria-hidden", "true");
  }

  async function refreshAutolightTopology() {
    try {
      const res = await fetch("/api/autolight/status", { cache: "no-store" });
      if (!res.ok) return;
      const data = await res.json();
      const fixtures = ((data.render && data.render.topology && data.render.topology.fixtures) || []);
      autolightTopologyData = fixtures.map((f) => ({ ...f }));
      renderAutolightTopologyTable();
    } catch (err) { /* silent */ }
  }

  function compareForSort(a, b, key) {
    const va = a[key];
    const vb = b[key];
    if (key === "cluster") {
      const sa = Array.isArray(va) ? va.join(",") : "";
      const sb = Array.isArray(vb) ? vb.join(",") : "";
      return sa.localeCompare(sb);
    }
    if (key === "device_id") {
      const na = parseInt(va, 10);
      const nb = parseInt(vb, 10);
      if (Number.isFinite(na) && Number.isFinite(nb)) return na - nb;
      return String(va).localeCompare(String(vb));
    }
    if (typeof va === "number" && typeof vb === "number") return va - vb;
    if (va === null || va === undefined) return -1;
    if (vb === null || vb === undefined) return 1;
    return String(va).localeCompare(String(vb));
  }

  function renderAutolightTopologyTable() {
    const tbody = document.querySelector("#autolight-topology-table tbody");
    const searchEl = document.getElementById("autolight-topology-search");
    const query = (searchEl && searchEl.value || "").trim().toLowerCase();
    if (!tbody) return;
    const sorted = [...autolightTopologyData].sort((a, b) => {
      const c = compareForSort(a, b, autolightTopologySort.key);
      return autolightTopologySort.asc ? c : -c;
    });
    const filtered = query
      ? sorted.filter((f) =>
          String(f.device_id).toLowerCase().includes(query) ||
          String(f.cname || "").toLowerCase().includes(query) ||
          String(f.fixture || "").toLowerCase().includes(query))
      : sorted;

    tbody.innerHTML = "";
    filtered.forEach((f) => {
      const tr = document.createElement("tr");
      const side = String(f.side || "");
      if (side === "left") tr.classList.add("is-left");
      else if (side === "right") tr.classList.add("is-right");
      else tr.classList.add("is-orphan");

      const tdId = document.createElement("td");
      const idBtn = document.createElement("button");
      idBtn.type = "button";
      idBtn.className = "identify-btn";
      idBtn.textContent = String(f.device_id);
      idBtn.title = `Click to flash ${f.device_id} for 2 s`;
      idBtn.addEventListener("click", () => postAutolightIdentify(f.device_id));
      tdId.appendChild(idBtn);
      tr.appendChild(tdId);

      const cells = [
        ["cname",   f.cname || ""],
        ["fixture", f.fixture || ""],
        ["universe", String(f.universe ?? "")],
        ["address", String(f.address ?? "")],
        ["x",       f.x !== null && f.x !== undefined ? Number(f.x).toFixed(0) : "—"],
        ["y",       f.y !== null && f.y !== undefined ? Number(f.y).toFixed(0) : "—"],
      ];
      cells.forEach(([cls, text]) => {
        const td = document.createElement("td");
        td.textContent = text;
        if (cls === "x" || cls === "y") td.className = "num";
        tr.appendChild(td);
      });

      const tdSide = document.createElement("td");
      tdSide.textContent = side || "—";
      if (side === "left") tdSide.className = "side-left";
      else if (side === "right") tdSide.className = "side-right";
      tr.appendChild(tdSide);

      const tdPair = document.createElement("td");
      tdPair.textContent = f.pair_id ? `#${f.pair_id}` : "—";
      tr.appendChild(tdPair);

      const tdCluster = document.createElement("td");
      tdCluster.textContent = Array.isArray(f.cluster) ? `${f.cluster[0]},${f.cluster[1]}` : "—";
      tr.appendChild(tdCluster);

      const tdCaps = document.createElement("td");
      const caps = [];
      if (f.has_dimmer) caps.push("dim");
      if (f.has_color) caps.push("rgb");
      if (f.has_movement) caps.push("mov");
      if (f.strobe_friendly) caps.push("strobe");
      tdCaps.textContent = caps.join(" ") || "—";
      tr.appendChild(tdCaps);

      tbody.appendChild(tr);
    });

    // Sort indicators
    document.querySelectorAll("#autolight-topology-table th[data-sort]").forEach((th) => {
      th.classList.remove("is-sorted", "is-asc");
      if (th.dataset.sort === autolightTopologySort.key) {
        th.classList.add("is-sorted");
        if (autolightTopologySort.asc) th.classList.add("is-asc");
      }
    });
  }

  async function postAutolightIdentify(deviceId) {
    try {
      const res = await fetch("/api/autolight/identify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device_id: String(deviceId), duration_s: 2 }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast(data.error || `Identify ${deviceId} failed`, "error");
        return;
      }
      toast(`Flashing ${deviceId} for 2 s`, "success");
    } catch (err) { /* silent */ }
  }

  // ---------------------------------------------------------------------
  // Effects customization modal
  // ---------------------------------------------------------------------

  let autolightEffectsCache = [];
  let autolightEffectsPending = {};   // per-effect pending overrides, flushed on Save

  function roundTo(v, step) {
    return Math.round(v / step) * step;
  }

  async function fetchAutolightEffects() {
    try {
      const res = await fetch("/api/autolight/effects", { cache: "no-store" });
      if (!res.ok) throw new Error("effects fetch failed");
      const data = await res.json();
      autolightEffectsCache = Array.isArray(data.items) ? data.items : [];
      autolightMoodCache = Array.isArray(data.moods) ? data.moods : [];
    } catch (err) {
      autolightEffectsCache = [];
      autolightMoodCache = [];
    }
  }

  function renderAutolightMoodBar() {
    const bar = document.getElementById("autolight-mood-bar");
    if (!bar) return;
    bar.innerHTML = "";
    const currentMoods = new Set(((dmxSettingsCache && dmxSettingsCache.autolight && dmxSettingsCache.autolight.mood_filter) || []).map(String));
    const label = document.createElement("span");
    label.style.marginRight = "6px";
    label.style.alignSelf = "center";
    label.style.color = "var(--muted, #8b97a6)";
    label.textContent = "Mood filter:";
    bar.appendChild(label);
    autolightMoodCache.forEach((mood) => {
      const chip = document.createElement("span");
      chip.className = "mood-chip" + (currentMoods.has(mood) ? " is-on" : "");
      chip.textContent = mood;
      chip.addEventListener("click", () => {
        if (currentMoods.has(mood)) currentMoods.delete(mood);
        else currentMoods.add(mood);
        postAutolightMoodFilter(Array.from(currentMoods));
      });
      bar.appendChild(chip);
    });
    if (currentMoods.size > 0) {
      const clear = document.createElement("span");
      clear.className = "mood-chip";
      clear.textContent = "× clear";
      clear.addEventListener("click", () => postAutolightMoodFilter([]));
      bar.appendChild(clear);
    }
  }

  async function postAutolightMoodFilter(moods) {
    try {
      const res = await fetch("/api/autolight/mood-filter", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ moods }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast(data.error || "Mood filter failed", "error");
        return;
      }
      if (dmxSettingsCache && typeof dmxSettingsCache === "object" && data.autolight) {
        dmxSettingsCache.autolight = data.autolight;
      }
      renderAutolightMoodBar();
    } catch (err) { /* silent */ }
  }

  function openAutolightEffectsModal() {
    const modal = document.getElementById("autolight-effects-modal");
    if (!modal) return;
    autolightEffectsPending = {};
    modal.classList.add("is-open");
    modal.setAttribute("aria-hidden", "false");
    fetchAutolightEffects().then(() => {
      renderAutolightMoodBar();
      renderAutolightEffectsTable();
    });
  }

  function closeAutolightEffectsModal() {
    const modal = document.getElementById("autolight-effects-modal");
    if (!modal) return;
    modal.classList.remove("is-open");
    modal.setAttribute("aria-hidden", "true");
  }

  // -------------------------------------------------------------------
  // AutoLight Training modal
  // -------------------------------------------------------------------
  //
  // Two pieces of state matter:
  //   * trainingPollTimer — keeps the modal in sync with the live director
  //     (current track, listen counts, satisfaction sample count). Running
  //     only while the modal is open.
  //   * sliderPendingValue / sliderLastSent — the slider streams at user
  //     drag speed (60-120 events/s) but we only POST at 10 Hz, always
  //     sending the LATEST value so we never lag behind the user's hand.

  let trainingPollTimer = null;
  let trainingLibraryCache = [];
  let trainingStatusCache = {};
  let sliderPendingValue = null;
  let sliderLastSent = 0;
  let sliderFlushTimer = null;
  const SATISFACTION_THROTTLE_MS = 100;  // 10 Hz cap on outgoing POSTs

  function openAutolightTrainingModal() {
    const modal = document.getElementById("autolight-training-modal");
    if (!modal) return;
    modal.classList.add("is-open");
    modal.setAttribute("aria-hidden", "false");
    ensureMovesMeta();
    fetchTrainingLibrary();
    fetchTrainingStatus();
    // Populate the camera selector immediately so the user sees what's
    // available without having to click "Start" first. Labels stay
    // hidden until permission is granted; we re-enumerate after the
    // first successful start.
    refreshCameraList();
    if (trainingPollTimer === null) {
      trainingPollTimer = setInterval(fetchTrainingStatus, 1000);
    }
  }

  function closeAutolightTrainingModal() {
    const modal = document.getElementById("autolight-training-modal");
    if (!modal) return;
    modal.classList.remove("is-open");
    modal.setAttribute("aria-hidden", "true");
    if (trainingPollTimer !== null) {
      clearInterval(trainingPollTimer);
      trainingPollTimer = null;
    }
    // Flush any pending slider value so the server gets the user's final
    // resting value before we stop streaming.
    flushSlider();
    // Stop the webcam too — leaving it running after the modal closes
    // would needlessly burn battery and block the camera for other apps.
    stopCamera();
  }

  async function fetchTrainingLibrary() {
    try {
      const res = await fetch("/api/autolight/training/library", { cache: "no-store" });
      if (!res.ok) return;
      const data = await res.json();
      trainingLibraryCache = Array.isArray(data.items) ? data.items : [];
      renderTrainingLibrary();
    } catch (err) {
      console.warn("training library fetch failed", err);
    }
  }

  async function fetchTrainingStatus() {
    try {
      const res = await fetch("/api/autolight/training/status", { cache: "no-store" });
      if (!res.ok) return;
      trainingStatusCache = await res.json();
      renderTrainingStatus();
    } catch (err) {
      console.warn("training status fetch failed", err);
    }
  }

  function formatDuration(ms) {
    if (!ms || ms <= 0) return "—";
    const s = Math.round(ms / 1000);
    const m = Math.floor(s / 60);
    const r = s % 60;
    return `${m}:${String(r).padStart(2, "0")}`;
  }

  function renderTrainingLibrary() {
    const tbody = document.querySelector("#autolight-training-library-table tbody");
    const statusEl = document.getElementById("autolight-training-library-status");
    if (!tbody) return;
    tbody.innerHTML = "";
    const currentTrackId = trainingStatusCache.current_track_id || null;

    if (!trainingLibraryCache.length) {
      if (statusEl) statusEl.textContent = "No tracks in library yet.";
      return;
    }
    if (statusEl) {
      statusEl.textContent = `${trainingLibraryCache.length} track${trainingLibraryCache.length > 1 ? "s" : ""} in library.`;
    }

    // We pull listen counts / satisfaction sample counts from the live
    // memory file via /api/autolight/status — skip that here, it'd be
    // another fetch per render. The status poll will re-render this when
    // the current track changes. For now show "—" for static rows.
    trainingLibraryCache.forEach((entry) => {
      const tr = document.createElement("tr");
      if (entry.track_id === currentTrackId) {
        tr.classList.add("is-current");
      }
      tr.innerHTML = `
        <td>${escapeHtml(entry.title || "—")}</td>
        <td>${escapeHtml(entry.artist || "—")}</td>
        <td>${formatDuration(entry.duration_ms)}</td>
        <td>${entry.track_id === currentTrackId ? (trainingStatusCache.current_track_listen_count || 1) : "—"}</td>
        <td>${entry.track_id === currentTrackId ? (trainingStatusCache.current_track_satisfaction_samples || 0) : "—"}</td>
        <td>
          <button type="button" class="secondary" data-train-play="${escapeAttr(entry.track_id)}">Play</button>
          <button type="button" class="secondary" data-train-remove="${escapeAttr(entry.track_id)}">Remove</button>
        </td>
      `;
      tbody.appendChild(tr);
    });

    tbody.querySelectorAll("[data-train-play]").forEach((btn) => {
      btn.addEventListener("click", () => postTrainingPlay(btn.dataset.trainPlay));
    });
    tbody.querySelectorAll("[data-train-remove]").forEach((btn) => {
      btn.addEventListener("click", () => postTrainingRemove(btn.dataset.trainRemove));
    });
  }

  function renderTrainingStatus() {
    const stateEl = document.getElementById("autolight-training-state");
    const nowEl = document.getElementById("autolight-training-now-playing");
    const enabledToggle = document.getElementById("autolight-training-enabled");
    const enabled = Boolean(trainingStatusCache.enabled);

    if (enabledToggle && enabledToggle.dataset.userInteracting !== "1") {
      enabledToggle.checked = enabled;
    }
    if (stateEl) {
      if (!enabled) {
        stateEl.textContent = "Disabled";
        stateEl.classList.remove("is-on");
      } else if (!trainingStatusCache.memory_required_ok) {
        stateEl.textContent = "Enabling memory…";
        stateEl.classList.remove("is-on");
      } else {
        stateEl.textContent = "Listening";
        stateEl.classList.add("is-on");
      }
    }
    if (nowEl) {
      const tid = trainingStatusCache.current_track_id;
      if (!tid) {
        nowEl.textContent = "No track detected.";
        nowEl.classList.remove("is-active");
      } else {
        const inLib = trainingStatusCache.current_track_in_library;
        const listens = trainingStatusCache.current_track_listen_count || 0;
        const samples = trainingStatusCache.current_track_satisfaction_samples || 0;
        nowEl.textContent = `Now: ${tid} · ${listens} listen${listens === 1 ? "" : "s"} · ${samples} satisfaction sample${samples === 1 ? "" : "s"}${inLib ? " · in library" : ""}`;
        nowEl.classList.toggle("is-active", inLib && enabled);
      }
    }

    // Active move + scores tableau
    const compositions = trainingStatusCache.compositions || {};
    renderTrainingActiveMove(compositions);
    renderTrainingMoveScores(compositions);

    // Re-render the library table to reflect current-track highlighting +
    // updated counts. Cheap because the table is small.
    renderTrainingLibrary();
  }

  function renderTrainingActiveMove(compositions) {
    const moveEl = document.getElementById("autolight-training-active-move");
    if (!moveEl) return;
    const active = compositions.active;
    if (!active) {
      moveEl.textContent = "No move active — agents driving the rig.";
      moveEl.classList.remove("is-active");
      return;
    }
    const pct = Math.round((Number(active.progress) || 0) * 100);
    const score = Number(active.score) || 0;
    const scoreTxt = score > 0.05
      ? `(score +${score.toFixed(2)} — well-rated)`
      : score < -0.05
        ? `(score ${score.toFixed(2)} — re-evaluating)`
        : "(no score yet)";
    moveEl.textContent = `Move: ${active.name} · ${pct}% · ${active.samples_so_far} samples ${scoreTxt}`;
    moveEl.title = active.description || "";
    moveEl.classList.add("is-active");
  }

  let movesMetaCache = null;
  async function ensureMovesMeta() {
    if (movesMetaCache !== null) return movesMetaCache;
    try {
      const res = await fetch("/api/autolight/training/moves", { cache: "no-store" });
      if (!res.ok) {
        movesMetaCache = [];
        return movesMetaCache;
      }
      const data = await res.json();
      movesMetaCache = Array.isArray(data.items) ? data.items : [];
    } catch (err) {
      console.warn("moves meta fetch failed", err);
      movesMetaCache = [];
    }
    return movesMetaCache;
  }

  function renderTrainingMoveScores(compositions) {
    const tbody = document.querySelector("#autolight-training-move-scores tbody");
    if (!tbody) return;
    const scores = compositions.scores || {};
    const samples = compositions.score_samples || {};
    const meta = movesMetaCache || [];
    const metaByName = {};
    meta.forEach((m) => { metaByName[m.name] = m; });

    const names = Object.keys(scores);
    if (!names.length) {
      tbody.innerHTML = "";
      return;
    }
    names.sort((a, b) => {
      const sa = scores[a] || 0;
      const sb = scores[b] || 0;
      if (sa !== sb) return sb - sa;
      return (samples[b] || 0) - (samples[a] || 0);
    });
    tbody.innerHTML = "";
    names.forEach((name) => {
      const score = Number(scores[name]) || 0;
      const sampleCount = Number(samples[name]) || 0;
      const cls = sampleCount === 0 ? "score-zero" : (score > 0.05 ? "score-pos" : (score < -0.05 ? "score-neg" : "score-zero"));
      const m = metaByName[name] || {};
      const intents = Array.isArray(m.eligible_intents) ? m.eligible_intents.join(" · ") : "—";
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td title="${escapeAttr(m.description || '')}">${escapeHtml(name)}</td>
        <td class="${cls}">${sampleCount === 0 ? "—" : (score >= 0 ? "+" : "") + score.toFixed(2)}</td>
        <td>${sampleCount}</td>
        <td class="muted" style="font-size: 11px;">${escapeHtml(intents)}</td>
      `;
      tbody.appendChild(tr);
    });
  }

  async function postTrainingControl(enabled) {
    try {
      const res = await fetch("/api/autolight/training/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      if (!res.ok) return;
      const data = await res.json();
      trainingStatusCache = data.training || trainingStatusCache;
      renderTrainingStatus();
    } catch (err) {
      console.warn("training control failed", err);
    }
  }

  async function postTrainingLibraryAdd(path, recursive) {
    if (!path) return;
    try {
      const res = await fetch("/api/autolight/training/library", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths: [path], recursive }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        alert(`Library add failed: ${err.error || res.statusText}`);
        return;
      }
      const data = await res.json();
      trainingLibraryCache = data.items || [];
      if (Array.isArray(data.errors) && data.errors.length) {
        const msgs = data.errors.map((e) => `${e.path}: ${e.error}`).join("\n");
        alert(`Some paths failed:\n${msgs}`);
      }
      renderTrainingLibrary();
    } catch (err) {
      console.warn("training library add failed", err);
    }
  }

  async function postTrainingLibraryClear() {
    if (!confirm("Clear the entire training library?")) return;
    try {
      const res = await fetch("/api/autolight/training/library", { method: "DELETE" });
      if (!res.ok) return;
      trainingLibraryCache = [];
      renderTrainingLibrary();
    } catch (err) {
      console.warn("training library clear failed", err);
    }
  }

  async function postTrainingRemove(trackId) {
    if (!trackId) return;
    try {
      const res = await fetch(`/api/autolight/training/library/${encodeURIComponent(trackId)}`, { method: "DELETE" });
      if (!res.ok) return;
      const data = await res.json();
      trainingLibraryCache = data.items || [];
      renderTrainingLibrary();
    } catch (err) {
      console.warn("training library remove failed", err);
    }
  }

  async function postTrainingPlay(trackId) {
    if (!trackId) return;
    try {
      const res = await fetch("/api/autolight/training/play", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ track_id: trackId }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        alert(`Play failed: ${err.error || res.statusText}`);
      }
    } catch (err) {
      console.warn("training play failed", err);
    }
  }

  // ---- Real-time satisfaction slider streaming ---------------------
  // The slider's `input` event fires every time the user drags by a pixel
  // (potentially 60-120 events/s). We always store the latest value into
  // sliderPendingValue. A 10 Hz throttle controls how often we actually
  // POST it to the server — combined with a tail-flush timer to make
  // sure the user's resting value always reaches the server even if the
  // last drag event was during the throttle window.

  function flushSlider() {
    if (sliderFlushTimer !== null) {
      clearTimeout(sliderFlushTimer);
      sliderFlushTimer = null;
    }
    if (sliderPendingValue === null) return;
    const value = sliderPendingValue;
    sliderPendingValue = null;
    sliderLastSent = performance.now();
    fetch("/api/autolight/training/satisfaction", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
      keepalive: true,
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((result) => {
        if (!result) return;
        const readout = document.getElementById("autolight-training-slider-readout");
        if (readout) {
          const samples = Number(result.samples_this_session) || 0;
          const v = Number(result.value);
          let suffix = "";
          if (!result.ok && result.reason === "training_disabled") {
            suffix = " · (training off — value not stored)";
          } else if (!result.ok && result.reason === "no_track_or_memory_off") {
            suffix = " · (no current track to learn from)";
          }
          readout.textContent = `Slider: ${v.toFixed(2)} · ${samples} samples this track${suffix}`;
        }
      })
      .catch((err) => console.warn("satisfaction post failed", err));
  }

  function handleSliderInput(evt) {
    const v = parseFloat(evt.target.value);
    if (!Number.isFinite(v)) return;
    sliderPendingValue = v;
    const now = performance.now();
    const wait = SATISFACTION_THROTTLE_MS - (now - sliderLastSent);
    if (wait <= 0) {
      flushSlider();
    } else if (sliderFlushTimer === null) {
      sliderFlushTimer = setTimeout(flushSlider, wait);
    }
  }

  // -------------------------------------------------------------------
  // Camera calibration (webcam + per-fixture identification)
  // -------------------------------------------------------------------
  //
  // The browser owns the webcam stream and the frame analysis — no
  // image data hits the server. Calibration loop:
  //   1. Server flashes one fixture (we POST to /identify)
  //   2. After a short settle delay we sample N frames, average their
  //      luminance, find the brightest cluster.
  //   3. We POST the (x, y) — normalised to the video frame — to the
  //      server, which persists it.
  //   4. Repeat for each fixture.
  //
  // The overlay canvas draws a small dot per known fixture position
  // continuously while the camera is on, so the user can see what's
  // what during training.

  let cameraStream = null;
  let cameraOverlayTimer = null;
  let cameraDevices = [];
  let cameraPositions = {};
  let calibrationRunning = false;
  let availableCameras = [];   // [{deviceId, label}]
  let labelsAvailable = false; // true once we've succeeded once (browsers
                               // hide labels until a getUserMedia succeeds)

  function setCameraStatus(msg, kind) {
    const el = document.getElementById("autolight-camera-status");
    if (!el) return;
    el.textContent = msg;
    el.classList.remove("is-on", "is-error");
    if (kind === "on") el.classList.add("is-on");
    else if (kind === "error") el.classList.add("is-error");
  }

  function cameraApiCheck() {
    // getUserMedia requires a secure context (HTTPS or localhost). On
    // plain HTTP over the LAN the API is undefined and the user has no
    // way to know why. Surface this explicitly.
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setCameraStatus(
        "Camera API unavailable. Open the app via http://localhost or HTTPS.",
        "error"
      );
      return false;
    }
    return true;
  }

  async function refreshCameraList() {
    if (!cameraApiCheck()) return;
    let devices;
    try {
      devices = await navigator.mediaDevices.enumerateDevices();
    } catch (err) {
      setCameraStatus(`Cannot list cameras: ${err.name}`, "error");
      return;
    }
    availableCameras = devices
      .filter((d) => d.kind === "videoinput")
      .map((d, i) => ({
        deviceId: d.deviceId,
        label: d.label || (labelsAvailable ? `Camera ${i + 1}` : `Camera ${i + 1} (label hidden — start once to reveal)`),
      }));

    const select = document.getElementById("autolight-camera-select");
    if (!select) return;
    const previous = select.value;
    select.innerHTML = "";
    if (!availableCameras.length) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "— no camera detected —";
      select.appendChild(opt);
      select.disabled = true;
      return;
    }
    select.disabled = false;
    availableCameras.forEach((c) => {
      const opt = document.createElement("option");
      opt.value = c.deviceId;
      opt.textContent = c.label;
      select.appendChild(opt);
    });
    // Preserve previous choice if still available; otherwise default to
    // the first camera.
    if (previous && availableCameras.some((c) => c.deviceId === previous)) {
      select.value = previous;
    } else {
      select.value = availableCameras[0].deviceId;
    }
  }

  function getSelectedCameraId() {
    const select = document.getElementById("autolight-camera-select");
    return select ? select.value : "";
  }

  async function startCamera() {
    if (cameraStream) return;
    if (!cameraApiCheck()) return;

    const deviceId = getSelectedCameraId();
    // Build constraints. We DON'T pass facingMode anymore — on a laptop
    // there's no "environment" camera and Chrome rejects the request
    // outright. Use deviceId when one is selected, else let the browser
    // pick the default.
    const videoConstraints = {
      width: { ideal: 1280 },
      height: { ideal: 720 },
    };
    if (deviceId) {
      videoConstraints.deviceId = { exact: deviceId };
    }

    setCameraStatus("Starting camera…");
    try {
      cameraStream = await navigator.mediaDevices.getUserMedia({
        video: videoConstraints,
        audio: false,
      });
    } catch (err) {
      let msg;
      switch (err.name) {
        case "NotAllowedError":
        case "SecurityError":
          msg = "Camera permission denied — check the browser's site settings.";
          break;
        case "NotFoundError":
        case "OverconstrainedError":
          msg = "Selected camera is unavailable. Try another in the dropdown.";
          break;
        case "NotReadableError":
          msg = "Camera is busy (used by another app). Close other camera users and retry.";
          break;
        case "AbortError":
          msg = "Camera start was aborted before the stream opened.";
          break;
        default:
          msg = `Camera error: ${err.name} (${err.message || "no detail"})`;
      }
      setCameraStatus(msg, "error");
      return;
    }

    const video = document.getElementById("autolight-camera-video");
    if (!video) return;
    video.srcObject = cameraStream;
    // Some browsers refuse autoplay even with `muted` set in HTML. Calling
    // play() explicitly + ignoring the rejection works around the policy
    // without breaking on browsers that auto-play correctly.
    try { await video.play(); } catch (_e) { /* ignore */ }

    labelsAvailable = true;
    // Re-enumerate now that we have permission — labels become populated.
    refreshCameraList();

    setCameraStatus("Camera on. Calibrate to map fixtures.", "on");
    document.getElementById("autolight-camera-stop").disabled = false;
    document.getElementById("autolight-camera-calibrate").disabled = false;
    document.getElementById("autolight-camera-start").disabled = true;
    fetchCameraDevices();
    if (cameraOverlayTimer === null) {
      cameraOverlayTimer = setInterval(drawCameraOverlay, 200);
    }
  }

  function stopCamera() {
    if (cameraStream) {
      cameraStream.getTracks().forEach((t) => t.stop());
      cameraStream = null;
    }
    const video = document.getElementById("autolight-camera-video");
    if (video) video.srcObject = null;
    if (cameraOverlayTimer !== null) {
      clearInterval(cameraOverlayTimer);
      cameraOverlayTimer = null;
    }
    document.getElementById("autolight-camera-stop").disabled = true;
    document.getElementById("autolight-camera-calibrate").disabled = true;
    document.getElementById("autolight-camera-start").disabled = false;
    setCameraStatus("Camera off.");
    drawCameraOverlay();
  }

  async function switchCameraIfRunning() {
    // Hot-swap: when the user picks a different camera while a stream is
    // already running, stop the current one and start the new one.
    if (!cameraStream) return;
    stopCamera();
    await startCamera();
  }

  async function fetchCameraDevices() {
    try {
      const res = await fetch("/api/autolight/training/devices", { cache: "no-store" });
      if (!res.ok) return;
      const data = await res.json();
      cameraDevices = Array.isArray(data.items) ? data.items : [];
      cameraPositions = {};
      cameraDevices.forEach((d) => {
        if (typeof d.x === "number" && typeof d.y === "number") {
          cameraPositions[d.device_id] = { x: d.x, y: d.y };
        }
      });
      drawCameraOverlay();
    } catch (err) {
      console.warn("camera devices fetch failed", err);
    }
  }

  function drawCameraOverlay() {
    const canvas = document.getElementById("autolight-camera-overlay");
    const video = document.getElementById("autolight-camera-video");
    if (!canvas || !video) return;
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    if (canvas.width !== w) canvas.width = w;
    if (canvas.height !== h) canvas.height = h;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, w, h);
    if (!cameraStream) return;
    Object.entries(cameraPositions).forEach(([devId, p]) => {
      const px = p.x * w;
      const py = p.y * h;
      // Dot
      ctx.beginPath();
      ctx.arc(px, py, 8, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(16, 185, 129, 0.85)";
      ctx.fill();
      ctx.lineWidth = 2;
      ctx.strokeStyle = "#022c22";
      ctx.stroke();
      // Label
      ctx.font = "11px sans-serif";
      ctx.fillStyle = "#e2e8f0";
      ctx.fillText(devId, px + 12, py + 4);
    });
  }

  // Capture N frames from the live video, average their luminance, find
  // the brightest cluster. Returns ``{x, y}`` in [0, 1] frame coords or
  // null when nothing bright is seen above a threshold.
  function captureBrightestSpot(samples = 4) {
    const video = document.getElementById("autolight-camera-video");
    const grab = document.getElementById("autolight-camera-grabber");
    if (!video || !grab || !cameraStream) return null;
    const vw = video.videoWidth;
    const vh = video.videoHeight;
    if (!vw || !vh) return null;

    // Downsample for speed: do detection at 160×90 max, accumulate
    // luminance into the smaller buffer.
    const dw = 160;
    const dh = Math.round((vh / vw) * dw);
    grab.width = dw;
    grab.height = dh;
    const ctx = grab.getContext("2d", { willReadFrequently: true });

    const accumulator = new Float32Array(dw * dh);
    for (let s = 0; s < samples; s++) {
      ctx.drawImage(video, 0, 0, dw, dh);
      const img = ctx.getImageData(0, 0, dw, dh);
      const data = img.data;
      for (let i = 0, j = 0; j < data.length; i++, j += 4) {
        // Rec. 601 luminance
        const lum = 0.299 * data[j] + 0.587 * data[j + 1] + 0.114 * data[j + 2];
        accumulator[i] += lum;
      }
    }

    // Find the brightest 5x5 patch sum — more robust than a single
    // pixel argmax because it locks onto a fixture beam not a sensor
    // hot pixel.
    const patch = 5;
    let bestSum = -1;
    let bestX = 0;
    let bestY = 0;
    for (let y = 0; y <= dh - patch; y++) {
      for (let x = 0; x <= dw - patch; x++) {
        let sum = 0;
        for (let py = 0; py < patch; py++) {
          for (let px = 0; px < patch; px++) {
            sum += accumulator[(y + py) * dw + (x + px)];
          }
        }
        if (sum > bestSum) {
          bestSum = sum;
          bestX = x;
          bestY = y;
        }
      }
    }

    // Centre of the patch, normalised.
    const cx = (bestX + patch / 2) / dw;
    const cy = (bestY + patch / 2) / dh;
    // Reject if average per-pixel luminance is too low (no fixture really lit)
    const avgLum = bestSum / (samples * patch * patch);
    if (avgLum < 80) return null;
    return { x: cx, y: cy, brightness: avgLum };
  }

  async function runCalibration() {
    if (calibrationRunning) return;
    if (!cameraStream) {
      setCameraStatus("Start the camera first.", "error");
      return;
    }
    calibrationRunning = true;
    document.getElementById("autolight-camera-calibrate").disabled = true;

    // Refresh device list right before we start so we calibrate the
    // currently-registered set.
    await fetchCameraDevices();
    const targets = cameraDevices.slice();
    if (!targets.length) {
      setCameraStatus("No fixtures registered.", "error");
      calibrationRunning = false;
      document.getElementById("autolight-camera-calibrate").disabled = false;
      return;
    }

    const progress = document.getElementById("autolight-camera-calibration-progress");
    let captured = 0;
    let skipped = 0;
    for (let i = 0; i < targets.length; i++) {
      const d = targets[i];
      if (progress) {
        progress.textContent = `Calibrating ${i + 1}/${targets.length}: ${d.cname || d.device_id}…`;
      }
      // Trigger the flash
      const idRes = await fetch("/api/autolight/training/identify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device_id: d.device_id, duration_s: 1.5 }),
      });
      if (!idRes.ok) {
        skipped++;
        continue;
      }
      // Settle for the flash to be visible (~600 ms after trigger)
      await sleep(600);
      // Capture
      const spot = captureBrightestSpot(6);
      if (!spot) {
        skipped++;
        // Wait the rest of the flash duration before next iteration so
        // we don't overlap two flashes.
        await sleep(900);
        continue;
      }
      // Persist
      await fetch("/api/autolight/training/camera-position", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device_id: d.device_id, x: spot.x, y: spot.y }),
      });
      cameraPositions[d.device_id] = { x: spot.x, y: spot.y };
      captured++;
      drawCameraOverlay();
      // Wait the remainder of the flash so the fixture goes dark before
      // the next one starts; otherwise fixtures bleed into each other.
      await sleep(900);
    }

    if (progress) {
      progress.textContent = `Done — captured ${captured}/${targets.length}${skipped ? ` (${skipped} skipped: too dim or off-frame)` : ""}.`;
    }
    setCameraStatus(`Calibrated ${captured} fixture${captured === 1 ? "" : "s"}.`, "on");
    calibrationRunning = false;
    document.getElementById("autolight-camera-calibrate").disabled = false;
  }

  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  async function clearCameraPositions() {
    if (!confirm("Clear all calibrated fixture positions?")) return;
    try {
      await fetch("/api/autolight/training/camera-position", { method: "DELETE" });
      cameraPositions = {};
      cameraDevices.forEach((d) => { d.x = null; d.y = null; });
      drawCameraOverlay();
    } catch (err) {
      console.warn("clear positions failed", err);
    }
  }

  function bindAutolightTrainingCameraControls() {
    const startBtn = document.getElementById("autolight-camera-start");
    if (startBtn) startBtn.addEventListener("click", startCamera);
    const stopBtn = document.getElementById("autolight-camera-stop");
    if (stopBtn) stopBtn.addEventListener("click", stopCamera);
    const calBtn = document.getElementById("autolight-camera-calibrate");
    if (calBtn) calBtn.addEventListener("click", runCalibration);
    const clearBtn = document.getElementById("autolight-camera-clear-positions");
    if (clearBtn) clearBtn.addEventListener("click", clearCameraPositions);
    const refreshBtn = document.getElementById("autolight-camera-refresh");
    if (refreshBtn) refreshBtn.addEventListener("click", refreshCameraList);
    const select = document.getElementById("autolight-camera-select");
    if (select) {
      select.addEventListener("change", switchCameraIfRunning);
    }
    // Re-enumerate when devices appear/disappear (USB cam plugged in
    // mid-session).
    if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
      navigator.mediaDevices.addEventListener("devicechange", refreshCameraList);
    }
  }

  function bindAutolightTrainingControls() {
    const openBtn = document.getElementById("autolight-training-open");
    if (openBtn) openBtn.addEventListener("click", openAutolightTrainingModal);

    const closeBtn = document.getElementById("autolight-training-close");
    if (closeBtn) closeBtn.addEventListener("click", closeAutolightTrainingModal);

    const modal = document.getElementById("autolight-training-modal");
    if (modal) {
      modal.addEventListener("click", (evt) => {
        if (evt.target instanceof HTMLElement && evt.target.dataset.modalClose === "1") {
          closeAutolightTrainingModal();
        }
      });
    }

    const scanBtn = document.getElementById("autolight-training-scan");
    const pathInput = document.getElementById("autolight-training-path-input");
    const recursiveToggle = document.getElementById("autolight-training-recursive-toggle");
    if (scanBtn && pathInput) {
      scanBtn.addEventListener("click", () => {
        const path = pathInput.value.trim();
        const recursive = recursiveToggle ? recursiveToggle.checked : true;
        postTrainingLibraryAdd(path, recursive);
      });
      pathInput.addEventListener("keydown", (evt) => {
        if (evt.key === "Enter") {
          evt.preventDefault();
          scanBtn.click();
        }
      });
    }

    const clearBtn = document.getElementById("autolight-training-clear");
    if (clearBtn) clearBtn.addEventListener("click", postTrainingLibraryClear);

    const enabledToggle = document.getElementById("autolight-training-enabled");
    if (enabledToggle) {
      enabledToggle.addEventListener("focus", () => { enabledToggle.dataset.userInteracting = "1"; });
      enabledToggle.addEventListener("blur", () => { enabledToggle.dataset.userInteracting = "0"; });
      enabledToggle.addEventListener("change", () => {
        postTrainingControl(Boolean(enabledToggle.checked));
      });
    }

    const slider = document.getElementById("autolight-training-slider");
    if (slider) {
      // input fires on every drag pixel; change fires on release. We wire
      // both — change is the safety net for keyboard/accessibility users.
      slider.addEventListener("input", handleSliderInput);
      slider.addEventListener("change", handleSliderInput);
    }

    bindAutolightTrainingCameraControls();
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
  function escapeAttr(s) { return escapeHtml(s); }

  function renderAutolightEffectsTable() {
    const tbody = document.querySelector("#autolight-effects-table tbody");
    if (!tbody) return;
    tbody.innerHTML = "";
    autolightEffectsCache.forEach((item) => {
      const tr = document.createElement("tr");
      tr.dataset.effectName = item.name;
      const eff = item.effective || {};
      const override = item.config || {};
      const enabled = "enabled" in override ? Boolean(override.enabled) : true;
      const weight = Number("weight" in override ? override.weight : (eff.weight ?? 1.0));
      const duration = Number("duration_beats" in override ? override.duration_beats : (eff.duration_beats ?? item.default_duration_beats));
      const cooldown = Number("cooldown_bars" in override ? override.cooldown_bars : (eff.cooldown_bars ?? item.cooldown_bars));

      tr.classList.toggle("is-disabled", !enabled);

      const tdOn = document.createElement("td");
      const chk = document.createElement("input");
      chk.type = "checkbox";
      chk.checked = enabled;
      chk.addEventListener("change", () => {
        autolightEffectsPending[item.name] = autolightEffectsPending[item.name] || {};
        autolightEffectsPending[item.name].enabled = chk.checked;
        tr.classList.toggle("is-disabled", !chk.checked);
      });
      tdOn.appendChild(chk);
      tr.appendChild(tdOn);

      const tdName = document.createElement("td");
      tdName.className = "effect-name";
      tdName.textContent = item.name;
      tr.appendChild(tdName);

      const tdScenes = document.createElement("td");
      tdScenes.className = "effect-scenes";
      tdScenes.textContent = Array.isArray(item.eligible_scenes) ? item.eligible_scenes.join("·") : "—";
      tr.appendChild(tdScenes);

      const tdBpm = document.createElement("td");
      tdBpm.className = "effect-bpm";
      const minB = Number(item.min_bpm || 0);
      const maxB = Number(item.max_bpm || 999);
      const minTxt = minB > 0 ? String(Math.round(minB)) : "—";
      const maxTxt = maxB < 900 ? String(Math.round(maxB)) : "—";
      tdBpm.textContent = `${minTxt}–${maxTxt}`;
      tr.appendChild(tdBpm);

      const tdMood = document.createElement("td");
      tdMood.className = "effect-moods";
      tdMood.textContent = Array.isArray(item.mood_tags) && item.mood_tags.length ? item.mood_tags.join(", ") : "—";
      tr.appendChild(tdMood);

      const tdWeight = document.createElement("td");
      const wRange = document.createElement("input");
      wRange.type = "range";
      wRange.min = "0";
      wRange.max = "3";
      wRange.step = "0.1";
      wRange.value = String(weight);
      const wLabel = document.createElement("span");
      wLabel.className = "weight-value";
      wLabel.textContent = weight.toFixed(1);
      wRange.addEventListener("input", () => {
        const v = parseFloat(wRange.value) || 0;
        wLabel.textContent = v.toFixed(1);
        autolightEffectsPending[item.name] = autolightEffectsPending[item.name] || {};
        autolightEffectsPending[item.name].weight = v;
      });
      tdWeight.appendChild(wRange);
      tdWeight.appendChild(wLabel);
      tr.appendChild(tdWeight);

      const tdDuration = document.createElement("td");
      const dInput = document.createElement("input");
      dInput.type = "number";
      dInput.step = "0.25";
      dInput.min = "0.25";
      dInput.max = "32";
      dInput.value = String(duration);
      dInput.addEventListener("change", () => {
        const v = parseFloat(dInput.value);
        if (!Number.isFinite(v) || v <= 0) return;
        autolightEffectsPending[item.name] = autolightEffectsPending[item.name] || {};
        autolightEffectsPending[item.name].duration_beats = roundTo(v, 0.25);
      });
      tdDuration.appendChild(dInput);
      tr.appendChild(tdDuration);

      const tdCooldown = document.createElement("td");
      const cInput = document.createElement("input");
      cInput.type = "number";
      cInput.step = "0.25";
      cInput.min = "0";
      cInput.max = "16";
      cInput.value = String(cooldown);
      cInput.addEventListener("change", () => {
        const v = parseFloat(cInput.value);
        if (!Number.isFinite(v) || v < 0) return;
        autolightEffectsPending[item.name] = autolightEffectsPending[item.name] || {};
        autolightEffectsPending[item.name].cooldown_bars = roundTo(v, 0.25);
      });
      tdCooldown.appendChild(cInput);
      tr.appendChild(tdCooldown);

      const tdPreview = document.createElement("td");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "secondary effect-trigger";
      btn.textContent = "Fire now";
      btn.addEventListener("click", () => {
        postAutolightEffectTrigger(item.name);
      });
      tdPreview.appendChild(btn);
      tr.appendChild(tdPreview);

      tbody.appendChild(tr);
    });
  }

  async function saveAutolightEffectsConfig() {
    // Start with the server's currently-stored config then layer pending overrides.
    const base = {};
    autolightEffectsCache.forEach((item) => {
      if (item.config && typeof item.config === "object") {
        base[item.name] = { ...item.config };
      }
    });
    for (const [name, patch] of Object.entries(autolightEffectsPending)) {
      base[name] = { ...(base[name] || {}), ...patch };
    }
    try {
      const res = await fetch("/api/autolight/effects/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ effect_config: base }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast(data.error || "Save failed", "error");
        return;
      }
      if (dmxSettingsCache && typeof dmxSettingsCache === "object" && data.autolight) {
        dmxSettingsCache.autolight = data.autolight;
      }
      autolightEffectsCache = Array.isArray(data.items) ? data.items : autolightEffectsCache;
      autolightEffectsPending = {};
      toast("Effects config saved", "success");
      renderAutolightEffectsTable();
    } catch (err) {
      toast("Save failed", "error");
    }
  }

  async function resetAutolightEffectsConfig() {
    try {
      const res = await fetch("/api/autolight/effects/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ effect_config: {} }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast(data.error || "Reset failed", "error");
        return;
      }
      if (dmxSettingsCache && typeof dmxSettingsCache === "object" && data.autolight) {
        dmxSettingsCache.autolight = data.autolight;
      }
      autolightEffectsCache = Array.isArray(data.items) ? data.items : autolightEffectsCache;
      autolightEffectsPending = {};
      toast("All effects reset to defaults", "success");
      renderAutolightEffectsTable();
    } catch (err) {
      toast("Reset failed", "error");
    }
  }

  async function postAutolightEffectTrigger(name) {
    try {
      const res = await fetch("/api/autolight/effects/trigger", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast(data.error || `Trigger ${name} failed`, "error");
        return;
      }
      toast(`Triggered ${name}`, "success");
    } catch (err) {
      toast(`Trigger ${name} failed`, "error");
    }
  }

  async function postAutolightSoundCloudClientId(value) {
    try {
      const current = (dmxSettingsCache && dmxSettingsCache.autolight) ? { ...dmxSettingsCache.autolight } : {};
      current.soundcloud_client_id = String(value || "").trim();
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ autolight: current }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast(data.error || "SoundCloud save failed", "error");
        return;
      }
      if (dmxSettingsCache && typeof dmxSettingsCache === "object" && data.autolight) {
        dmxSettingsCache.autolight = data.autolight;
      }
      toast("SoundCloud client_id saved", "success");
      fetchAutolightStatus(true);
    } catch (err) {
      toast("SoundCloud save failed", "error");
    }
  }

  async function fetchAutolightAudioDevices() {
    const select = document.getElementById("autolight-audio-device");
    if (!select) return;
    try {
      const res = await fetch("/api/autolight/audio-devices", { cache: "no-store" });
      if (!res.ok) throw new Error("audio device list failed");
      const data = await res.json();
      const items = Array.isArray(data.items) ? data.items : [];
      const current = data.current === null || data.current === undefined ? "default" : String(data.current);
      select.innerHTML = "";
      const defaultOpt = document.createElement("option");
      defaultOpt.value = "default";
      defaultOpt.textContent = "Default speaker";
      select.appendChild(defaultOpt);
      for (const item of items) {
        const opt = document.createElement("option");
        opt.value = String(item.index);
        opt.textContent = `${item.name} (${item.sample_rate}Hz, ${item.channels}ch)`;
        select.appendChild(opt);
      }
      select.value = current;
    } catch (err) {
      /* leave the default option */
    }
  }

  async function postAutolightAudioDevice(indexValue) {
    try {
      const body = indexValue === "default" ? { index: null } : { index: Number(indexValue) };
      const res = await fetch("/api/autolight/audio-devices", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast(data.error || "Audio source change failed", "error");
        return;
      }
      if (data.status) {
        autolightStatusCache = data.status;
        updateAutolightSection(data.status);
      }
    } catch (err) {
      toast("Audio source change failed", "error");
    }
  }

  async function fetchAutolightStatus(force = false) {
    try {
      const res = await fetch(`/api/autolight/status${force ? "?refresh=1" : ""}`, { cache: "no-store" });
      if (!res.ok) throw new Error("autolight status fetch failed");
      const data = await res.json();
      autolightStatusCache = data;
      if (dmxSettingsCache && typeof dmxSettingsCache === "object") {
        dmxSettingsCache.autolight_status = data;
      }
      updateAutolightSection(data);
      return data;
    } catch (err) {
      updateAutolightSection(autolightStatusCache || {});
      return autolightStatusCache || {};
    }
  }

  async function controlAutolight(payload) {
    try {
      const res = await fetch("/api/autolight/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {})
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast(data.error || "Auto-Light control failed", "error");
        return null;
      }
      if (dmxSettingsCache && typeof dmxSettingsCache === "object" && data.autolight) {
        dmxSettingsCache.autolight = data.autolight;
      }
      if (data.status && typeof data.status === "object") {
        autolightStatusCache = data.status;
        if (dmxSettingsCache && typeof dmxSettingsCache === "object") {
          dmxSettingsCache.autolight_status = data.status;
        }
        updateAutolightSection(data.status);
      }
      return data;
    } catch (err) {
      toast("Auto-Light control failed", "error");
      return null;
    }
  }

  async function createAutolightSnapshot() {
    try {
      const res = await fetch("/api/autolight/snapshots", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({})
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast(data.error || "Snapshot failed", "error");
        return null;
      }
      toast("Auto-Light snapshot captured", "success");
      return data;
    } catch (err) {
      toast("Snapshot failed", "error");
      return null;
    }
  }

  async function fetchDmxSettings() {
    try {
      const res = await fetch("/api/settings", { cache: "no-store" });
      if (!res.ok) throw new Error("settings fetch failed");
      const data = await res.json();
      dmxSettingsCache = data;
      if (data && typeof data === "object" && data.sync_video) {
        const sync = data.sync_video || {};
        const baseUrl = normalizeBaseUrl(sync.base_url || sync.baseUrl || DEFAULT_CONFIG.baseUrl);
        setConfig({
          enabled: Boolean(sync.enabled),
          baseUrl,
          token: sync.token || ""
        });
      }
      if (data && typeof data === "object") {
        const runtime = data.dmx_runtime || {};
        if (typeof window.setPlaybackUiFps === "function") {
          window.setPlaybackUiFps(runtime.playback_ui_fps);
        }
        if (typeof window.setCtcSettings === "function") {
          window.setCtcSettings(data.ctc || {});
        }
        if (data.autolight_status) {
          autolightStatusCache = data.autolight_status;
          updateAutolightSection(data.autolight_status);
        }
      }
      return data;
    } catch (err) {
      return dmxSettingsCache || {};
    }
  }

  function describeAppUpdateStatus(status) {
    const meta = getAppMeta();
    if (!status || typeof status !== "object") {
      return tfmt("settings.autoUpdateStatusCurrent", "Current version: {version}", { version: meta.version });
    }
    if (status.checking) {
      return t("settings.autoUpdateStatusChecking", "Checking for updates...");
    }
    if (status.error) {
      return tfmt("settings.autoUpdateStatusError", "Update check failed: {error}", { error: status.error });
    }
    if (status.available) {
      return tfmt(
        "settings.autoUpdateStatusAvailable",
        "Update available: {current} -> {latest}",
        {
          current: status.current_version || meta.version,
          latest: status.latest_version || "?",
        }
      );
    }
    if (status.last_checked_at) {
      return tfmt(
        "settings.autoUpdateStatusUpToDate",
        "You're up to date: {version}",
        { version: status.current_version || meta.version }
      );
    }
    return tfmt("settings.autoUpdateStatusCurrent", "Current version: {version}", { version: status.current_version || meta.version });
  }

  function renderAppUpdateStatus(root, status) {
    const scope = root || document;
    const current = status && typeof status === "object" ? status : (appUpdateStatusCache || {});
    const statusText = scope.querySelector("#app-update-status-text");
    const checkBtn = scope.querySelector("#app-update-check-now");
    const installBtn = scope.querySelector("#app-update-install");
    const releaseLink = scope.querySelector("#app-update-release-link");
    const supported = current.supported !== false;
    const installSupported = !!current.install_supported;
    const available = !!current.available;
    const installReady = installSupported && available && !!String(current.download_url || "").trim();
    const checking = !!current.checking;
    const installing = !!current.installing;

    if (statusText) {
      statusText.textContent = supported
        ? describeAppUpdateStatus(current)
        : t("settings.autoUpdateUnsupported", "Auto-update is unavailable in this mode.");
    }
    if (checkBtn instanceof HTMLButtonElement) {
      checkBtn.disabled = checking || installing || !supported;
      checkBtn.textContent = checking
        ? t("settings.autoUpdateCheckingButton", "Checking...")
        : t("settings.autoUpdateCheckNow", "Check now");
    }
    if (installBtn instanceof HTMLButtonElement) {
      installBtn.disabled = checking || installing || !installReady;
      installBtn.textContent = installing
        ? t("settings.autoUpdateInstalling", "Installing...")
        : t("settings.autoUpdateInstall", "Install update");
      installBtn.hidden = !installSupported;
    }
    if (releaseLink instanceof HTMLAnchorElement) {
      const href = String(current.release_url || "").trim();
      releaseLink.href = href || "#";
      releaseLink.hidden = !href;
    }
  }

  async function fetchAppUpdateStatus(options) {
    const opts = options || {};
    const root = opts.root || document;
    const forceCheck = !!opts.forceCheck;
    const manual = !!opts.manual;

    if (appUpdateRequest) {
      const status = await appUpdateRequest.catch(() => appUpdateStatusCache || {});
      renderAppUpdateStatus(root, status);
      return status;
    }

    if (!forceCheck) {
      try {
        const res = await fetch("/api/update/status", { cache: "no-store" });
        const data = await res.json().catch(() => ({}));
        appUpdateStatusCache = data;
        renderAppUpdateStatus(root, data);
        return data;
      } catch (err) {
        const next = {
          supported: false,
          install_supported: false,
          current_version: getAppMeta().version,
          error: err?.message || "status request failed",
        };
        appUpdateStatusCache = next;
        renderAppUpdateStatus(root, next);
        return next;
      }
    }

    appUpdateStatusCache = {
      ...(appUpdateStatusCache || {}),
      checking: true,
      error: "",
      supported: true,
      current_version: getAppMeta().version,
    };
    renderAppUpdateStatus(root, appUpdateStatusCache);
    appUpdateRequest = fetch("/api/update/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ manual }),
    })
      .then(async (res) => {
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          throw new Error(data.error || "update check failed");
        }
        return data;
      })
      .finally(() => {
        appUpdateRequest = null;
      });

    try {
      const data = await appUpdateRequest;
      appUpdateStatusCache = data;
      renderAppUpdateStatus(root, data);
      if (manual) {
        if (data.error) {
          toast(data.error, "error");
        } else if (data.available) {
          toast(
            tfmt("settings.autoUpdateToastAvailable", "Update {version} is available.", {
              version: data.latest_version || "?",
            }),
            "success"
          );
        } else {
          toast(
            tfmt("settings.autoUpdateToastUpToDate", "You're already on {version}.", {
              version: data.current_version || getAppMeta().version,
            }),
            "info"
          );
        }
      }
      return data;
    } catch (err) {
      const next = {
        ...(appUpdateStatusCache || {}),
        checking: false,
        error: err?.message || "update check failed",
      };
      appUpdateStatusCache = next;
      renderAppUpdateStatus(root, next);
      if (manual) toast(next.error, "error");
      return next;
    }
  }

  async function installAppUpdate(root) {
    const scope = root || document;
    try {
      const res = await fetch("/api/update/install", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.error || "install failed");
      }
      appUpdateStatusCache = data;
      renderAppUpdateStatus(scope, data);
      if (data.error) {
        toast(data.error, "error");
      } else if (data.installing) {
        toast(t("settings.autoUpdateToastInstalling", "Update downloaded. The app will restart."), "success");
      } else {
        toast(t("settings.autoUpdateToastInstallUnavailable", "Install update is unavailable in this mode."), "warning");
      }
      return data;
    } catch (err) {
      const next = {
        ...(appUpdateStatusCache || {}),
        error: err?.message || "install failed",
      };
      appUpdateStatusCache = next;
      renderAppUpdateStatus(scope, next);
      toast(next.error, "error");
      return next;
    }
  }

  function bindAppUpdateControls(root) {
    const scope = root || document;
    const checkBtn = scope.querySelector("#app-update-check-now");
    const installBtn = scope.querySelector("#app-update-install");

    if (checkBtn instanceof HTMLButtonElement && checkBtn.dataset.bound !== "1") {
      checkBtn.dataset.bound = "1";
      checkBtn.addEventListener("click", () => {
        fetchAppUpdateStatus({ root: scope, forceCheck: true, manual: true });
      });
    }
    if (installBtn instanceof HTMLButtonElement && installBtn.dataset.bound !== "1") {
      installBtn.dataset.bound = "1";
      installBtn.addEventListener("click", () => {
        installAppUpdate(scope);
      });
    }
    renderAppUpdateStatus(scope, appUpdateStatusCache);
  }

  async function saveDmxSettings(dmxTargetIp, syncVideo, runtime, ctc, whatsNew, autoUpdate, autolight, cueEditor) {
    try {
      const payload = {
        dmx_target_ip: dmxTargetIp
      };
      if (syncVideo && typeof syncVideo === "object") {
        payload.sync_video = {
          enabled: Boolean(syncVideo.enabled),
          base_url: normalizeBaseUrl(syncVideo.baseUrl || syncVideo.base_url || ""),
          token: String(syncVideo.token || "").trim()
        };
      }
      if (runtime && typeof runtime === "object") {
        payload.dmx_runtime = runtime;
      }
      if (ctc && typeof ctc === "object") {
        payload.ctc = {
          enabled: Boolean(ctc.enabled),
          keybind: String(ctc.keybind || "F8").trim() || "F8",
          capture_release: Boolean(ctc.capture_release),
        };
      }
      if (whatsNew && typeof whatsNew === "object") {
        payload.whats_new = {
          show_on_startup: Boolean(whatsNew.show_on_startup),
        };
      }
      if (autoUpdate && typeof autoUpdate === "object") {
        payload.auto_update = {
          check_on_startup: Boolean(autoUpdate.check_on_startup),
        };
      }
      if (autolight && typeof autolight === "object") {
        payload.autolight = {
          enabled: Boolean(autolight.enabled),
          mode: String(autolight.mode || "live"),
          source_mode: String(autolight.source_mode || "player_metadata_then_local"),
          freeze_global: Boolean(autolight.freeze_global),
          allow_guarded_channels: Boolean(autolight.allow_guarded_channels),
          snapshot_auto_capture: Boolean(autolight.snapshot_auto_capture),
          override_timeout_ms: Number(autolight.override_timeout_ms || 5000),
          confidence_threshold: Number(autolight.confidence_threshold ?? 0.75),
          energy_sensitivity: Number(autolight.energy_sensitivity ?? 1.0),
          movement_sensitivity: Number(autolight.movement_sensitivity ?? 1.0),
        };
      }
      if (cueEditor && typeof cueEditor === "object") {
        payload.cue_editor = {
          view_mode: cueEditor.view_mode || "classic",
          timeline_priority_mode: cueEditor.timeline_priority_mode || "top",
          zoom_x: Number(cueEditor.zoom_x || 120),
          zoom_y: Number(cueEditor.zoom_y || 88),
        };
      }
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast(data.error || t("settings.saveFailed", "Settings save failed"), "error");
        return false;
      }
      dmxSettingsCache = data;
      return true;
    } catch (err) {
      toast(t("settings.saveFailed", "Settings save failed"), "error");
      return false;
    }
  }

  function getConfig() {
    return { ...syncConfig };
  }

  async function callSyncVideoApi(action, payload) {
    const cfg = getConfig();
    if (!cfg.enabled) {
      const now = Date.now();
      if (now - lastDisabledWarnTs > DISABLED_WARN_INTERVAL_MS) {
        lastDisabledWarnTs = now;
        toast(t("syncVideo.disabled", "Sync Video disabled"), "warning");
      }
      return false;
    }

    const base = normalizeBaseUrl(cfg.baseUrl);
    if (!base) {
      toast(t("syncVideo.baseUrlMissing", "Sync Video URL missing"), "error");
      return false;
    }

    const endpoint = ACTION_ENDPOINTS[action];
    if (!endpoint) return false;

    const headers = { "Content-Type": "application/json" };
    if (cfg.token) headers["x-api-token"] = cfg.token;

    try {
      const res = await fetch(base + endpoint, {
        method: "POST",
        headers,
        body: JSON.stringify(payload || {})
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        const msg = data.error || res.statusText || t("syncVideo.requestFailed", "Request failed");
        toast(msg, "error");
        return false;
      }
      return true;
    } catch (err) {
      toast(t("syncVideo.requestFailed", "Sync Video request failed"), "error");
      return false;
    }
  }

  function buildCueName(action, payload) {
    if (action === "play") return t("syncVideo.cueName.play", "Sync Video: Play");
    if (action === "pause") return t("syncVideo.cueName.pause", "Sync Video: Pause");
    if (action === "seek") {
      return tfmt("syncVideo.cueName.seek", "Sync Video: Seek {time}s", { time: payload.time });
    }
    if (action === "load_video") {
      return tfmt(
        "syncVideo.cueName.loadVideo",
        "Sync Video: Load {name}",
        { name: payload.name || t("syncVideo.cueName.videoFallback", "video") }
      );
    }
    if (action === "load_layout") {
      return tfmt(
        "syncVideo.cueName.loadLayout",
        "Sync Video: Layout {id}",
        { id: payload.id || t("syncVideo.cueName.layoutFallback", "layout") }
      );
    }
    return t("syncVideo.cueName.default", "Sync Video");
  }

  function createSyncCue(action, payload) {
    const seq = cuesObj.sequence || [];
    const step = {
      name: buildCueName(action, payload),
      sleep: "0",
      duration: "0",
      devices: null,
      sync_video: {
        action,
        ...payload
      }
    };
    seq.push(step);
    cuesObj.sequence = seq;

    selectedCueIndex = seq.length - 1;
    selectedCueIndices.clear();
    selectedCueIndices.add(selectedCueIndex);
    renderCueTable();
    fillCuePropsFromSelected();
    toast(t("syncVideo.cueCreated", "Sync cue created"), "success");
  }

  async function runCueAction(step) {
    if (!step || !step.sync_video) return;
    const action = step.sync_video.action;
    if (!action) return;

    if (action === "play") {
      await callSyncVideoApi("play");
    } else if (action === "pause") {
      await callSyncVideoApi("pause");
    } else if (action === "seek") {
      const time = parseFloat(step.sync_video.time);
      if (!Number.isFinite(time)) return;
      await callSyncVideoApi("seek", { time });
    } else if (action === "load_video") {
      const name = step.sync_video.name || step.sync_video.src;
      if (!name) return;
      await callSyncVideoApi("load_video", { name });
    } else if (action === "load_layout") {
      const id = step.sync_video.id;
      if (!id) return;
      await callSyncVideoApi("load_layout", { id });
    }
  }

  async function openSettingsModal() {
    const cfg = getConfig();
    const dmxSettings = await fetchDmxSettings();
    const dmxIpValue = dmxSettings.dmx_target_ip || dmxSettings.local_ip || "";
    const localIpHint = dmxSettings.local_ip ? `(${dmxSettings.local_ip})` : "";
    const runtime = dmxSettings.dmx_runtime || {};
    const ctc = dmxSettings.ctc || {};
    const autoUpdate = dmxSettings.auto_update || {};
    const autolight = dmxSettings.autolight || {};
    const autolightStatus = dmxSettings.autolight_status || {};

    if (!window.Swal || !window.Swal.fire) {
      const dmxTargetIp = window.prompt(
        `${t("settings.dmxTargetIp", "DMX target IP")} ${localIpHint}`.trim(),
        dmxIpValue || "127.0.0.1"
      );
      if (dmxTargetIp === null) return;
      if (!dmxTargetIp.trim()) {
        toast(t("settings.dmxIpRequired", "DMX target IP required"), "error");
        return;
      }

      const enabled = window.confirm(t("settings.syncVideoEnable", "Enable Sync Video"));
      let baseUrl = cfg.baseUrl || DEFAULT_CONFIG.baseUrl;
      let token = cfg.token || "";

      if (enabled) {
        const basePrompt = window.prompt(
          t("settings.syncVideoUrl", "Sync Video URL"),
          baseUrl
        );
        if (basePrompt === null) return;
        baseUrl = normalizeBaseUrl(basePrompt);
        if (!baseUrl) {
          toast(t("settings.urlRequired", "URL required when Sync Video enabled"), "error");
          return;
        }

        const tokenPrompt = window.prompt(
          t("settings.syncVideoToken", "Sync Video Token"),
          token
        );
        if (tokenPrompt === null) return;
        token = tokenPrompt.trim();
      }

      const ok = await saveDmxSettings(dmxTargetIp.trim(), { enabled, baseUrl, token });
      if (!ok) return;
      setConfig({ enabled, baseUrl, token });
      return;
    }

    const maxSendHz = Number(runtime.max_send_hz ?? 40);
    const heartbeatSec = Number(runtime.heartbeat_sec ?? 0.1);
        const smoothStep = Number(runtime.smooth_step ?? 2);
        const smoothPredict = Boolean(runtime.smooth_predict);
        const smoothDisable = Boolean(runtime.smooth_disable);
    const deadband = Number(runtime.deadband ?? 0);
    const quantize = Number(runtime.quantize ?? 1);
    const emitHz = Number(runtime.emit_hz ?? 500);
    const previewHz = Number(runtime.preview_hz ?? 30);
    const playbackClockMode = String(runtime.playback_clock_mode || "timeline");
    const playbackEngineHz = Number(runtime.playback_engine_hz ?? 120);
    const playbackUiFps = Number(runtime.playback_ui_fps ?? 12);
    const cueEditor = dmxSettings.cue_editor || {};
    const cueEditorViewMode = String(cueEditor.view_mode || "classic");
    const cueEditorPriorityMode = String(cueEditor.timeline_priority_mode || "top");
    const cueEditorZoomX = Number(cueEditor.zoom_x ?? 120);
    const cueEditorZoomY = Number(cueEditor.zoom_y ?? 88);
    const ctcEnabled = Boolean(ctc.enabled);
    const ctcCaptureRelease = Boolean(ctc.capture_release);
    const whatsNewShowOnStartup = Boolean((dmxSettings.whats_new || {}).show_on_startup !== false);
    const autoUpdateCheckOnStartup = Boolean(autoUpdate.check_on_startup !== false);
    const autolightEnabled = Boolean(autolight.enabled);
    const autolightMode = String(autolight.mode || "live");
    const autolightSourceMode = String(autolight.source_mode || "player_metadata_then_local");
    const autolightFreezeGlobal = Boolean(autolight.freeze_global);
    const autolightAllowGuarded = Boolean(autolight.allow_guarded_channels);
    const autolightSnapshotAuto = Boolean(autolight.snapshot_auto_capture);
    const autolightOverrideTimeout = Number(autolight.override_timeout_ms ?? 5000);
    const autolightConfidenceThreshold = Number(autolight.confidence_threshold ?? 0.75);
    const autolightEnergySensitivity = Number(autolight.energy_sensitivity ?? 1.0);
    const autolightMovementSensitivity = Number(autolight.movement_sensitivity ?? 1.0);
    const autolightRunningPlayers = Array.isArray(autolightStatus.running_players) ? autolightStatus.running_players.join(", ") : "";
    const autolightSummary = String(autolightStatus.summary || "Auto-Light ready");
    const autolightTrackTitle = autolightStatus.track?.title || "";
    const autolightTrackArtist = autolightStatus.track?.artist || "";
    const ctcKeybind = typeof window.normalizeCtcKeybindValue === "function"
      ? window.normalizeCtcKeybindValue(ctc.keybind)
      : String(ctc.keybind || "F8").trim() || "F8";

    const html = `
      <div class="dmx-settings-form">
        <div class="dmx-settings-section" data-advanced="false">
          <div class="dmx-settings-section-title">${t("settings.generalTitle", "General")}</div>
          <label for="dmx-target-ip">${t("settings.dmxTargetIp", "DMX target IP")} ${localIpHint}</label>
          <input id="dmx-target-ip" type="text" value="${escapeHtml(dmxIpValue)}" placeholder="${escapeHtml(dmxSettings.local_ip || '127.0.0.1')}">
          <div class="dmx-settings-row">
            <input id="whats-new-startup-enabled" type="checkbox" ${whatsNewShowOnStartup ? "checked" : ""}>
            <label for="whats-new-startup-enabled">${t("settings.whatsNewOnStartup", "Show What's New on startup")}</label>
          </div>
          <div class="dmx-settings-row">
            <input id="auto-update-startup-enabled" type="checkbox" ${autoUpdateCheckOnStartup ? "checked" : ""}>
            <label for="auto-update-startup-enabled">${t("settings.autoUpdateOnStartup", "Check for app updates on startup")}</label>
          </div>
          <div class="dmx-settings-row align-start spread">
            <div class="dmx-toggle-text">
              <div class="dmx-settings-label">${t("settings.autoUpdateTitle", "Application update")}</div>
              <div id="app-update-status-text" class="dmx-settings-desc">${tfmt("settings.autoUpdateStatusCurrent", "Current version: {version}", { version: getAppMeta().version })}</div>
              <a
                id="app-update-release-link"
                class="dmx-settings-link"
                href="#"
                target="_blank"
                rel="noreferrer noopener"
              >${t("settings.autoUpdateOpenReleases", "Open releases page")}</a>
            </div>
            <div class="dmx-settings-actions">
              <button id="app-update-check-now" type="button" class="secondary">${t("settings.autoUpdateCheckNow", "Check now")}</button>
              <button id="app-update-install" type="button" class="primary">${t("settings.autoUpdateInstall", "Install update")}</button>
            </div>
          </div>
        </div>
        <div class="dmx-settings-section" data-advanced="false">
          <div class="dmx-settings-section-title">Auto-Light</div>
          <div class="dmx-settings-row">
            <input id="autolight-enabled" type="checkbox" ${autolightEnabled ? "checked" : ""}>
            <label for="autolight-enabled">Enable Auto-Light runtime</label>
          </div>
          <div class="dmx-settings-row">
            <div>
              <div class="dmx-settings-label">Mode</div>
              <div class="dmx-settings-desc">Live applies the runtime state, Assist prepares the engine without taking full control.</div>
            </div>
            <select id="autolight-mode">
              <option value="off" ${autolightMode === "off" ? "selected" : ""}>Off</option>
              <option value="assist" ${autolightMode === "assist" ? "selected" : ""}>Assist</option>
              <option value="live" ${autolightMode === "live" ? "selected" : ""}>Live</option>
            </select>
          </div>
          <div class="dmx-settings-row">
            <div>
              <div class="dmx-settings-label">Music source policy</div>
              <div class="dmx-settings-desc">Player metadata is the current source path. Local file analysis will plug into the same pipeline later.</div>
            </div>
            <select id="autolight-source-mode">
              <option value="player_metadata_then_local" ${autolightSourceMode === "player_metadata_then_local" ? "selected" : ""}>Player metadata then local</option>
              <option value="player_metadata_only" ${autolightSourceMode === "player_metadata_only" ? "selected" : ""}>Player metadata only</option>
              <option value="local_file_only" ${autolightSourceMode === "local_file_only" ? "selected" : ""}>Local file only</option>
            </select>
          </div>
          <div class="dmx-settings-row">
            <input id="autolight-freeze-global" type="checkbox" ${autolightFreezeGlobal ? "checked" : ""}>
            <label for="autolight-freeze-global">Freeze global output</label>
          </div>
          <div class="dmx-settings-row">
            <input id="autolight-allow-guarded" type="checkbox" ${autolightAllowGuarded ? "checked" : ""}>
            <label for="autolight-allow-guarded">Allow guarded channels</label>
          </div>
          <div class="dmx-settings-row">
            <input id="autolight-snapshot-auto" type="checkbox" ${autolightSnapshotAuto ? "checked" : ""}>
            <label for="autolight-snapshot-auto">Auto-capture snapshots</label>
          </div>
          <div class="dmx-settings-slider">
            <label for="autolight-override-timeout">Override timeout</label>
            <div class="dmx-settings-range">
              <input id="autolight-override-timeout" type="range" min="500" max="60000" step="100" value="${autolightOverrideTimeout}">
              <span id="autolight-override-timeout-value">${autolightOverrideTimeout} ms</span>
            </div>
            <div class="dmx-settings-desc">How long a manual override should block Auto-Light on the touched area.</div>
          </div>
          <div class="dmx-settings-slider">
            <label for="autolight-confidence-threshold">Confidence threshold</label>
            <div class="dmx-settings-range">
              <input id="autolight-confidence-threshold" type="range" min="0" max="1" step="0.01" value="${autolightConfidenceThreshold}">
              <span id="autolight-confidence-threshold-value">${autolightConfidenceThreshold.toFixed(2)}</span>
            </div>
            <div class="dmx-settings-desc">Minimum confidence future analyzers must reach before guarded capabilities can be used.</div>
          </div>
          <div class="dmx-settings-slider">
            <label for="autolight-energy-sensitivity">Energy sensitivity</label>
            <div class="dmx-settings-range">
              <input id="autolight-energy-sensitivity" type="range" min="0.1" max="2" step="0.05" value="${autolightEnergySensitivity}">
              <span id="autolight-energy-sensitivity-value">${autolightEnergySensitivity.toFixed(2)}</span>
            </div>
            <div class="dmx-settings-desc">Scales how strongly future musical energy will influence intensity and accents.</div>
          </div>
          <div class="dmx-settings-slider">
            <label for="autolight-movement-sensitivity">Movement sensitivity</label>
            <div class="dmx-settings-range">
              <input id="autolight-movement-sensitivity" type="range" min="0.1" max="2" step="0.05" value="${autolightMovementSensitivity}">
              <span id="autolight-movement-sensitivity-value">${autolightMovementSensitivity.toFixed(2)}</span>
            </div>
            <div class="dmx-settings-desc">Scales how strongly future movement planning will react to musical activity.</div>
          </div>
          <div class="dmx-settings-desc">${escapeHtml(autolightSummary)}</div>
          <div class="dmx-settings-desc">${escapeHtml(autolightRunningPlayers ? `Detected players: ${autolightRunningPlayers}` : "Detected players: none")}</div>
          <div class="dmx-settings-desc">${escapeHtml(autolightTrackTitle ? `Track: ${autolightTrackTitle}${autolightTrackArtist ? ` - ${autolightTrackArtist}` : ""}` : "Track metadata: not available yet")}</div>
        </div>
        <div class="dmx-settings-section" data-advanced="false">
          <div class="dmx-settings-section-title">Cue Editor</div>
          <div class="dmx-settings-row">
            <div>
              <div class="dmx-settings-label">View mode</div>
              <div class="dmx-settings-desc">Classic keeps the list editor. Timeline unlocks lanes, zoom and block editing. Rapid Fire shows one launch pad per cue list.</div>
            </div>
            <div class="dmx-segmented">
              <input id="cue-editor-view-mode" type="hidden" value="${["timeline", "rapidfire"].includes(cueEditorViewMode) ? cueEditorViewMode : "classic"}">
              <button type="button" class="dmx-segmented-btn" data-value="classic">Classic</button>
              <button type="button" class="dmx-segmented-btn" data-value="timeline">Timeline</button>
              <button type="button" class="dmx-segmented-btn" data-value="rapidfire">Rapid Fire</button>
            </div>
          </div>
          <div class="dmx-settings-row">
            <div>
              <div class="dmx-settings-label">Lane priority</div>
              <div class="dmx-settings-desc">Top = upper lane wins, Bottom = lower lane wins, Merge = last write wins on conflicts.</div>
            </div>
            <select id="cue-editor-priority-mode">
              <option value="top" ${cueEditorPriorityMode === "top" ? "selected" : ""}>Top</option>
              <option value="bottom" ${cueEditorPriorityMode === "bottom" ? "selected" : ""}>Bottom</option>
              <option value="merge" ${cueEditorPriorityMode === "merge" ? "selected" : ""}>Merge</option>
            </select>
          </div>
          <div class="dmx-settings-slider">
            <label for="cue-editor-zoom-x">Timeline zoom X</label>
            <div class="dmx-settings-range">
              <input id="cue-editor-zoom-x" type="range" min="20" max="480" step="5" value="${cueEditorZoomX}">
              <span id="cue-editor-zoom-x-value">${cueEditorZoomX} px/s</span>
            </div>
            <div class="dmx-settings-desc">Horizontal zoom used by the timeline editor.</div>
          </div>
          <div class="dmx-settings-slider">
            <label for="cue-editor-zoom-y">Timeline zoom Y</label>
            <div class="dmx-settings-range">
              <input id="cue-editor-zoom-y" type="range" min="48" max="240" step="4" value="${cueEditorZoomY}">
              <span id="cue-editor-zoom-y-value">${cueEditorZoomY} px</span>
            </div>
            <div class="dmx-settings-desc">Lane height used by the timeline editor.</div>
          </div>
        </div>
        <div class="dmx-settings-section" data-advanced="false">
          <div class="dmx-settings-section-title">${t("settings.syncVideoTitle", "Sync Video")}</div>
          <div class="dmx-settings-row">
            <input id="sync-video-enabled" type="checkbox" ${cfg.enabled ? "checked" : ""}>
            <label for="sync-video-enabled">${t("settings.syncVideoEnable", "Enable Sync Video")}</label>
          </div>
          <div>
            <label for="sync-video-url">${t("settings.syncVideoUrl", "Sync Video URL")}</label>
            <input id="sync-video-url" type="url" value="${escapeHtml(cfg.baseUrl)}" placeholder="http://127.0.0.1:3000">
          </div>
          <div>
            <label for="sync-video-token">${t("settings.syncVideoToken", "Sync Video Token")}</label>
            <input id="sync-video-token" type="text" value="${escapeHtml(cfg.token)}" placeholder="token">
          </div>
        </div>
        <div class="dmx-settings-section" data-advanced="false">
          <div class="dmx-settings-section-title">CTC</div>
          <div class="dmx-settings-row">
            <input id="ctc-enabled" type="checkbox" ${ctcEnabled ? "checked" : ""}>
            <label for="ctc-enabled">Enable Cue Time Creator</label>
          </div>
          <div class="dmx-settings-row">
            <input id="ctc-capture-release" type="checkbox" ${ctcCaptureRelease ? "checked" : ""}>
            <label for="ctc-capture-release">Capture release</label>
          </div>
          <div>
            <label for="ctc-keybind">CTC keybind</label>
            <input
              id="ctc-keybind"
              type="text"
              inputmode="none"
              autocomplete="off"
              data-keybind="${escapeHtml(ctcKeybind)}"
              value="${escapeHtml(formatCtcKeybind(ctcKeybind))}"
              placeholder="Press a key"
            >
          </div>
          <div class="dmx-settings-desc">Pressing the bound key during CTC appends an empty cue with the captured sleep time.</div>
          <div class="dmx-settings-desc">Capture release: press creates one cue, release creates another cue, holding the key does nothing in between.</div>
        </div>
        <div class="dmx-settings-section" data-advanced="true">
          <div class="dmx-settings-section-title">${t("settings.dmxRuntimeTitle", "DMX Runtime")}</div>

          <div class="dmx-settings-row">
            <div>
              <div class="dmx-settings-label">${t("settings.emitHz", "Output rate (Hz)")}</div>
              <div class="dmx-settings-desc">${t("settings.emitHzDesc", "How often every universe is re-emitted to the nodes, changed or not — like a DMX interface. 500 Hz by default.")}</div>
            </div>
            <input id="rt-emit-hz" type="number" min="1" max="1000" step="1" value="${emitHz}">
          </div>

          <div class="dmx-settings-row">
            <div>
              <div class="dmx-settings-label">${t("settings.previewHz", "Preview rate (Hz)")}</div>
              <div class="dmx-settings-desc">${t("settings.previewHzDesc", "How often the browser is sent the values it displays. Only affects the on-screen rig, never the output.")}</div>
            </div>
            <input id="rt-preview-hz" type="number" min="1" max="120" step="1" value="${previewHz}">
          </div>

          <div class="dmx-settings-row">
            <div>
              <div class="dmx-settings-label">${t("settings.playbackClockMode", "Playback clock")}</div>
              <div class="dmx-settings-desc">${t("settings.playbackClockModeDesc", "Timeline = chain phases normally, Absolute clock = launch each cue on host clock from the precomputed plan")}</div>
            </div>
            <select id="rt-playback-clock-mode">
              <option value="timeline" ${playbackClockMode === "timeline" ? "selected" : ""}>Timeline</option>
              <option value="absolute_clock" ${playbackClockMode === "absolute_clock" ? "selected" : ""}>Absolute clock</option>
            </select>
          </div>

          <div class="dmx-settings-slider">
            <label for="rt-playback-engine-hz">Playback engine rate</label>
            <div class="dmx-settings-range">
              <input id="rt-playback-engine-hz" type="range" min="40" max="240" step="1" value="${playbackEngineHz}">
              <span id="rt-playback-engine-hz-value">${playbackEngineHz} Hz</span>
            </div>
            <div class="dmx-settings-desc">Backend render/send cadence during cue-list playback. Higher helps short sleeps and short fades.</div>
          </div>

          <div class="dmx-settings-slider">
            <label for="rt-playback-ui-fps">Playback UI FPS</label>
            <div class="dmx-settings-range">
              <input id="rt-playback-ui-fps" type="range" min="1" max="60" step="1" value="${playbackUiFps}">
              <span id="rt-playback-ui-fps-value">${playbackUiFps} fps</span>
            </div>
            <div class="dmx-settings-desc">Limit visual playback refreshes while the backend runner stays authoritative.</div>
          </div>

          <div class="dmx-settings-row">
            <label class="switch">
              <input id="rt-artnet-diff" type="checkbox" ${runtime.artnet_diff ? "checked" : ""}>
              <span class="slider"></span>
            </label>
            <div>
              <div class="dmx-settings-label">${t("settings.artnetDiff", "ArtNet diff mode")}</div>
              <div class="dmx-settings-desc">${t("settings.artnetDiffDesc", "Send only changed channels")}</div>
            </div>
          </div>

          <div class="dmx-settings-row">
            <label class="switch">
              <input id="rt-artnet-heartbeat-full" type="checkbox" ${runtime.artnet_heartbeat_full ? "checked" : ""}>
              <span class="slider"></span>
            </label>
            <div>
              <div class="dmx-settings-label">${t("settings.artnetHeartbeatFull", "Full frame on heartbeat")}</div>
              <div class="dmx-settings-desc">${t("settings.artnetHeartbeatFullDesc", "When heartbeat triggers, send full universe")}</div>
            </div>
          </div>

          <div class="dmx-settings-row">
            <label class="switch">
              <input id="rt-dummy" type="checkbox" ${runtime.dummy_enabled ? "checked" : ""}>
              <span class="slider"></span>
            </label>
            <div>
              <div class="dmx-settings-label">${t("settings.dummyChannels", "Dummy channels")}</div>
              <div class="dmx-settings-desc">${t("settings.dummyChannelsDesc", "Toggle spare channels 0/255 to force updates")}</div>
            </div>
          </div>

          <div class="dmx-settings-row">
            <label class="switch">
              <input id="rt-continuous" type="checkbox" ${runtime.continuous ? "checked" : ""}>
              <span class="slider"></span>
            </label>
            <div>
              <div class="dmx-settings-label">${t("settings.continuousMode", "Continuous send")}</div>
              <div class="dmx-settings-desc">${t("settings.continuousModeDesc", "Force full send each tick (simple engine)")}</div>
            </div>
          </div>

          <div class="dmx-settings-row">
            <label class="switch">
              <input id="rt-ui-force-full" type="checkbox" ${runtime.ui_force_full_send ? "checked" : ""}>
              <span class="slider"></span>
            </label>
            <div>
              <div class="dmx-settings-label">${t("settings.uiForceFullSend", "UI full send")}</div>
              <div class="dmx-settings-desc">${t("settings.uiForceFullSendDesc", "Send all UI channels every change")}</div>
            </div>
          </div>

          <div class="dmx-settings-slider">
            <label for="rt-max-send-hz">${t("settings.maxSendHz", "Max send rate")}</label>
            <div class="dmx-settings-range">
              <input id="rt-max-send-hz" type="range" min="1" max="120" step="1" value="${maxSendHz}">
              <span id="rt-max-send-hz-value">${maxSendHz} Hz</span>
            </div>
            <div class="dmx-settings-desc">${t("settings.maxSendHzDesc", "Limit ArtNet packets per second")}</div>
          </div>

          <div class="dmx-settings-slider">
            <label for="rt-heartbeat-sec">${t("settings.heartbeatSec", "Heartbeat")}</label>
            <div class="dmx-settings-range">
              <input id="rt-heartbeat-sec" type="range" min="0" max="5" step="0.01" value="${heartbeatSec}">
              <span id="rt-heartbeat-sec-value">${heartbeatSec.toFixed(2)} s</span>
            </div>
            <div class="dmx-settings-desc">${t("settings.heartbeatSecDesc", "Resend even if no change")}</div>
          </div>

          <div class="dmx-settings-slider">
            <label for="rt-smooth-step">${t("settings.smoothStep", "Movement smooth step")}</label>
            <div class="dmx-settings-range">
              <input id="rt-smooth-step" type="range" min="1" max="32" step="1" value="${smoothStep}">
              <span id="rt-smooth-step-value">${smoothStep}</span>
              <label class="dmx-inline-toggle">
                <input id="rt-smooth-predict" type="checkbox" ${smoothPredict ? "checked" : ""}>
                <span>${t("settings.smoothPredict", "Predict")}</span>
              </label>
              <label class="dmx-inline-toggle">
                <input id="rt-smooth-disable" type="checkbox" ${smoothDisable ? "checked" : ""}>
                <span>${t("settings.smoothDisable", "Disable")}</span>
              </label>
            </div>
            <div class="dmx-settings-desc">${t("settings.smoothStepDesc", "Step size for pan/tilt smoothing")}</div>
          </div>

          <div class="dmx-settings-slider">
            <label for="rt-deadband">${t("settings.deadband", "Deadband")}</label>
            <div class="dmx-settings-range">
              <input id="rt-deadband" type="range" min="0" max="64" step="1" value="${deadband}">
              <span id="rt-deadband-value">${deadband}</span>
            </div>
            <div class="dmx-settings-desc">${t("settings.deadbandDesc", "Ignore small value changes")}</div>
          </div>

          <div class="dmx-settings-slider">
            <label for="rt-quantize">${t("settings.quantize", "Quantize")}</label>
            <div class="dmx-settings-range">
              <input id="rt-quantize" type="range" min="1" max="64" step="1" value="${quantize}">
              <span id="rt-quantize-value">${quantize}</span>
            </div>
            <div class="dmx-settings-desc">${t("settings.quantizeDesc", "Round to multiples to reduce jitter")}</div>
          </div>
        </div>

        <div class="dmx-settings-section" data-advanced="true">
          <div class="dmx-settings-section-title">${t("settings.debugTitle", "Debug")}</div>
          <div class="dmx-settings-row">
            <label class="switch">
              <input id="rt-log-ui" type="checkbox" ${runtime.log_ui ? "checked" : ""}>
              <span class="slider"></span>
            </label>
            <div>
              <div class="dmx-settings-label">${t("settings.logUi", "Log UI payloads")}</div>
              <div class="dmx-settings-desc">${t("settings.logUiDesc", "Log data received from UI")}</div>
            </div>
          </div>

          <div class="dmx-settings-row">
            <label class="switch">
              <input id="rt-log-ui-full" type="checkbox" ${runtime.log_ui_full ? "checked" : ""}>
              <span class="slider"></span>
            </label>
            <div>
              <div class="dmx-settings-label">${t("settings.logUiFull", "Log UI full payloads")}</div>
              <div class="dmx-settings-desc">${t("settings.logUiFullDesc", "Log full UI payload content")}</div>
            </div>
          </div>

          <div class="dmx-settings-row">
            <label class="switch">
              <input id="rt-log-dmx" type="checkbox" ${runtime.log_dmx ? "checked" : ""}>
              <span class="slider"></span>
            </label>
            <div>
              <div class="dmx-settings-label">${t("settings.logDmx", "Log DMX frames")}</div>
              <div class="dmx-settings-desc">${t("settings.logDmxDesc", "Log outgoing DMX values")}</div>
            </div>
          </div>

          <div class="dmx-settings-row">
            <label class="switch">
              <input id="rt-log-dmx-full" type="checkbox" ${runtime.log_dmx_full ? "checked" : ""}>
              <span class="slider"></span>
            </label>
            <div>
              <div class="dmx-settings-label">${t("settings.logDmxFull", "Log full DMX frames")}</div>
              <div class="dmx-settings-desc">${t("settings.logDmxFullDesc", "Verbose DMX logs (large)")}</div>
            </div>
          </div>

          <div class="dmx-settings-row">
            <label class="switch">
              <input id="rt-log-artnet" type="checkbox" ${runtime.log_artnet ? "checked" : ""}>
              <span class="slider"></span>
            </label>
            <div>
              <div class="dmx-settings-label">${t("settings.logArtnet", "Log ArtNet send")}</div>
              <div class="dmx-settings-desc">${t("settings.logArtnetDesc", "Log ArtNet packets")}</div>
            </div>
          </div>

          <div class="dmx-settings-row">
            <label class="switch">
              <input id="rt-log-artnet-full" type="checkbox" ${runtime.log_artnet_full ? "checked" : ""}>
              <span class="slider"></span>
            </label>
            <div>
              <div class="dmx-settings-label">${t("settings.logArtnetFull", "Log full ArtNet data")}</div>
              <div class="dmx-settings-desc">${t("settings.logArtnetFullDesc", "Verbose ArtNet logs (large)")}</div>
            </div>
          </div>

          <div class="dmx-settings-row">
            <label class="switch">
              <input id="rt-profile-runner" type="checkbox" ${runtime.profile_runner ? "checked" : ""}>
              <span class="slider"></span>
            </label>
            <div>
              <div class="dmx-settings-label">Profile runner</div>
              <div class="dmx-settings-desc">Log backend render, send and state push timings every second.</div>
            </div>
          </div>
        </div>
      </div>
    `;

    if (isGuiWebView()) {
      openSettingsModalGui(t("settings.title", "General Settings"), html);
      return;
    }

    window.Swal.fire({
      title: t("settings.title", "General Settings"),
      html,
      showCancelButton: true,
      confirmButtonText: t("settings.save", "Save"),
      cancelButtonText: t("settings.cancel", "Cancel"),
      focusConfirm: false,
      heightAuto: false,
      customClass: { popup: "dmx-settings-modal" },
      didOpen: () => {
        bindRuntimeRangeListeners();
        bindCtcKeybindInput(document);
        mountAdvancedSettingsToggle(document, document.querySelector(".swal2-actions"));
        bindAppUpdateControls(document);
        fetchAppUpdateStatus({ root: document });
      },
      preConfirm: () => {
        const result = readSettingsForm((msg) => window.Swal.showValidationMessage(msg));
        if (!result) return false;
        return result;
      }
    }).then((result) => {
      if (!result.isConfirmed || !result.value) return;
      applySettingsResult(result.value);
    });
  }

  function bindSyncVideoControls() {
    const openSettingsBtn = document.getElementById("open-settings");
    if (openSettingsBtn) {
      openSettingsBtn.addEventListener("click", openSettingsModal);
    }

    const playBtn = document.getElementById("sync-video-play");
    if (playBtn) playBtn.addEventListener("click", () => callSyncVideoApi("play"));
    const playCueBtn = document.getElementById("sync-video-play-cue");
    if (playCueBtn) playCueBtn.addEventListener("click", () => createSyncCue("play", {}));

    const pauseBtn = document.getElementById("sync-video-pause");
    if (pauseBtn) pauseBtn.addEventListener("click", () => callSyncVideoApi("pause"));
    const pauseCueBtn = document.getElementById("sync-video-pause-cue");
    if (pauseCueBtn) pauseCueBtn.addEventListener("click", () => createSyncCue("pause", {}));

    const seekInput = document.getElementById("sync-video-seek");
    const seekBtn = document.getElementById("sync-video-seek-btn");
    if (seekBtn) {
      seekBtn.addEventListener("click", () => {
        const value = parseFloat(seekInput?.value);
        if (!Number.isFinite(value)) {
          toast(t("syncVideo.seekInvalid", "Invalid seek time"), "error");
          return;
        }
        callSyncVideoApi("seek", { time: value });
      });
    }
    const seekCueBtn = document.getElementById("sync-video-seek-cue");
    if (seekCueBtn) {
      seekCueBtn.addEventListener("click", () => {
        const value = parseFloat(seekInput?.value);
        if (!Number.isFinite(value)) {
          toast(t("syncVideo.seekInvalid", "Invalid seek time"), "error");
          return;
        }
        createSyncCue("seek", { time: value });
      });
    }

    const videoInput = document.getElementById("sync-video-video");
    const loadVideoBtn = document.getElementById("sync-video-load-video");
    if (loadVideoBtn) {
      loadVideoBtn.addEventListener("click", () => {
        const name = videoInput?.value.trim();
        if (!name) {
          toast(t("syncVideo.videoMissing", "Video name required"), "error");
          return;
        }
        callSyncVideoApi("load_video", { name });
      });
    }
    const loadVideoCueBtn = document.getElementById("sync-video-load-video-cue");
    if (loadVideoCueBtn) {
      loadVideoCueBtn.addEventListener("click", () => {
        const name = videoInput?.value.trim();
        if (!name) {
          toast(t("syncVideo.videoMissing", "Video name required"), "error");
          return;
        }
        createSyncCue("load_video", { name });
      });
    }

    const layoutInput = document.getElementById("sync-video-layout");
    const loadLayoutBtn = document.getElementById("sync-video-load-layout");
    if (loadLayoutBtn) {
      loadLayoutBtn.addEventListener("click", () => {
        const id = layoutInput?.value.trim();
        if (!id) {
          toast(t("syncVideo.layoutMissing", "Layout id required"), "error");
          return;
        }
        callSyncVideoApi("load_layout", { id });
      });
    }
    const loadLayoutCueBtn = document.getElementById("sync-video-load-layout-cue");
    if (loadLayoutCueBtn) {
      loadLayoutCueBtn.addEventListener("click", () => {
        const id = layoutInput?.value.trim();
        if (!id) {
          toast(t("syncVideo.layoutMissing", "Layout id required"), "error");
          return;
        }
        createSyncCue("load_layout", { id });
      });
    }
  }

  function bindAutolightControls() {
    const modeOff = document.getElementById("autolight-mode-off");
    if (modeOff) {
      modeOff.addEventListener("click", () => controlAutolight({ enabled: false, mode: "off" }));
    }

    const modeAssist = document.getElementById("autolight-mode-assist");
    if (modeAssist) {
      modeAssist.addEventListener("click", () => controlAutolight({ enabled: true, mode: "assist" }));
    }

    const modeLive = document.getElementById("autolight-mode-live");
    if (modeLive) {
      modeLive.addEventListener("click", () => controlAutolight({ enabled: true, mode: "live" }));
    }

    // Quick re-enable from the collapsed state. Defaults to live mode —
    // if the user wants assist they can switch via the buttons that
    // become visible once the panel expands.
    const quickEnable = document.getElementById("autolight-quick-enable");
    if (quickEnable) {
      quickEnable.addEventListener("click", () => controlAutolight({ enabled: true, mode: "live" }));
    }

    const refreshBtn = document.getElementById("autolight-refresh");
    if (refreshBtn) {
      refreshBtn.addEventListener("click", () => fetchAutolightStatus(true));
    }

    const snapshotBtn = document.getElementById("autolight-snapshot");
    if (snapshotBtn) {
      snapshotBtn.addEventListener("click", () => createAutolightSnapshot());
    }

    const freezeToggle = document.getElementById("autolight-freeze-toggle");
    if (freezeToggle) {
      freezeToggle.addEventListener("change", () => {
        controlAutolight({ freeze_global: Boolean(freezeToggle.checked) }).then((result) => {
          if (!result) {
            freezeToggle.checked = !freezeToggle.checked;
          }
        });
      });
    }

    const renderDirectorBtn = document.getElementById("autolight-render-director");
    if (renderDirectorBtn) {
      renderDirectorBtn.addEventListener("click", () => controlAutolight({ render_mode: "director" }));
    }
    const renderEffectsBtn = document.getElementById("autolight-render-effects");
    if (renderEffectsBtn) {
      renderEffectsBtn.addEventListener("click", () => controlAutolight({ render_mode: "effects" }));
    }
    const renderOffBtn = document.getElementById("autolight-render-off");
    if (renderOffBtn) {
      renderOffBtn.addEventListener("click", () => controlAutolight({ render_mode: "off" }));
    }
    const memoryToggle = document.getElementById("autolight-memory-toggle");
    if (memoryToggle) {
      memoryToggle.addEventListener("focus", () => { memoryToggle.dataset.userInteracting = "1"; });
      memoryToggle.addEventListener("blur", () => { memoryToggle.dataset.userInteracting = "0"; });
      memoryToggle.addEventListener("change", () => {
        controlAutolight({ memory_persistence: Boolean(memoryToggle.checked) });
      });
    }

    // AutoLight 2.0 guardrails mini-panel.
    const djCeiling = document.getElementById("dj-ceiling");
    const djCeilingVal = document.getElementById("dj-ceiling-val");
    if (djCeiling) {
      djCeiling.addEventListener("input", () => {
        if (djCeilingVal) djCeilingVal.textContent = `${djCeiling.value}%`;
      });
      djCeiling.addEventListener("change", () => {
        controlAutolight({ intensity_ceiling: (parseInt(djCeiling.value, 10) || 100) / 100 });
      });
    }
    const djContrast = document.getElementById("dj-contrast");
    const djContrastVal = document.getElementById("dj-contrast-val");
    if (djContrast) {
      djContrast.addEventListener("input", () => {
        if (djContrastVal) djContrastVal.textContent = `${djContrast.value}%`;
      });
      djContrast.addEventListener("change", () => {
        controlAutolight({ contrast: (parseInt(djContrast.value, 10) || 0) / 100 });
      });
    }
    const djSmallVenue = document.getElementById("dj-small-venue");
    if (djSmallVenue) {
      djSmallVenue.addEventListener("change", () => {
        controlAutolight({ small_venue: Boolean(djSmallVenue.checked) });
      });
    }
    const djAllowStrobe = document.getElementById("dj-allow-strobe");
    if (djAllowStrobe) {
      djAllowStrobe.addEventListener("change", () => {
        controlAutolight({ allow_strobe: Boolean(djAllowStrobe.checked) });
      });
    }
    const djMetadata = document.getElementById("dj-metadata");
    if (djMetadata) {
      djMetadata.addEventListener("change", () => {
        controlAutolight({ metadata_enabled: Boolean(djMetadata.checked) });
      });
    }

    const audioSelect = document.getElementById("autolight-audio-device");
    if (audioSelect) {
      audioSelect.addEventListener("focus", () => { audioSelect.dataset.userInteracting = "1"; });
      audioSelect.addEventListener("blur", () => { audioSelect.dataset.userInteracting = "0"; });
      audioSelect.addEventListener("change", () => {
        postAutolightAudioDevice(audioSelect.value);
      });
    }

    const scInput = document.getElementById("autolight-soundcloud-client-id");
    const scSave = document.getElementById("autolight-soundcloud-save");
    if (scInput) {
      scInput.addEventListener("focus", () => { scInput.dataset.userInteracting = "1"; });
      scInput.addEventListener("blur", () => { scInput.dataset.userInteracting = "0"; });
    }
    if (scSave && scInput) {
      scSave.addEventListener("click", () => {
        postAutolightSoundCloudClientId(scInput.value);
      });
    }

    const customizeBtn = document.getElementById("autolight-customize");
    if (customizeBtn) {
      customizeBtn.addEventListener("click", openAutolightEffectsModal);
    }
    const modal = document.getElementById("autolight-effects-modal");
    if (modal) {
      modal.addEventListener("click", (evt) => {
        if (evt.target instanceof HTMLElement && evt.target.dataset.modalClose === "1") {
          closeAutolightEffectsModal();
        }
      });
    }
    const saveBtn = document.getElementById("autolight-effects-save");
    if (saveBtn) {
      saveBtn.addEventListener("click", () => {
        saveAutolightEffectsConfig().then(closeAutolightEffectsModal);
      });
    }
    const resetBtn = document.getElementById("autolight-effects-reset");
    if (resetBtn) {
      resetBtn.addEventListener("click", resetAutolightEffectsConfig);
    }
    const tuningBtn = document.getElementById("autolight-effects-tuning");
    if (tuningBtn) {
      tuningBtn.addEventListener("click", openAutolightTuningModal);
    }
    const tuningModal = document.getElementById("autolight-tuning-modal");
    if (tuningModal) {
      tuningModal.addEventListener("click", (evt) => {
        if (evt.target instanceof HTMLElement && evt.target.dataset.modalClose === "1") {
          closeAutolightTuningModal();
        }
      });
    }
    const tuningSaveBtn = document.getElementById("autolight-tuning-save");
    if (tuningSaveBtn) tuningSaveBtn.addEventListener("click", saveAutolightTuning);
    const tuningResetBtn = document.getElementById("autolight-tuning-reset");
    if (tuningResetBtn) tuningResetBtn.addEventListener("click", resetAutolightTuning);

    // Main-panel controls
    const genreSelect = document.getElementById("autolight-genre-select");
    if (genreSelect) {
      genreSelect.addEventListener("focus", () => { genreSelect.dataset.userInteracting = "1"; });
      genreSelect.addEventListener("blur", () => { genreSelect.dataset.userInteracting = "0"; });
      genreSelect.addEventListener("change", () => postAutolightGenre(genreSelect.value));
    }
    const tapBtn = document.getElementById("autolight-tap-tempo");
    if (tapBtn) tapBtn.addEventListener("click", recordTapTempo);
    const tapResetBtn = document.getElementById("autolight-tap-reset");
    if (tapResetBtn) tapResetBtn.addEventListener("click", () => { autolightTapTimestamps = []; postAutolightTapTempo(null); });
    const calibrateBtn = document.getElementById("autolight-calibrate");
    if (calibrateBtn) calibrateBtn.addEventListener("click", () => postAutolightCalibrate(30));

    document.querySelectorAll("[data-scene-lock]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const scene = btn.dataset.sceneLock || "";
        postAutolightSceneLock(scene, 60);
      });
    });

    const topoBtn = document.getElementById("autolight-topology-btn");
    if (topoBtn) topoBtn.addEventListener("click", openAutolightTopologyModal);
    const topoModal = document.getElementById("autolight-topology-modal");
    if (topoModal) {
      topoModal.addEventListener("click", (evt) => {
        if (evt.target instanceof HTMLElement && evt.target.dataset.modalClose === "1") {
          closeAutolightTopologyModal();
        }
      });
    }
    document.querySelectorAll("#autolight-topology-table th[data-sort]").forEach((th) => {
      th.addEventListener("click", () => {
        const k = th.dataset.sort;
        if (autolightTopologySort.key === k) {
          autolightTopologySort.asc = !autolightTopologySort.asc;
        } else {
          autolightTopologySort.key = k;
          autolightTopologySort.asc = true;
        }
        renderAutolightTopologyTable();
      });
    });
    const topoSearch = document.getElementById("autolight-topology-search");
    if (topoSearch) topoSearch.addEventListener("input", renderAutolightTopologyTable);

    document.addEventListener("keydown", (evt) => {
      if (evt.key === "Escape") {
        const em = document.getElementById("autolight-effects-modal");
        const tm = document.getElementById("autolight-tuning-modal");
        const rm = document.getElementById("autolight-topology-modal");
        if (em && em.classList.contains("is-open")) closeAutolightEffectsModal();
        if (tm && tm.classList.contains("is-open")) closeAutolightTuningModal();
        if (rm && rm.classList.contains("is-open")) closeAutolightTopologyModal();
      }
    });
  }

  window.syncVideo = {
    getConfig,
    setConfig,
    callSyncVideoApi,
    runCueAction
  };

  document.addEventListener("DOMContentLoaded", () => {
    bindSyncVideoControls();
    bindAutolightControls();
    bindAutolightTrainingControls();
    updateSyncVideoSection();
    updateAutolightSection();
    fetchAutolightAudioDevices();
    fetchAutolightGenres();
    fetchDmxSettings().then(() => {
      updateSyncVideoSection();
      updateAutolightSection();
      fetchAutolightStatus(false);
    });
    if (autolightPollHandle) {
      window.clearInterval(autolightPollHandle);
    }
    autolightPollHandle = window.setInterval(() => {
      fetchAutolightStatus(false);
    }, 250);
    // High-frequency audio-only poll for the spectrogram + beat meters.
    // Hits the lightweight /api/autolight/audio endpoint at ~25 Hz so the
    // spectrogram tracks the audio analyzer (~48 Hz) instead of the 4 Hz
    // full-status poll.
    if (window._autolightAudioPollHandle) {
      window.clearInterval(window._autolightAudioPollHandle);
    }
    window._autolightAudioPollHandle = window.setInterval(fetchAutolightAudio, 40);
  });
})();
