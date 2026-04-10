// static/ui.js
// Safe UI layer: uses Notyf + SweetAlert2 if present,
// otherwise falls back to in-page toasts and non-blocking defaults.

window.ui = (() => {
  const isGuiApp = (() => {
    try {
      return new URLSearchParams(window.location.search).get("gui") === "1";
    } catch (e) {
      return false;
    }
  })();

  let notyf = null;

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

  function buildGuiModalShell(title, confirmText = "OK", cancelText = "Annuler", showCancel = true, modalClass = "") {
    const overlay = document.createElement("div");
    overlay.className = "dmx-modal-overlay";

    const modal = document.createElement("div");
    modal.className = "dmx-modal";
    if (modalClass) {
      modal.classList.add(...String(modalClass).split(/\s+/).filter(Boolean));
    }

    const header = document.createElement("div");
    header.className = "dmx-modal-header";

    const titleEl = document.createElement("div");
    titleEl.className = "dmx-modal-title";
    titleEl.textContent = title || "";

    const closeBtn = document.createElement("button");
    closeBtn.className = "dmx-modal-close";
    closeBtn.type = "button";
    closeBtn.setAttribute("aria-label", "Close");
    closeBtn.textContent = "×";

    header.appendChild(titleEl);
    header.appendChild(closeBtn);

    const form = document.createElement("form");
    form.className = "dmx-modal-body";
    form.id = `dmx-modal-form-${Date.now()}-${Math.random().toString(16).slice(2)}`;

    const actions = document.createElement("div");
    actions.className = "dmx-modal-actions";
    const actionsRight = document.createElement("div");
    actionsRight.className = "dmx-modal-actions-right";

    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "secondary dmx-modal-cancel";
    cancelBtn.textContent = cancelText;
    cancelBtn.style.display = showCancel ? "" : "none";

    const confirmBtn = document.createElement("button");
    confirmBtn.type = "submit";
    confirmBtn.className = "primary dmx-modal-save";
    confirmBtn.textContent = confirmText;
    confirmBtn.setAttribute("form", form.id);

    actionsRight.appendChild(cancelBtn);
    actionsRight.appendChild(confirmBtn);
    actions.appendChild(actionsRight);

    modal.appendChild(header);
    modal.appendChild(form);
    modal.appendChild(actions);
    overlay.appendChild(modal);

    return { overlay, modal, form, closeBtn, cancelBtn, confirmBtn };
  }

  function openGuiModal({
    title,
    confirmText = "OK",
    cancelText = "Annuler",
    showCancel = true,
    buildBody,
    onSubmit,
    modalClass = "",
    initialFocusSelector = "",
  }) {
    return new Promise((resolve) => {
      document.querySelectorAll(".dmx-modal-overlay").forEach((node) => node.remove());

      const { overlay, modal, form, closeBtn, cancelBtn } =
        buildGuiModalShell(title, confirmText, cancelText, showCancel, modalClass);

      const close = (result) => {
        document.removeEventListener("keydown", onKeyDown, true);
        overlay.remove();
        resolve(result);
      };

      const onKeyDown = (ev) => {
        if (ev.key === "Escape") {
          ev.preventDefault();
          close(null);
        }
      };

      document.addEventListener("keydown", onKeyDown, true);
      overlay.addEventListener("click", (ev) => {
        if (ev.target === overlay) close(null);
      });
      closeBtn.addEventListener("click", () => close(null));
      cancelBtn.addEventListener("click", () => close(null));

      if (typeof buildBody === "function") {
        buildBody(form);
      }

      form.addEventListener("submit", (ev) => {
        ev.preventDefault();
        const result = typeof onSubmit === "function" ? onSubmit(form) : true;
        if (result === false) return;
        close(result);
      });

      document.body.appendChild(overlay);
      const focusTarget = (
        (typeof initialFocusSelector === "string" && initialFocusSelector.trim())
          ? modal.querySelector(initialFocusSelector)
          : null
      ) || form.querySelector("input, textarea, select, button");
      if (focusTarget && typeof focusTarget.focus === "function") {
        setTimeout(() => {
          form.scrollTop = 0;
          focusTarget.focus({ preventScroll: true });
        }, 0);
      }
    });
  }

  function appendGuiModalText(form, text) {
    const p = document.createElement("div");
    p.style.whiteSpace = "pre-wrap";
    p.style.lineHeight = "1.5";
    p.style.paddingBottom = "8px";
    p.textContent = text || "";
    form.appendChild(p);
  }

  function appendGuiModalField(form, labelText, input) {
    const wrap = document.createElement("label");
    wrap.style.display = "grid";
    wrap.style.gap = "6px";
    wrap.style.marginBottom = "12px";

    const label = document.createElement("span");
    label.style.fontSize = "12px";
    label.style.opacity = "0.8";
    label.textContent = labelText;

    input.style.width = "100%";
    input.style.padding = "8px 10px";
    input.style.borderRadius = "8px";
    input.style.border = "1px solid rgba(148, 163, 184, 0.35)";
    input.style.background = "rgba(15, 23, 42, 0.75)";
    input.style.color = "var(--text)";
    input.style.boxSizing = "border-box";

    wrap.appendChild(label);
    wrap.appendChild(input);
    form.appendChild(wrap);
    return input;
  }

  async function confirmModal(title, text, icon = "warning") {
    if (isGuiApp) {
      return !!(await openGuiModal({
        title,
        confirmText: "OK",
        cancelText: "Annuler",
        showCancel: true,
        buildBody: (form) => appendGuiModalText(form, text || ""),
        onSubmit: () => true,
      }));
    }
    if (hasSwal()) {
      const res = await window.Swal.fire({
        title,
        text,
        icon,
        showCancelButton: true,
        confirmButtonText: "OK",
        cancelButtonText: "Annuler",
        reverseButtons: true,
        focusCancel: true
      });
      return !!res.isConfirmed;
    }
    toast(`(confirm indisponible) ${title}: ${text}`, "warning");
    return true;
  }

  async function promptModal(title, inputValue = "", placeholder = "") {
    if (isGuiApp) {
      return await openGuiModal({
        title,
        confirmText: "OK",
        cancelText: "Annuler",
        showCancel: true,
        buildBody: (form) => {
          const input = document.createElement("input");
          input.type = "text";
          input.value = inputValue ?? "";
          input.placeholder = placeholder || "";
          input.dataset.role = "modal-input";
          appendGuiModalField(form, title, input);
        },
        onSubmit: (form) => {
          const input = form.querySelector('[data-role="modal-input"]');
          return input ? String(input.value ?? "") : "";
        },
      });
    }
    if (hasSwal()) {
      const res = await window.Swal.fire({
        title,
        input: "text",
        inputValue,
        inputPlaceholder: placeholder,
        showCancelButton: true,
        confirmButtonText: "OK",
        cancelButtonText: "Annuler"
      });
      return res.isConfirmed ? (res.value ?? "") : null;
    }
    toast(`(input indisponible) ${title} → action annulée`, "warning");
    return null;
  }

  async function deviceEditModal(dev) {
    if (isGuiApp) {
      return await openGuiModal({
        title: `Edit Device ${dev.id}`,
        confirmText: "Save",
        cancelText: "Cancel",
        showCancel: true,
        buildBody: (form) => {
          const cname = document.createElement("input");
          cname.type = "text";
          cname.value = dev.cname ?? "";
          cname.dataset.role = "cname";
          appendGuiModalField(form, "CName", cname);

          const uni = document.createElement("input");
          uni.type = "number";
          uni.min = "0";
          uni.value = String(dev.universe ?? 0);
          uni.dataset.role = "universe";
          appendGuiModalField(form, "Universe", uni);

          const addr = document.createElement("input");
          addr.type = "number";
          addr.min = "0";
          addr.max = "511";
          addr.value = String(dev.address ?? 0);
          addr.dataset.role = "address";
          appendGuiModalField(form, "Address", addr);
        },
        onSubmit: (form) => {
          const cname = form.querySelector('[data-role="cname"]');
          const uni = form.querySelector('[data-role="universe"]');
          const addr = form.querySelector('[data-role="address"]');
          return {
            ...dev,
            cname: String(cname?.value ?? "").trim(),
            universe: parseInt(uni?.value, 10) || 0,
            address: parseInt(addr?.value, 10) || 0,
          };
        },
      });
    }
    if (hasSwal()) {
      const res = await window.Swal.fire({
        title: `Edit Device ${dev.id}`,
        html: `
          <div style="display:grid;gap:8px;text-align:left">
            <label style="font-size:12px;opacity:.7">CName</label>
            <input id="sw-cname" class="swal2-input" value="${dev.cname ?? ""}" placeholder="Device name">

            <label style="font-size:12px;opacity:.7">Universe</label>
            <input id="sw-uni" class="swal2-input" type="number" min="0" value="${dev.universe ?? 0}">

            <label style="font-size:12px;opacity:.7">Address</label>
            <input id="sw-addr" class="swal2-input" type="number" min="0" max="511" value="${dev.address ?? 0}">
          </div>
        `,
        showCancelButton: true,
        confirmButtonText: "Save",
        cancelButtonText: "Cancel",
        preConfirm: () => {
          const cname = document.getElementById("sw-cname").value.trim();
          const universe = parseInt(document.getElementById("sw-uni").value, 10) || 0;
          const address = parseInt(document.getElementById("sw-addr").value, 10) || 0;
          return { ...dev, cname, universe, address };
        }
      });
      return res.isConfirmed ? res.value : null;
    }
    toast("(device edit indisponible) SweetAlert2 non chargé.", "warning");
    return null;
  }

  async function htmlModal({
    title,
    html = "",
    confirmText = "Close",
    cancelText = "Cancel",
    showCancel = false,
    modalClass = "",
    onSubmit = null,
    afterOpen = null,
    initialFocusSelector = "",
  }) {
    return await openGuiModal({
      title,
      confirmText,
      cancelText,
      showCancel,
      modalClass,
      initialFocusSelector,
      buildBody: (form) => {
        form.innerHTML = html || "";
        if (typeof afterOpen === "function") {
          afterOpen(form);
        }
      },
      onSubmit: (form) => (typeof onSubmit === "function" ? onSubmit(form) : true),
    });
  }

  return { toast, confirmModal, promptModal, deviceEditModal, htmlModal };
})();

