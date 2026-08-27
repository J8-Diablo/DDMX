"""Tests for the rig editing additions: bulk add, copy/paste, keyboard delete.

The DMX allocator (planDeviceAddresses) is the part that can silently corrupt a
patch, so it is exercised for real: its source is lifted out of rig.js and run
in node against a synthetic rig.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_NODE = shutil.which("node")
requires_node = pytest.mark.skipif(_NODE is None, reason="node not available on PATH")


def _read(rel: str) -> str:
    with open(os.path.join(_REPO_ROOT, rel), "r", encoding="utf-8") as fh:
        return fh.read()


# -----------------------------------------------------------------------------
# DMX allocator, run for real in node
# -----------------------------------------------------------------------------

def _allocator_source() -> str:
    """The batch-patching helpers, lifted verbatim from rig.js."""
    src = _read("static/rig.js")
    start = src.index("const MAX_PLAN_UNIVERSE")
    end = src.index("function findAutoRemapSlot")
    block = src[start:end]
    assert "function planDeviceAddresses" in block
    assert "function firstFreeSlotIn" in block
    return block


_HARNESS = """
// Browser globals the lifted code expects.
var rigDevices = {};
var fixtures = {};
function getFixtureFootprint(fi) {
  return Math.max(1, parseInt((fi && (fi.footprint != null ? fi.footprint : fi.addr_count)) ?? 1, 10) || 1);
}
%(source)s

const out = [];
function check(name, cond, detail) {
  out.push({ name, ok: !!cond, detail: detail === undefined ? null : detail });
}
function setRig(devices, footprints) {
  rigDevices = {};
  fixtures = {};
  for (const [name, fp] of Object.entries(footprints)) fixtures[name] = { footprint: fp };
  devices.forEach((d, i) => { rigDevices[String(i + 1)] = { id: String(i + 1), ...d }; });
}
function overlaps(slots, footprints) {
  const byUni = {};
  slots.forEach((s, i) => {
    (byUni[s.universe] = byUni[s.universe] || []).push([s.address, s.address + footprints[i] - 1]);
  });
  for (const list of Object.values(byUni)) {
    list.sort((a, b) => a[0] - b[0]);
    for (let i = 1; i < list.length; i++) if (list[i][0] <= list[i - 1][1]) return true;
  }
  return false;
}

// 1. empty rig, 8 x 10ch from scratch
setRig([], { ten: 10 });
let fps = new Array(8).fill(10);
let plan = planDeviceAddresses(fps, { universe: 0, address: null, overflow: true });
check("packs sequentially from 0",
      !plan.error && plan.slots.every((s, i) => s.universe === 0 && s.address === i * 10),
      JSON.stringify(plan));

// 2. honours an explicit start address
plan = planDeviceAddresses(fps, { universe: 2, address: 100, overflow: true });
check("honours start universe + address",
      !plan.error && plan.slots[0].universe === 2 && plan.slots[0].address === 100 && plan.slots[7].address === 170,
      JSON.stringify(plan));

// 3. fills the gap left between two existing devices
setRig([
  { fixture: "ten", universe: 0, address: 0 },
  { fixture: "ten", universe: 0, address: 30 },
], { ten: 10 });
plan = planDeviceAddresses([10, 10], { universe: 0, address: null, overflow: true });
check("reuses the free gap",
      !plan.error && plan.slots[0].address === 10 && plan.slots[1].address === 20,
      JSON.stringify(plan));

// 4. never overlaps an existing device
setRig([{ fixture: "ten", universe: 0, address: 5 }], { ten: 10 });
plan = planDeviceAddresses([10, 10, 10], { universe: 0, address: 0, overflow: true });
check("skips over an occupied block",
      !plan.error && plan.slots.every((s) => s.address + 9 < 5 || s.address > 14),
      JSON.stringify(plan));

