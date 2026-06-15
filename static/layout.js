// static/layout.js
// Layout v2: tabbed bottom panel (Cues / Sync Video / Auto-Light) + resizable
// splitters between RIG | Controller (width) and top | bottom (height).
// Pure DOM/CSS manipulation of existing elements — no template dependency
// beyond this script tag.

(function () {
  const LS_TAB = "dmx_bottom_tab";
  const LS_RIGW = "dmx_rig_w";
  const LS_TOPH = "dmx_top_h";

  const TAB_LABELS = { cues: "Cues", sync: "Sync Video", autolight: "Auto-Light" };

  function _t(key, fallback) { return (typeof window.t === "function") ? window.t(key, fallback) : fallback; }

  // Tag by exclusion: Sync Video and Auto-Light are their own tabs; everything
  // else in the cues panel (cue files, toolbar, loop actions, table, timeline,
  // props…) belongs to the "cues" tab. The panel header + tab bar stay visible.
  function tagGroups(panel) {
    const sync = panel.querySelector("#sync-video-section");
    if (sync) sync.setAttribute("data-bgroup", "sync");
    const al = panel.querySelector("#autolight-section");
    if (al) al.setAttribute("data-bgroup", "autolight");
    Array.from(panel.children).forEach((ch) => {
      if (ch.classList.contains("panel-header") || ch.classList.contains("cues-tabbar")) return;
      if (ch.getAttribute("data-bgroup")) return;
      ch.setAttribute("data-bgroup", "cues");
    });
  }

  function setBottomTab(id) {
    const panel = document.querySelector(".cues-panel");
    if (!panel) return;
    if (!TAB_LABELS[id]) id = "cues";
    panel.dataset.tab = id;
    try { localStorage.setItem(LS_TAB, id); } catch (e) {}
    panel.querySelectorAll(".cues-tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === id));
    // Let the active view recompute (timeline needs a render when shown).
    if (id === "cues" && typeof window.renderTimelineEditor === "function") {
      try { window.renderTimelineEditor(); } catch (e) {}
    }
  }
  window.setBottomTab = setBottomTab;

  function buildBottomTabs() {
    const panel = document.querySelector(".cues-panel");
    if (!panel || panel.dataset.tabsBuilt === "1") return;
    panel.dataset.tabsBuilt = "1";
    tagGroups(panel);

    const bar = document.createElement("div");
    bar.className = "cues-tabbar";
    for (const id of Object.keys(TAB_LABELS)) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "cues-tab-btn";
      b.dataset.tab = id;
      const key = "bottomTab." + id;
      b.textContent = _t(key, TAB_LABELS[id]);
      b.setAttribute("data-i18n", key);
      b.addEventListener("click", () => setBottomTab(id));
      bar.appendChild(b);
    }
    panel.insertBefore(bar, panel.firstChild);

    let initial = "cues";
    try { initial = localStorage.getItem(LS_TAB) || "cues"; } catch (e) {}
    setBottomTab(initial);
  }

  function buildSplitters() {
    const grid = document.querySelector(".main-grid");
    if (!grid || grid.dataset.splitBuilt === "1") return;
    grid.dataset.splitBuilt = "1";

    const v = document.createElement("div");
    v.className = "tl-vsplit";
    v.title = "Drag to resize Rig / Controller";
    const h = document.createElement("div");
    h.className = "tl-hsplit";
    h.title = "Drag to resize top / bottom";
    grid.appendChild(v);
    grid.appendChild(h);

    try {
      const rw = localStorage.getItem(LS_RIGW); if (rw) grid.style.setProperty("--rig-w", rw);
      const th = localStorage.getItem(LS_TOPH); if (th) grid.style.setProperty("--top-h", th);
    } catch (e) {}

    let mode = null;
    const onMove = (e) => {
      if (!mode) return;
      const r = grid.getBoundingClientRect();
      if (mode === "v") {
        const x = Math.max(220, Math.min(r.width - 280, e.clientX - r.left));
        grid.style.setProperty("--rig-w", x + "px");
      } else {
        const y = Math.max(160, Math.min(r.height - 150, e.clientY - r.top));
        grid.style.setProperty("--top-h", y + "px");
      }
      if (typeof window.updateRigCanvasSize === "function") window.updateRigCanvasSize();
      if (typeof window.drawRig === "function") window.drawRig();
    };
    const onUp = () => {
      if (!mode) return;
      try {
        if (mode === "v") localStorage.setItem(LS_RIGW, grid.style.getPropertyValue("--rig-w"));
        else localStorage.setItem(LS_TOPH, grid.style.getPropertyValue("--top-h"));
      } catch (e) {}
      mode = null;
      document.body.style.userSelect = "";
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      if (typeof window.renderTimelineEditor === "function") { try { window.renderTimelineEditor(); } catch (e) {} }
    };
    const start = (m) => (e) => {
      e.preventDefault();
      mode = m;
      document.body.style.userSelect = "none";
      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    };
    v.addEventListener("mousedown", start("v"));
    h.addEventListener("mousedown", start("h"));
  }

  // Merge the cue file row + loop actions + playback/edit toolbar into a single
  // horizontal bar to reclaim vertical space.
  function mergeCueBar() {
    const panel = document.querySelector(".cues-panel");
    if (!panel || panel.querySelector(".cue-bar")) return;
    const files = panel.querySelector(".cue-files");
    const loop = panel.querySelector(".cue-loop-actions");
    const toolbar = panel.querySelector(".cue-toolbar");
    if (!files) return;
    const bar = document.createElement("div");
    bar.className = "cue-bar";
    panel.insertBefore(bar, files);
    [files, loop, toolbar].forEach((el) => { if (el) bar.appendChild(el); });
  }

  document.addEventListener("DOMContentLoaded", () => {
    mergeCueBar();
    buildBottomTabs();
    buildSplitters();
  });
})();
