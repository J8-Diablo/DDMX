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

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function formatFixtureOptionLabel(name, fx) {
    return `${fx?.meta?.model || fx?.info?.model || name} (${name})`;
  }

  function listFixtureChoices(currentFixture = "") {
    const current = String(currentFixture || "").trim();
    const allFixtures = (typeof fixtures === "object" && fixtures) ? fixtures : {};
    const options = [];

    for (const [name, fx] of Object.entries(allFixtures)) {
      if (fx?.error) continue;
      options.push({
        value: name,
        label: formatFixtureOptionLabel(name, fx),
      });
    }

    options.sort((left, right) => left.label.localeCompare(right.label));

    if (current && !options.some(option => option.value === current)) {
      options.unshift({
        value: current,
        label: `${current} (missing fixture)`,
      });
    }

    if (!options.length) {
      options.push({
        value: current,
        label: current || "No fixtures available",
      });
    }

    return options;
  }

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

  async function alertModal(title, text, icon = "warning") {
    if (isGuiApp) {
      await openGuiModal({
        title,
        confirmText: "OK",
        cancelText: "Annuler",
        showCancel: false,
        buildBody: (form) => appendGuiModalText(form, text || ""),
        onSubmit: () => true,
      });
      return true;
    }
    if (hasSwal()) {
      await window.Swal.fire({
        title,
        text,
        icon,
        confirmButtonText: "OK",
      });
      return true;
    }
    toast(`${title}: ${text}`, icon === "error" ? "error" : "warning");
    return true;
  }

  function createFixtureRemapOption(value, title, subtitle) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "fixture-remap-choice";
    btn.dataset.value = value;

    const titleEl = document.createElement("strong");
    titleEl.textContent = title;
    const subEl = document.createElement("span");
    subEl.textContent = subtitle;

    btn.appendChild(titleEl);
    btn.appendChild(subEl);
    return btn;
  }

  function renderFixtureChangeDecisionBody(form, config, state) {
    form.innerHTML = "";

    const summary = document.createElement("div");
    summary.className = "fixture-remap-summary";

    const callout = document.createElement("div");
    callout.className = "fixture-remap-callout";
    callout.textContent = config.warningText || "";
    summary.appendChild(callout);

    const choiceRow = document.createElement("div");
    choiceRow.className = "fixture-remap-choice-row";
    const remapBtn = createFixtureRemapOption(
      "remap",
      "Remap (Recommended)",
      config.remapText || "Find a safe free address and update cues automatically."
    );
    const keepBtn = createFixtureRemapOption(
      "keep",
      "Keep This Address",
      config.keepText || "Keep the current address. This may break some cue values or effects."
    );

    if (!config.remap?.available) {
      remapBtn.disabled = true;
    }

    const keepDetails = document.createElement("div");
    keepDetails.className = "fixture-remap-proposal";
    keepDetails.innerHTML = `<strong>Keep mode</strong><div>${escapeHtml(config.keepDetail || "")}</div>`;

    const remapDetails = document.createElement("div");
    remapDetails.className = "fixture-remap-proposal";
    remapDetails.innerHTML = config.remap?.available
      ? `<strong>Update address</strong><div>${escapeHtml(config.remap.summary || "")}</div>`
      : `<strong>Update address unavailable</strong><div>${escapeHtml(config.remap?.summary || "No free DMX slot was found.")}</div>`;

    const setStrategy = (value) => {
      state.strategy = value;
      remapBtn.classList.toggle("is-active", value === "remap");
      keepBtn.classList.toggle("is-active", value === "keep");
    };

    remapBtn.addEventListener("click", () => {
      if (!remapBtn.disabled) setStrategy("remap");
    });
    keepBtn.addEventListener("click", () => setStrategy("keep"));

    choiceRow.appendChild(remapBtn);
    choiceRow.appendChild(keepBtn);
    summary.appendChild(choiceRow);
    summary.appendChild(keepDetails);
    summary.appendChild(remapDetails);
    form.appendChild(summary);

    setStrategy(
      state.strategy === "keep" || !config.remap?.available
        ? "keep"
        : "remap"
    );
  }

  function renderFixtureRemapResolutionBody(form, config, state) {
    form.innerHTML = "";

    const summary = document.createElement("div");
    summary.className = "fixture-remap-summary";

    const callout = document.createElement("div");
    callout.className = "fixture-remap-callout";
    callout.textContent = config.warningText || "";
    summary.appendChild(callout);

    const remapDetails = document.createElement("div");
    remapDetails.className = "fixture-remap-section";

    const proposal = document.createElement("div");
    proposal.className = "fixture-remap-proposal";
    proposal.innerHTML = `<strong>New address</strong><div>${escapeHtml(config.remap?.summary || "")}</div>`;
    remapDetails.appendChild(proposal);

    if (Array.isArray(config.autoResolutions) && config.autoResolutions.length) {
      const section = document.createElement("div");
      section.className = "fixture-remap-section";

      const title = document.createElement("div");
      title.className = "fixture-remap-section-title";
      title.textContent = "Auto Resolution";
      section.appendChild(title);

      const list = document.createElement("div");
      list.className = "fixture-remap-detail-list";
      config.autoResolutions.forEach((item) => {
        const row = document.createElement("div");
        row.className = "fixture-remap-detail";
        row.innerHTML = `<div>${escapeHtml(item.label || "")}</div><div><code>${escapeHtml(item.detail || "")}</code></div>`;
        list.appendChild(row);
      });
      section.appendChild(list);
      remapDetails.appendChild(section);
    }

    if (Array.isArray(config.manualResolutions) && config.manualResolutions.length) {
      const section = document.createElement("div");
      section.className = "fixture-remap-section";

      const title = document.createElement("div");
      title.className = "fixture-remap-section-title";
      title.textContent = "Manual Resolution";
      section.appendChild(title);

      config.manualResolutions.forEach((item) => {
        const row = document.createElement("div");
        row.className = "fixture-remap-manual-row";

        const label = document.createElement("div");
        label.className = "fixture-remap-manual-label";
        label.textContent = item.label || item.sourceKey || "";
        row.appendChild(label);

        if (item.helpText) {
          const help = document.createElement("div");
          help.className = "fixture-remap-manual-help";
          help.textContent = item.helpText;
          row.appendChild(help);
        }

        const select = document.createElement("select");
        select.dataset.sourceKey = item.sourceKey || "";
        const empty = document.createElement("option");
        empty.value = "";
        empty.textContent = "Choose a target channel";
        select.appendChild(empty);

        (item.options || []).forEach((option) => {
          const opt = document.createElement("option");
          opt.value = option.value;
          opt.textContent = option.label;
          select.appendChild(opt);
        });

        if (state.manualMappings[item.sourceKey]) {
          select.value = state.manualMappings[item.sourceKey];
        }

        select.addEventListener("change", () => {
          state.manualMappings[item.sourceKey] = String(select.value || "");
        });
        row.appendChild(select);
        section.appendChild(row);
      });

      const note = document.createElement("div");
      note.className = "fixture-remap-footer-note";
      note.textContent = "All manual correspondences must be selected before saving the remap.";
      section.appendChild(note);

      remapDetails.appendChild(section);
    }

    summary.appendChild(remapDetails);
    form.appendChild(summary);
  }

  function renderOperationStatusBody(form, config) {
    form.innerHTML = "";

    const summary = document.createElement("div");
    summary.className = "status-summary";

    if (config?.message) {
      const msg = document.createElement("div");
      msg.className = "fixture-remap-callout";
      msg.textContent = config.message;
      summary.appendChild(msg);
    }

    if (config?.hero) {
      const hero = document.createElement("div");
      hero.className = "status-hero";
      hero.textContent = config.hero;
      summary.appendChild(hero);
    }

    if (config?.details) {
      const details = document.createElement("div");
      details.className = "status-detail";
      details.textContent = config.details;
      summary.appendChild(details);
    }

    form.appendChild(summary);
  }

  async function fixtureChangeDecisionModal(config) {
    const state = {
      strategy: config?.defaultStrategy === "keep" ? "keep" : "remap",
    };

    return await openGuiModal({
      title: config?.title || "Fixture Change Warning",
      confirmText: "Continue",
      cancelText: "Cancel",
      showCancel: true,
      modalClass: "dmx-modal-warning",
      buildBody: (form) => renderFixtureChangeDecisionBody(form, config || {}, state),
      onSubmit: () => ({
        strategy: state.strategy,
      }),
    });
  }

  async function fixtureRemapModal(config) {
    const state = {
      manualMappings: {},
    };

    return await openGuiModal({
      title: config?.title || "Resolve Fixture Remap",
      confirmText: "Apply Remap",
      cancelText: "Cancel",
      showCancel: true,
      modalClass: "dmx-modal-warning",
      buildBody: (form) => renderFixtureRemapResolutionBody(form, config || {}, state),
      onSubmit: () => {
        const unresolved = Array.isArray(config?.manualResolutions) ? config.manualResolutions : [];
        for (const item of unresolved) {
          if (!String(state.manualMappings[item.sourceKey] || "").trim()) {
            toast("Complete every manual correspondence before saving the remap.", "warning");
            return false;
          }
        }
        return {
          manualMappings: { ...state.manualMappings },
        };
      },
    });
  }

  // A cue list whose blocks overlap or leave holes cannot be played as a cue
  // list: three ways out, and the operator picks. Returns "classic",
  // "timeline", or null when cancelled.
  async function cueTimeModelModal(config) {
    const state = { choice: "classic" };
    const name = String(config?.name || "");
    const lines = Array.isArray(config?.lines) ? config.lines : [];
    const tr = (key, fallback) => (typeof window.t === "function" ? window.t(key, fallback) : fallback);

    return await openGuiModal({
      title: tr("cues.timeIssuesTitle", "This cue list does not fit the Cue list mode"),
      confirmText: tr("cues.timeIssuesOpenClassic", "Open as Cue list"),
      cancelText: tr("common.cancel", "Cancel"),
      showCancel: true,
      modalClass: "dmx-modal-warning",
      buildBody: (form) => {
        const intro = document.createElement("p");
        intro.className = "dmx-modal-text";
        intro.textContent = tr(
          "cues.timeIssuesIntro",
          "Some passages were detected as incompatible and playback may not behave as authored.",
        ) + (name ? ` (${name})` : "");
        form.appendChild(intro);

        if (lines.length) {
          const list = document.createElement("ul");
          list.className = "dmx-modal-list";
          for (const line of lines) {
            const li = document.createElement("li");
            li.textContent = line;
            list.appendChild(li);
          }
          form.appendChild(list);
        }

        const risk = document.createElement("p");
        risk.className = "dmx-modal-text muted";
        risk.textContent = tr(
          "cues.timeIssuesRisk",
          "A cue list plays one cue at a time with no gaps: overlaps get serialised and holes keep the previous look instead of going dark.",
        );
        form.appendChild(risk);

        // The third way out: same dialog, other mode.
        const row = document.createElement("div");
        row.className = "dmx-modal-actions-inline";
        const timelineBtn = document.createElement("button");
        timelineBtn.type = "button";
        timelineBtn.className = "secondary";
        timelineBtn.textContent = tr("cues.timeIssuesOpenTimeline", "Open in Timeline mode");
        timelineBtn.addEventListener("click", () => {
          state.choice = "timeline";
          if (typeof form.requestSubmit === "function") form.requestSubmit();
          else form.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
        });
        row.appendChild(timelineBtn);
        form.appendChild(row);
      },
      onSubmit: () => state.choice,
    });
  }

  async function operationStatusModal(config) {
    const status = String(config?.status || "success").toLowerCase();
    const modalClass = status === "error" ? "dmx-modal-error" : "dmx-modal-success";

    if (isGuiApp) {
      await openGuiModal({
        title: config?.title || (status === "error" ? "Operation Failed" : "Operation Complete"),
        confirmText: "OK",
        cancelText: "Cancel",
        showCancel: false,
        modalClass,
        buildBody: (form) => renderOperationStatusBody(form, config || {}),
        onSubmit: () => true,
      });
      return true;
    }
    if (hasSwal()) {
      await window.Swal.fire({
        title: config?.title || (status === "error" ? "Operation Failed" : "Operation Complete"),
        html: `
          <div style="display:grid;gap:10px;text-align:left">
            ${config?.message ? `<div>${escapeHtml(config.message)}</div>` : ""}
            ${config?.hero ? `<div style="font-size:22px;font-weight:800;color:${status === "error" ? "#fca5a5" : "#86efac"}">${escapeHtml(config.hero)}</div>` : ""}
            ${config?.details ? `<div>${escapeHtml(config.details)}</div>` : ""}
          </div>
        `,
        icon: status === "error" ? "error" : "success",
        confirmButtonText: "OK",
      });
      return true;
    }
    toast(
      `${config?.title || (status === "error" ? "Operation failed" : "Operation complete")}: ${config?.hero || config?.message || ""}`,
      status === "error" ? "error" : "success"
    );
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
    const fixtureChoices = listFixtureChoices(dev.fixture);
    if (isGuiApp) {
      return await openGuiModal({
        title: `Edit Device ${dev.id}`,
        confirmText: "Save",
        cancelText: "Cancel",
        showCancel: true,
        buildBody: (form) => {
          const fixtureSelect = document.createElement("select");
          fixtureSelect.dataset.role = "fixture";
          fixtureChoices.forEach((option) => {
            const opt = document.createElement("option");
            opt.value = option.value;
            opt.textContent = option.label;
            fixtureSelect.appendChild(opt);
          });
          fixtureSelect.value = fixtureChoices.some(option => option.value === dev.fixture)
            ? dev.fixture
            : fixtureChoices[0]?.value || "";
          appendGuiModalField(form, "Fixture", fixtureSelect);

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
          const fixture = form.querySelector('[data-role="fixture"]');
          const cname = form.querySelector('[data-role="cname"]');
          const uni = form.querySelector('[data-role="universe"]');
          const addr = form.querySelector('[data-role="address"]');
          return {
            ...dev,
            fixture: String(fixture?.value ?? dev.fixture ?? "").trim(),
            cname: String(cname?.value ?? "").trim(),
            universe: parseInt(uni?.value, 10) || 0,
            address: parseInt(addr?.value, 10) || 0,
          };
        },
      });
    }
    if (hasSwal()) {
      const fixtureOptionsHtml = fixtureChoices.map((option) => {
        const selected = option.value === dev.fixture ? " selected" : "";
        return `<option value="${escapeHtml(option.value)}"${selected}>${escapeHtml(option.label)}</option>`;
      }).join("");
      const res = await window.Swal.fire({
        title: `Edit Device ${dev.id}`,
        html: `
          <div style="display:grid;gap:8px;text-align:left">
            <label style="font-size:12px;opacity:.7">Fixture</label>
            <select id="sw-fixture" class="swal2-input">${fixtureOptionsHtml}</select>

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
          const fixture = document.getElementById("sw-fixture").value.trim();
          const cname = document.getElementById("sw-cname").value.trim();
          const universe = parseInt(document.getElementById("sw-uni").value, 10) || 0;
          const address = parseInt(document.getElementById("sw-addr").value, 10) || 0;
          return { ...dev, fixture, cname, universe, address };
        }
      });
      return res.isConfirmed ? res.value : null;
    }
    toast("(device edit indisponible) SweetAlert2 non chargé.", "warning");
    return null;
  }

  // ---------------------------------------------------------------------------
  // Bulk add: one modal for "how many devices, patched where, laid out how".
  // Pure DOM (openGuiModal), so it also works with no CDN reachable.
  // ---------------------------------------------------------------------------

  function readBulkAddForm(form) {
    const num = (role, fallback) => {
      const el = form.querySelector(`[data-role="${role}"]`);
      const raw = String(el?.value ?? "").trim();
      if (!raw) return fallback;
      const parsed = parseInt(raw, 10);
      return Number.isFinite(parsed) ? parsed : fallback;
    };
    const addrEl = form.querySelector('[data-role="address"]');
    const addrRaw = String(addrEl?.value ?? "").trim();
    return {
      count: Math.max(1, Math.min(512, num("count", 1))),
      prefix: String(form.querySelector('[data-role="prefix"]')?.value ?? "").trim(),
      universe: Math.max(0, num("universe", 0)),
      // Empty address = auto (first free block).
      address: addrRaw === "" ? null : Math.max(0, Math.min(511, parseInt(addrRaw, 10) || 0)),
      overflow: Boolean(form.querySelector('[data-role="overflow"]')?.checked),
      columns: Math.max(1, Math.min(64, num("columns", 8))),
      spacing: Math.max(8, Math.min(400, num("spacing", 40))),
    };
  }

  async function bulkAddDeviceModal(config) {
    const {
      title = "Add devices",
      confirmText = "Add",
      cancelText = "Cancel",
      headline = "",
      labels = {},
      defaults = {},
      onPreview = null,
    } = config || {};

    const L = {
      count: "Number of devices",
      prefix: "Name prefix",
      universe: "Universe",
      address: "Start address (empty = auto)",
      overflow: "Continue into the next universe when full",
      columns: "Devices per row",
      spacing: "Spacing (px)",
      ...labels,
    };

    return await openGuiModal({
      title,
      confirmText,
      cancelText,
      showCancel: true,
      modalClass: "dmx-modal-bulk-add",
      initialFocusSelector: '[data-role="count"]',
      buildBody: (form) => {
        if (headline) appendGuiModalText(form, headline);

        const count = document.createElement("input");
        count.type = "number";
        count.min = "1";
        count.max = "512";
        count.value = String(defaults.count ?? 1);
        count.dataset.role = "count";
        appendGuiModalField(form, L.count, count);

        const prefix = document.createElement("input");
        prefix.type = "text";
        prefix.value = String(defaults.prefix ?? "Device");
        prefix.dataset.role = "prefix";
        appendGuiModalField(form, L.prefix, prefix);

        const universe = document.createElement("input");
        universe.type = "number";
        universe.min = "0";
        universe.value = String(defaults.universe ?? 0);
        universe.dataset.role = "universe";
        appendGuiModalField(form, L.universe, universe);

        const address = document.createElement("input");
        address.type = "number";
        address.min = "0";
        address.max = "511";
        address.placeholder = "auto";
        if (defaults.address != null) address.value = String(defaults.address);
        address.dataset.role = "address";
        appendGuiModalField(form, L.address, address);

        const overflowWrap = document.createElement("label");
        overflowWrap.style.display = "flex";
        overflowWrap.style.alignItems = "center";
        overflowWrap.style.gap = "8px";
        overflowWrap.style.marginBottom = "12px";
        overflowWrap.style.fontSize = "12px";
        overflowWrap.style.opacity = "0.85";
        const overflow = document.createElement("input");
        overflow.type = "checkbox";
        overflow.checked = defaults.overflow !== false;
        overflow.dataset.role = "overflow";
        overflow.style.width = "auto";
        overflowWrap.appendChild(overflow);
        const overflowText = document.createElement("span");
        overflowText.textContent = L.overflow;
        overflowWrap.appendChild(overflowText);
        form.appendChild(overflowWrap);

        const columns = document.createElement("input");
        columns.type = "number";
        columns.min = "1";
        columns.max = "64";
        columns.value = String(defaults.columns ?? 8);
        columns.dataset.role = "columns";
        appendGuiModalField(form, L.columns, columns);

        const spacing = document.createElement("input");
        spacing.type = "number";
        spacing.min = "8";
        spacing.max = "400";
        spacing.value = String(defaults.spacing ?? 40);
        spacing.dataset.role = "spacing";
        appendGuiModalField(form, L.spacing, spacing);

        if (typeof onPreview === "function") {
          const preview = document.createElement("div");
          preview.className = "dmx-modal-preview";
          preview.dataset.role = "preview";
          const refresh = () => {
            let text = "";
            try {
              text = String(onPreview(readBulkAddForm(form)) ?? "");
            } catch (err) {
              text = "";
            }
            preview.textContent = text;
          };
          form.appendChild(preview);
          form.addEventListener("input", refresh);
          form.addEventListener("change", refresh);
          refresh();
        }
      },
      onSubmit: (form) => readBulkAddForm(form),
    });
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

  return {
    toast,
    confirmModal,
    alertModal,
    promptModal,
    deviceEditModal,
    bulkAddDeviceModal,
    fixtureChangeDecisionModal,
    fixtureRemapModal,
    cueTimeModelModal,
    operationStatusModal,
    htmlModal
  };
})();

