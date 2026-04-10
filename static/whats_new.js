(function () {
  function getAppMeta() {
    const meta = window.APP_META || {};
    return {
      name: String(meta.name || "DDMX"),
      version: String(meta.version || "0.0.0"),
    };
  }

  async function fetchSettings() {
    const res = await fetch("/api/settings", { cache: "no-store" });
    if (!res.ok) throw new Error("settings fetch failed");
    return await res.json();
  }

  async function saveWhatsNewSettings(next) {
    const payload = {
      whats_new: {
        show_on_startup: !!next.show_on_startup,
        last_seen_version: String(next.last_seen_version || ""),
      }
    };
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error("settings save failed");
    return await res.json();
  }

  async function fetchWhatsNewPayload() {
    const res = await fetch("/api/whats_new/current", { cache: "no-store" });
    if (!res.ok) throw new Error("whats_new fetch failed");
    return await res.json();
  }

  async function openWhatsNewModal(options = {}) {
    if (!window.ui?.htmlModal) return false;
    const meta = getAppMeta();
    const payload = await fetchWhatsNewPayload();
    const settings = options.settings || await fetchSettings();
    const whatsNew = settings?.whats_new || {};
    const initialChecked = whatsNew.show_on_startup !== false;
    let latestChecked = initialChecked;

    const result = await window.ui.htmlModal({
      title: (typeof window.tfmt === "function")
        ? window.tfmt("whatsNew.title", "What's New in {version}?", { version: meta.version })
        : (payload.title || `What's New in ${meta.version}?`),
      html: payload.html || "",
      confirmText: "Close",
      cancelText: "Later",
      showCancel: false,
      modalClass: "dmx-whats-new-modal",
      initialFocusSelector: ".whats-new-focus-start",
      afterOpen: (form) => {
        form.scrollTop = 0;
        if (typeof window.applyI18nTranslations === "function") {
          window.applyI18nTranslations(form);
        }
        const toggle = form.querySelector("#whats-new-startup-toggle");
        if (toggle instanceof HTMLInputElement) {
          toggle.checked = initialChecked;
          const sync = () => {
            latestChecked = !!toggle.checked;
            const sw = toggle.closest(".switch");
            if (sw) sw.classList.toggle("is-checked", latestChecked);
          };
          toggle.addEventListener("change", sync);
          toggle.addEventListener("input", sync);
          sync();
        }
        if (typeof window.bindSwitchVisuals === "function") {
          window.bindSwitchVisuals(form);
        }
      },
      onSubmit: () => true,
    });

    await saveWhatsNewSettings({
      show_on_startup: latestChecked,
      last_seen_version: meta.version,
    });
    return !!result;
  }

  async function maybeShowWhatsNewOnStartup() {
    try {
      const settings = await fetchSettings();
      const whatsNew = settings?.whats_new || {};
      const showOnStartup = whatsNew.show_on_startup !== false;
      if (!showOnStartup) return;
      await openWhatsNewModal({ settings });
    } catch (err) {
      console.warn("[WHATS_NEW] startup failed:", err);
    }
  }

  window.openWhatsNewModal = openWhatsNewModal;

  document.addEventListener("DOMContentLoaded", () => {
    window.setTimeout(() => {
      maybeShowWhatsNewOnStartup();
    }, 650);
  });
})();
