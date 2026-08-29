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
      cueEditorSettings,
    } = result;
    const ok = await saveDmxSettings(
      dmxTargetIp,
      { enabled, baseUrl, token },
      runtimeSettings,
      ctcSettings,
      whatsNewSettings,
      autoUpdateSettings,
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

  async function saveDmxSettings(dmxTargetIp, syncVideo, runtime, ctc, whatsNew, autoUpdate, cueEditor) {
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

  document.addEventListener("DOMContentLoaded", () => {
    bindSyncVideoControls();
    updateSyncVideoSection();
    fetchDmxSettings().then(() => {
      updateSyncVideoSection();
    });
  });
})();
