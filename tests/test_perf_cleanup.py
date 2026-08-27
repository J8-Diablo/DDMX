"""Smoke tests for the perf-cleanup branch.

Validates that:
- The Flask app module imports without error.
- The DMX engine instantiates and its idle broadcast interval matches the new
  20Hz cadence (Phase 6.1).
- The engine accepts playback_ui_fps up to 60 (matches the new JS-side cap).
- All key JS files have valid syntax via `node -c`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# -----------------------------------------------------------------------------
# Python: app + engine smoke tests
# -----------------------------------------------------------------------------

def test_app_module_imports():
    """Flask app module must import without side-effect errors."""
    import app  # noqa: F401


def test_engine_module_imports():
    import dmx_engine  # noqa: F401


def test_engine_idle_broadcast_is_20hz():
    """Phase 6.1: idle (no playback) broadcast must be 50ms = 20Hz, not 250ms."""
    from dmx_engine import DMXRenderEngine

    engine = DMXRenderEngine()
    try:
        # No playback active by default
        with engine._lock:
            engine._playback_state = {"active": False}
            interval = engine._effective_state_broadcast_sec_locked()
        assert interval == pytest.approx(0.05, abs=1e-6), (
            f"Idle broadcast interval should be 50ms, got {interval * 1000:.1f}ms"
        )
    finally:
        # Best effort cleanup
        try:
            engine.stop()
        except Exception:
            pass


def test_engine_playback_broadcast_uses_ui_fps():
    """When playback is active, broadcast rate follows playback_ui_fps."""
    from dmx_engine import DMXRenderEngine

    engine = DMXRenderEngine()
    try:
        with engine._lock:
            engine._playback_state = {"active": True}
            engine._playback_ui_fps = 30.0
            interval = engine._effective_state_broadcast_sec_locked()
        assert interval == pytest.approx(1.0 / 30.0, abs=1e-6)

        with engine._lock:
            engine._playback_ui_fps = 60.0
            interval = engine._effective_state_broadcast_sec_locked()
        # Capped to 60Hz minimum interval
        assert interval == pytest.approx(1.0 / 60.0, abs=1e-6)
    finally:
        try:
            engine.stop()
        except Exception:
            pass


def test_engine_accepts_60fps_ui():
    """playback_ui_fps up to 60 must be accepted by set_playback_ui_fps."""
    from dmx_engine import DMXRenderEngine

    engine = DMXRenderEngine()
    try:
        engine.set_playback_ui_fps(60.0)
        assert engine._playback_ui_fps == pytest.approx(60.0)

        engine.set_playback_ui_fps(120.0)  # clamped to 60
        assert engine._playback_ui_fps == pytest.approx(60.0)

        engine.set_playback_ui_fps(0.5)  # clamped up to 1
        assert engine._playback_ui_fps == pytest.approx(1.0)
    finally:
        try:
            engine.stop()
        except Exception:
            pass


# -----------------------------------------------------------------------------
# JS: syntax check via node
# -----------------------------------------------------------------------------

JS_FILES = [
    "static/core.js",
    "static/rig.js",
    "static/cues.js",
    "static/effects.js",
]


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node not available on PATH",
)
@pytest.mark.parametrize("rel_path", JS_FILES)
def test_js_syntax(rel_path):
    """Each modified JS file must pass `node -c`."""
    abs_path = os.path.join(_REPO_ROOT, rel_path)
    result = subprocess.run(
        ["node", "-c", abs_path],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"node -c failed for {rel_path}:\n{result.stderr}"
    )


# -----------------------------------------------------------------------------
# JS: lang JSON validity
# -----------------------------------------------------------------------------

LANG_FILES = ["en", "fr", "de", "es", "it", "nl", "pt", "ge"]


@pytest.mark.parametrize("lang_code", LANG_FILES)
def test_lang_json_valid(lang_code):
    import json

    path = os.path.join(_REPO_ROOT, "static", "lang", f"{lang_code}.json")
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    # Sanity: distribute keys exist (from the earlier Distribute feature)
    assert "controller.distribute" in data, (
        f"missing controller.distribute key in {lang_code}.json"
    )
    assert "controller.distributeLinear" in data
    assert "controller.distributeRandom" in data


# -----------------------------------------------------------------------------
# JS: ensure key new symbols exist in the source (regression guard)
# -----------------------------------------------------------------------------

def _read(rel):
    with open(os.path.join(_REPO_ROOT, rel), "r", encoding="utf-8") as fh:
        return fh.read()


def test_rig_js_has_shared_drag_handler():
    """Phase: window listener leak fix introduced _activeDragHandler."""
    src = _read("static/rig.js")
    assert "_activeDragHandler" in src


def test_rig_js_has_drawrig_throttle():
    """Phase: drawRig is rAF-throttled."""
    src = _read("static/rig.js")
    assert "_drawRigPending" in src
    assert "_drawRigImpl" in src


def test_rig_js_has_selection_apply_debounce():
    """Phase 2: scheduleSelectionApply wraps applySelectionToEngine."""
    src = _read("static/rig.js")
    assert "scheduleSelectionApply" in src
    assert "_selectionApplyTimer" in src


def test_core_js_has_memoization_caches():
    """Phase 1.2 + 4: device caches with invalidators."""
    src = _read("static/core.js")
    assert "_devicePreviewChannelsCache" in src
    assert "invalidateDevicePreviewCache" in src
    assert "_deviceAttrAbsCache" in src
    assert "invalidateDeviceAttrCache" in src


def test_core_js_universe_change_detection():
    """Phase 1.3: handleEngineState skips redraw when universes unchanged."""
    src = _read("static/core.js")
    assert "universesChanged" in src


def test_core_js_render_mode_clears_state():
    """Phase 3.2: setRenderMode clears preview caches on mode switch."""
    src = _read("static/core.js")
    # The clear happens via Object.keys delete loop for lastDmxFrames
    assert "for (const k of Object.keys(lastDmxFrames))" in src


def test_core_js_fps_cap_60():
    """Earlier fix: DMX_PLAYBACK_UI_FPS capped at 60."""
    src = _read("static/core.js")
    assert "Math.min(60, raw)" in src


def test_cues_js_playback_ui_caching():
    """Phase 1.4: updatePlaybackUI has DOM value caching."""
    src = _read("static/cues.js")
    assert "_playbackUiCache" in src
    assert "_setIfChanged" in src


def test_effects_js_has_backend_sync_debounce():
    """Earlier fix: scheduleBackendSync debounces backend mode param changes."""
    src = _read("static/effects.js")
    assert "scheduleBackendSync" in src


def test_effects_js_attr_click_guard():
    """Phase 5.1: clicking the same attr row skips rebuild."""
    src = _read("static/effects.js")
    # The guard line we added
    assert "already active, skip rebuild" in src


# -----------------------------------------------------------------------------
# Backend mode "rémanence" bug fix tests
# -----------------------------------------------------------------------------

def test_effects_js_playback_guard_removed():
    """Bug fix: syncBackendLiveGroups no longer aborts during playback so
    live effect edits propagate to the backend mid-cue."""
    src = _read("static/effects.js")
    # The function exists
    assert "async function syncBackendLiveGroups" in src
    # And no longer has the early-return on playbackActive inside it
    # (we check the function body for the guard pattern)
    func_start = src.index("async function syncBackendLiveGroups")
    func_end = src.index("\n}", func_start)
    body = src[func_start:func_end]
    assert "if (window.playbackActive) return;" not in body


def test_effects_js_calls_purge_endpoint():
    """disableGroupOnRig must hit /purge in backend mode (rémanence fix)."""
    src = _read("static/effects.js")
    assert "/api/live/effects/groups/purge" in src
    func_start = src.index("function disableGroupOnRig")
    func_end = src.index("\n}", func_start)
    assert "/api/live/effects/groups/purge" in src[func_start:func_end]


def test_app_py_has_purge_endpoint():
    """app.py exposes the new purge endpoint."""
    src = _read("app.py")
    assert "/api/live/effects/groups/purge" in src
    assert "remove_effect_group_everywhere" in src


def test_engine_remove_effect_group_everywhere():
    """Engine purges a group from both live and cue pools."""
    from dmx_engine import DMXRenderEngine

    engine = DMXRenderEngine()
    try:
        with engine._lock:
            engine._live_effect_groups = {
                "g1": {"id": "g1", "type": "intelligent"},
                "g2": {"id": "g2", "type": "intelligent"},
            }
            engine._cue_effect_groups = {
                "g1": {"id": "g1", "type": "intelligent"},
                "g3": {"id": "g3", "type": "intelligent"},
            }
            engine._live_groups_by_device = {"d1": {"g1"}, "d2": {"g2"}}
            engine._cue_groups_by_device = {"d1": {"g1"}, "d3": {"g3"}}

        removed = engine.remove_effect_group_everywhere(["g1"])

        # g1 was in both pools, so 2 removals
        assert removed == 2
        with engine._lock:
            assert "g1" not in engine._live_effect_groups
            assert "g1" not in engine._cue_effect_groups
            assert "g2" in engine._live_effect_groups  # untouched
            assert "g3" in engine._cue_effect_groups  # untouched
            # Device maps rebuilt without g1
            assert "g1" not in engine._live_groups_by_device.get("d1", set())
            assert "g1" not in engine._cue_groups_by_device.get("d1", set())
    finally:
        try:
            engine.stop()
        except Exception:
            pass


def test_engine_remove_effect_group_handles_empty():
    """Calling purge with empty/None is a no-op (no exception)."""
    from dmx_engine import DMXRenderEngine

    engine = DMXRenderEngine()
    try:
        assert engine.remove_effect_group_everywhere([]) == 0
        assert engine.remove_effect_group_everywhere(None) == 0
    finally:
        try:
            engine.stop()
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Dead-code cleanup guards
# -----------------------------------------------------------------------------

def test_effect_chaser_type_evaluates():
    """`chaser` used to call a `_chaser` that did not exist (NameError inside
    the render loop). It must evaluate and actually vary across the cycle."""
    import Effect

    spec = {"type": "chaser", "amplitude": 100, "frequency": 1.0, "width": 0.25}
    seen = set()
    for count in (1, 4, 8):
        for idx in range(count):
            for t_s in (0.0, 0.2, 0.4, 0.6, 0.8):
                value = Effect.eval_effects(spec, t_s, idx=idx, count=count)
                assert -1.0 <= value <= 1.0
                seen.add(round(value, 3))
    assert len(seen) > 1, "chaser output never changes"


def test_no_shadowed_toplevel_js_functions():
    """Two top-level `function foo()` in one file silently shadow each other —
    that is how cues.js grew a second, divergent playback engine."""
    import collections
    import glob
    import re

    decl = re.compile(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(")
    offenders = []
    for path in sorted(glob.glob(os.path.join(_REPO_ROOT, "static", "*.js"))):
        names = collections.Counter()
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                match = decl.match(line)
                if match:
                    names[match.group(1)] += 1
        for name, hits in names.items():
            if hits > 1:
                offenders.append("%s: %s x%d" % (os.path.basename(path), name, hits))
    assert not offenders, "shadowed top-level functions: " + ", ".join(offenders)


def test_ui_follow_playback_engine_is_gone():
    """Playback is backend-driven; the browser-side sequencer was dead code."""
    src = _read("static/cues.js")
    for gone in ("uiFollowSequence", "uiFollowStep", "computeFadePattern", "uiFollowStopFlag"):
        assert gone not in src, "%s came back in cues.js" % gone
