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

  async function loadLanguage(lang) {
    const safeLang = lang || DEFAULT_LANG;
    try {
      const resp = await fetch(`/static/lang/${safeLang}.json?t=${Date.now()}`);
      if (!resp.ok) throw new Error(resp.statusText);
      const json = await resp.json();
      translations = json || {};
      currentLang = safeLang;
      localStorage.setItem(LANG_STORAGE_KEY, currentLang);
      document.documentElement.lang = currentLang;
      applyTranslations();
    } catch (err) {
      console.warn("[i18n] load failed, fallback to en:", err);
      if (safeLang !== DEFAULT_LANG) {
        return loadLanguage(DEFAULT_LANG);
      }
    }
  }

  function applyTranslations() {
    document.querySelectorAll("[data-i18n]").forEach((el) => {
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
  }

  function bindLanguageSwitcher() {
    const select = document.getElementById("lang-select");
    if (!select) return;
    select.addEventListener("change", (e) => {
      const lang = e.target.value || DEFAULT_LANG;
      loadLanguage(lang);
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindLanguageSwitcher();
    const saved = localStorage.getItem(LANG_STORAGE_KEY) || DEFAULT_LANG;
    loadLanguage(saved);
  });
})();

