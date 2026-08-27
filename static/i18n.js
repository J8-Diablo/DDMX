// static/i18n.js
// Lightweight client-side i18n with JSON dictionaries (default: en, alt: fr)

(function() {
  const LANG_STORAGE_KEY = "dmx_lang";
  const DEFAULT_LANG = "en";
  let translations = {};
  let currentLang = DEFAULT_LANG;

  // Expose a safe lookup
  window.t = function t(key, fallback) {
    if (!key) return fallback || "";
    return translations[key] || fallback || key;
  };
  window.tfmt = function tfmt(key, fallback, params) {
    const template = window.t(key, fallback);
    if (!params) return template;
    return template.replace(/\{(\w+)\}/g, (_, k) => (params[k] == null ? "" : String(params[k])));
  };

  async function loadLanguage(lang) {
    const safeLang = lang || DEFAULT_LANG;
    try {
      const resp = await fetch(`/static/lang/${safeLang}.json?t=${Date.now()}`);
      if (!resp.ok) throw new Error(resp.statusText);
      const json = await resp.json();
      translations = json || {};
      currentLang = safeLang;
      localStorage.setItem(LANG_STORAGE_KEY, currentLang);
      if (window.APP_META) {
        window.APP_META.preferredLanguage = currentLang;
      }
      document.documentElement.lang = currentLang;
      applyTranslations();
    } catch (err) {
      console.warn("[i18n] load failed, fallback to en:", err);
      if (safeLang !== DEFAULT_LANG) {
        return loadLanguage(DEFAULT_LANG);
      }
    }
  }

  function applyTranslations(root = document) {
    const nodes = [];
    if (root instanceof Element && root.hasAttribute("data-i18n")) {
      nodes.push(root);
    }
    root.querySelectorAll?.("[data-i18n]")?.forEach((el) => nodes.push(el));
    nodes.forEach((el) => {
      const key = el.dataset.i18n;
      const attr = el.dataset.i18nAttr;
      const value = translations[key];
      if (!value) return;
      if (attr) {
        el.setAttribute(attr, value);
      } else {
        el.textContent = value;
      }
    });
    // Update dropdown state if present
    const select = document.getElementById("lang-select");
    if (select && select.value !== currentLang) {
      select.value = currentLang;
    }
    // Views that build their labels in JS (they have no data-i18n nodes to
    // rewrite) re-render on this event.
    document.dispatchEvent(new CustomEvent("i18n:applied", { detail: { lang: currentLang } }));
  }
  window.applyI18nTranslations = applyTranslations;

  function bindLanguageSwitcher() {
    const select = document.getElementById("lang-select");
    if (!select) return;
    select.addEventListener("change", (e) => {
      const lang = e.target.value || DEFAULT_LANG;
      loadLanguage(lang);
      persistLanguage(lang);
    });
  }

  async function persistLanguage(lang) {
    try {
      await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ui: {
            language: String(lang || DEFAULT_LANG).trim().toLowerCase() || DEFAULT_LANG,
          }
        })
      });
    } catch (err) {
      console.warn("[i18n] persist language failed:", err);
    }
  }

  async function resolveInitialLanguage() {
    const preferred = String(window.APP_META?.preferredLanguage || "").trim().toLowerCase();
    if (preferred) return preferred;
    try {
      const resp = await fetch("/api/settings", { cache: "no-store" });
      if (resp.ok) {
        const data = await resp.json();
        const fromSettings = String(data?.ui?.language || "").trim().toLowerCase();
        if (fromSettings) return fromSettings;
      }
    } catch (err) {}
    return localStorage.getItem(LANG_STORAGE_KEY) || DEFAULT_LANG;
  }

  document.addEventListener("DOMContentLoaded", async () => {
    bindLanguageSwitcher();
    const saved = await resolveInitialLanguage();
    loadLanguage(saved);
  });
})();

