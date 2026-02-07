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
      return data;
    } catch (err) {
      return dmxSettingsCache || {};
    }
  }

  async function saveDmxSettings(dmxTargetIp, syncVideo, runtime) {
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
    const deadband = Number(runtime.deadband ?? 0);
    const quantize = Number(runtime.quantize ?? 1);

    const html = `
      <div class="dmx-settings-form">
        <div class="dmx-settings-section">
          <div class="dmx-settings-section-title">${t("settings.generalTitle", "General")}</div>
          <label for="dmx-target-ip">${t("settings.dmxTargetIp", "DMX target IP")} ${localIpHint}</label>
          <input id="dmx-target-ip" type="text" value="${escapeHtml(dmxIpValue)}" placeholder="${escapeHtml(dmxSettings.local_ip || '127.0.0.1')}">
        </div>
        <div class="dmx-settings-section">
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
        <div class="dmx-settings-section">
          <div class="dmx-settings-section-title">${t("settings.dmxRuntimeTitle", "DMX Runtime")}</div>

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

        <div class="dmx-settings-section">
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
        </div>
      </div>
    `;

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
      },
      preConfirm: () => {
        const dmxTargetIp = document.getElementById("dmx-target-ip")?.value.trim();
        const enabled = document.getElementById("sync-video-enabled")?.checked || false;
        const baseUrl = normalizeBaseUrl(document.getElementById("sync-video-url")?.value || "");
        const token = document.getElementById("sync-video-token")?.value.trim() || "";

        const runtimeSettings = {
          artnet_diff: document.getElementById("rt-artnet-diff")?.checked || false,
          artnet_heartbeat_full: document.getElementById("rt-artnet-heartbeat-full")?.checked || false,
          dummy_enabled: document.getElementById("rt-dummy")?.checked || false,
          continuous: document.getElementById("rt-continuous")?.checked || false,
          ui_force_full_send: document.getElementById("rt-ui-force-full")?.checked || false,
          max_send_hz: parseFloat(document.getElementById("rt-max-send-hz")?.value || "40"),
          heartbeat_sec: parseFloat(document.getElementById("rt-heartbeat-sec")?.value || "0.1"),
          smooth_step: parseInt(document.getElementById("rt-smooth-step")?.value || "2", 10),
          deadband: parseInt(document.getElementById("rt-deadband")?.value || "0", 10),
          quantize: parseInt(document.getElementById("rt-quantize")?.value || "1", 10),
          log_ui: document.getElementById("rt-log-ui")?.checked || false,
          log_ui_full: document.getElementById("rt-log-ui-full")?.checked || false,
          log_dmx: document.getElementById("rt-log-dmx")?.checked || false,
          log_dmx_full: document.getElementById("rt-log-dmx-full")?.checked || false,
          log_artnet: document.getElementById("rt-log-artnet")?.checked || false,
          log_artnet_full: document.getElementById("rt-log-artnet-full")?.checked || false,
        };

        if (!dmxTargetIp) {
          window.Swal.showValidationMessage(t("settings.dmxIpRequired", "DMX target IP required"));
          return false;
        }
        if (enabled && !baseUrl) {
          window.Swal.showValidationMessage(t("settings.urlRequired", "URL required when Sync Video enabled"));
          return false;
        }
        return { enabled, baseUrl, token, dmxTargetIp, runtimeSettings };
      }
    }).then((result) => {
      if (!result.isConfirmed || !result.value) return;
      const { enabled, baseUrl, token, dmxTargetIp, runtimeSettings } = result.value;
      saveDmxSettings(dmxTargetIp, { enabled, baseUrl, token }, runtimeSettings);
      setConfig({ enabled, baseUrl, token });
      if (runtimeSettings && typeof runtimeSettings.ui_force_full_send === "boolean") {
        window.DMX_FORCE_FULL_SEND = runtimeSettings.ui_force_full_send;
      }
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

  window.syncVideo = {
    getConfig,
    setConfig,
    callSyncVideoApi,
    runCueAction
  };

  document.addEventListener("DOMContentLoaded", () => {
    bindSyncVideoControls();
    updateSyncVideoSection();
    fetchDmxSettings().then(updateSyncVideoSection);
  });
})();