// 5. rolls over into the next universe when the current one is full
setRig([], { ten: 10 });
fps = new Array(60).fill(10);
plan = planDeviceAddresses(fps, { universe: 0, address: 0, overflow: true });
const unis = [...new Set(plan.slots.map((s) => s.universe))];
check("rolls into the next universe", !plan.error && unis.length === 2 && unis[0] === 0 && unis[1] === 1,
      JSON.stringify(unis));
check("no self-overlap across 60 devices", !plan.error && !overlaps(plan.slots, fps));

// 6. refuses instead of overflowing when told not to
plan = planDeviceAddresses(fps, { universe: 0, address: 0, overflow: false });
check("errors when overflow is off and it does not fit", !!plan.error, plan.error || null);

// 7. a fixture bigger than a universe is rejected, not silently truncated
setRig([], { huge: 600 });
plan = planDeviceAddresses([600], { universe: 0, address: null, overflow: true });
check("rejects an impossible footprint", !!plan.error, plan.error || null);

// 8. mixed footprints (paste of a heterogeneous selection)
setRig([], { a: 3, b: 12, c: 7 });
fps = [3, 12, 7, 3];
plan = planDeviceAddresses(fps, { universe: 0, address: null, overflow: true });
check("packs mixed footprints without overlap",
      !plan.error && !overlaps(plan.slots, fps) && plan.slots[1].address === 3 && plan.slots[2].address === 15,
      JSON.stringify(plan));

// 9. empty batch is a no-op
plan = planDeviceAddresses([], {});
check("empty batch returns no slots", !plan.error && plan.slots.length === 0);

// 10. the range label used by the modal preview
setRig([], { ten: 10 });
fps = new Array(3).fill(10);
plan = planDeviceAddresses(fps, { universe: 1, address: 0, overflow: true });
check("formats the patch range", formatPlanRange(plan.slots, fps) === "U1.0 → U1.29",
      formatPlanRange(plan.slots, fps));

