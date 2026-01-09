// static/ui.js
// Safe UI layer: uses Notyf + SweetAlert2 if present,
// otherwise falls back to in-page toasts and non-blocking defaults.

window.ui = (() => {
  let notyf = null;
  const t = (key, fallback) =>
    (typeof window.t === "function" ? window.t(key, fallback) : (fallback || key));
  const tfmt = (key, fallback, params) => {
    if (typeof window.tfmt === "function") return window.tfmt(key, fallback, params);
    const template = t(key, fallback);
    if (!params) return template;
    return template.replace(/\{(\w+)\}/g, (_, k) => (params[k] == null ? "" : String(params[k])));
  };

  try {
    if (window.Notyf) {
      notyf = new window.Notyf({
        duration: 2200,
        position: { x: "center", y: "top" },
        dismissible: true,
        ripple: false
      });
    }
  } catch (e) {
    notyf = null;
  }

  const MAX_TOASTS = 2;

  function ensureToastContainer() {
    let c = document.getElementById("toast-container");
    if (c) return c;
    c = document.createElement("div");
    c.id = "toast-container";
    c.style.position = "fixed";
    c.style.left = "50%";
    c.style.transform = "translateX(-50%)";
    c.style.top = "12px";
    c.style.display = "flex";
    c.style.flexDirection = "column";
    c.style.alignItems = "center";
    c.style.gap = "8px";
    c.style.zIndex = 99999;
    document.body.appendChild(c);
    return c;
  }

  function fallbackToast(message, type = "success") {
    if (!message) return;
    const c = ensureToastContainer();

    // Remove oldest toasts if we have too many
    while (c.children.length >= MAX_TOASTS) {
      c.firstChild.remove();
    }

    const el = document.createElement("div");
    el.textContent = message;
    el.style.padding = "8px 14px";
    el.style.borderRadius = "8px";
    el.style.font = "13px system-ui";
    el.style.color = "#fff";
    el.style.whiteSpace = "nowrap";
    el.style.background =
      type === "error" ? "#dc2626" :
      type === "warning" ? "#f59e0b" :
      type === "info" ? "#2563eb" :
      "#16a34a";
    el.style.boxShadow = "0 6px 18px rgba(0,0,0,.35)";
    c.appendChild(el);
    setTimeout(() => el.remove(), 2400);
  }

  function toast(message, type = "success") {
    if (!message) return;

    if (notyf) {
      try {
        if (type === "error") notyf.error(message);
        else if (type === "warning") notyf.open({ type: "warning", message });
        else if (type === "info") notyf.open({ type: "info", message });
        else notyf.success(message);
        return;
      } catch (e) {
        notyf = null;
      }
    }
    fallbackToast(message, type);
  }

  const hasSwal = () => typeof window.Swal !== "undefined" && window.Swal?.fire;

  async function confirmModal(title, text, icon = "warning") {
    if (hasSwal()) {
      const res = await window.Swal.fire({
        title,
        text,
        icon,
        showCancelButton: true,
        confirmButtonText: t("common.ok", "OK"),
        cancelButtonText: t("common.cancel", "Cancel"),
        reverseButtons: true,
        focusCancel: true
      });
      return !!res.isConfirmed;
    }
    const msg = [title, text].filter(Boolean).join("\n\n");
    return window.confirm(msg);
  }

  async function promptModal(title, inputValue = "", placeholder = "") {
    if (hasSwal()) {
      const res = await window.Swal.fire({
        title,
        input: "text",
        inputValue,
        inputPlaceholder: placeholder,
        showCancelButton: true,
        confirmButtonText: t("common.ok", "OK"),
        cancelButtonText: t("common.cancel", "Cancel")
      });
      return res.isConfirmed ? (res.value ?? "") : null;
    }
    const hint = placeholder ? `\n${placeholder}` : "";
    const res = window.prompt(`${title}${hint}`, inputValue);
    return res === null ? null : res;
  }

  async function deviceEditModal(dev) {
    if (hasSwal()) {
      const modalTitle = tfmt("ui.deviceEditTitle", "Edit Device {id}", { id: dev.id });
      const res = await window.Swal.fire({
        title: modalTitle,
        html: `
          <div style="display:grid;gap:8px;text-align:left">
            <label style="font-size:12px;opacity:.7">${t("ui.deviceEditCnameLabel", "CName")}</label>
            <input id="sw-cname" class="swal2-input" value="${dev.cname ?? ""}" placeholder="${t("ui.deviceEditCnamePlaceholder", "Device name")}">

            <label style="font-size:12px;opacity:.7">${t("ui.deviceEditUniverseLabel", "Universe")}</label>
            <input id="sw-uni" class="swal2-input" type="number" min="0" value="${dev.universe ?? 0}">

            <label style="font-size:12px;opacity:.7">${t("ui.deviceEditAddressLabel", "Address")}</label>
            <input id="sw-addr" class="swal2-input" type="number" min="0" max="511" value="${dev.address ?? 0}">
          </div>
        `,
        showCancelButton: true,
        confirmButtonText: t("common.save", "Save"),
        cancelButtonText: t("common.cancel", "Cancel"),
        preConfirm: () => {
          const cname = document.getElementById("sw-cname").value.trim();
          const universe = parseInt(document.getElementById("sw-uni").value, 10) || 0;
          const address = parseInt(document.getElementById("sw-addr").value, 10) || 0;
          return { ...dev, cname, universe, address };
        }
      });
      return res.isConfirmed ? res.value : null;
    }
    const cname = window.prompt(t("ui.deviceEditCnameLabel", "CName"), dev.cname ?? "");
    if (cname === null) return null;
    const universeRaw = window.prompt(
      t("ui.deviceEditUniverseLabel", "Universe"),
      String(dev.universe ?? 0)
    );
    if (universeRaw === null) return null;
    const addressRaw = window.prompt(
      t("ui.deviceEditAddressLabel", "Address"),
      String(dev.address ?? 0)
    );
    if (addressRaw === null) return null;
    return {
      ...dev,
      cname: cname.trim(),
      universe: parseInt(universeRaw, 10) || 0,
      address: parseInt(addressRaw, 10) || 0
    };
  }

  return { toast, confirmModal, promptModal, deviceEditModal };
})();