console.log(JSON.stringify(out));
"""


@requires_node
def test_dmx_batch_allocator():
    script = _HARNESS % {"source": _allocator_source()}
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "alloc.js")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(script)
        result = subprocess.run([_NODE, path], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    checks = json.loads(result.stdout.strip().splitlines()[-1])
    failed = [c for c in checks if not c["ok"]]
    assert not failed, "\n".join(f"{c['name']}: {c['detail']}" for c in failed)
    assert len(checks) == 11


# -----------------------------------------------------------------------------
# Wiring
# -----------------------------------------------------------------------------

def test_bulk_add_replaces_the_immediate_add():
    src = _read("static/rig.js")
    assert "async function addDevicesWithModal" in src
    # The one-click add is gone: the fixture submenu now opens the modal.
    assert "addDeviceAuto" not in src
    assert "addDevicesWithModal(n, wx, wy)" in src


def test_modal_is_defined_and_exported():
    ui = _read("static/ui.js")
    assert "async function bulkAddDeviceModal" in ui
    assert "function readBulkAddForm" in ui
    assert re.search(r"return \{[^}]*bulkAddDeviceModal", ui, re.S), "not exported from ui.js"
    # Pure DOM path: no SweetAlert dependency, so it works with no CDN.
    body = ui[ui.index("async function bulkAddDeviceModal"):ui.index("async function htmlModal")]
    assert "openGuiModal" in body
    assert "Swal" not in body
    assert "bulkAddDeviceModal" in _read("static/core.js")


@pytest.mark.parametrize("role", ["count", "prefix", "universe", "address", "overflow", "columns", "spacing"])
def test_modal_exposes_every_setting(role):
    assert f'"{role}"' in _read("static/ui.js")


def test_copy_paste_helpers():
    src = _read("static/rig.js")
    assert "function copySelectedDevices" in src
    assert "function pasteClipboardDevices" in src
    assert "let rigClipboard" in src
    # Clones must be given fresh ids and freshly planned DMX slots.
    paste = src[src.index("function pasteClipboardDevices"):]
    paste = paste[:paste.index("\n}\n")]
    assert "planDeviceAddresses" in paste
    assert "createDeviceFromSpec" in paste
    # And the channel values are deep-copied, not shared by reference.
    copy = src[src.index("function copySelectedDevices"):]
    copy = copy[:copy.index("\n}\n")]
    assert "{ ...(deviceLocalValues" in copy


def test_context_menu_has_copy_paste_and_delete():
    src = _read("static/rig.js")
    root = src[src.index("function _rmRoot"):src.index("function _rmFixtures")]
    for key in ("rigmenu.copy", "rigmenu.pasteHere", "rigmenu.delete"):
        assert key in root
    assert 'shortcut: "Ctrl+C"' in root
    assert 'shortcut: "Ctrl+V"' in root
    assert 'shortcut: "Del"' in root


def test_keyboard_shortcuts_are_guarded():
    src = _read("static/rig.js")
    assert "function bindRigKeyboardShortcuts" in src
    assert "bindRigKeyboardShortcuts();" in src
    guard = src[src.index("function _rigShortcutsBlocked"):src.index("function bindRigKeyboardShortcuts")]
    # Editable fields and open modals keep the keyboard; the rig must be the
    # area the operator last touched.
    assert "_rigAreaActive" in guard
    assert "input, textarea, select" in guard
    assert ".dmx-modal-overlay" in guard

    handler = src[src.index("function bindRigKeyboardShortcuts"):]
    assert '"Delete"' in handler and '"Backspace"' in handler
    assert "deleteSelectedDevices()" in handler
    assert "copySelectedDevices()" in handler
    assert "pasteClipboardDevices()" in handler


def test_delete_still_asks_before_removing():
    src = _read("static/rig.js")
    body = src[src.index("async function deleteSelectedDevices"):]
    body = body[:body.index("\n}\n")]
    assert "confirmModal" in body


def test_css_for_menu_hints_and_preview():
    css = _read("static/app.css")
    assert ".rig-menu-shortcut" in css
    assert ".dmx-modal-preview" in css
    assert ".dmx-modal-bulk-add" in css


@requires_node
def test_touched_js_still_parses():
    for rel in ("static/rig.js", "static/ui.js", "static/core.js"):
        result = subprocess.run([_NODE, "-c", os.path.join(_REPO_ROOT, rel)], capture_output=True, text=True)
        assert result.returncode == 0, f"{rel}: {result.stderr}"


# -----------------------------------------------------------------------------
# i18n
# -----------------------------------------------------------------------------

_RIG_KEYS = [
    "rigmenu.copy", "rigmenu.pasteHere", "rigmenu.copyNothing", "rigmenu.pasteEmpty",
    "rigmenu.bulkTitle", "rigmenu.bulkConfirm", "rigmenu.bulkChannels", "rigmenu.bulkCount",
    "rigmenu.bulkPrefix", "rigmenu.bulkAddress", "rigmenu.bulkOverflow", "rigmenu.bulkColumns",
    "rigmenu.bulkSpacing",
]


def test_every_language_has_the_new_rig_keys():
    for path in sorted(glob.glob(os.path.join(_REPO_ROOT, "static", "lang", "*.json"))):
        with open(path, "r", encoding="utf-8") as fh:
            catalogue = json.load(fh)
        missing = [k for k in _RIG_KEYS if k not in catalogue]
        assert not missing, f"{os.path.basename(path)} misses {missing}"
        empty = [k for k in _RIG_KEYS if not str(catalogue.get(k, "")).strip()]
        assert not empty, f"{os.path.basename(path)} has empty values for {empty}"


def test_keys_used_by_rig_js_exist():
    src = _read("static/rig.js")
    used = set(re.findall(r'_rmT\(\s*"([A-Za-z0-9_.]+)"', src))
    catalogue = json.loads(_read("static/lang/en.json"))
    missing = sorted(k for k in used if k not in catalogue)
    assert not missing, f"keys used by rig.js but absent from en.json: {missing}"
