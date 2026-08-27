"""Tests for the Rapid Fire cue-panel view.

Rapid Fire is the third cue-panel view (next to Cue list and Timeline): a grid
of launch pads, one per cue list of the active project, each firing its list in
one click. These tests cover the settings contract, the static wiring between
template / JS / CSS, and the i18n catalogue.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _read(rel: str) -> str:
    with open(os.path.join(_REPO_ROOT, rel), "r", encoding="utf-8") as fh:
        return fh.read()


# -----------------------------------------------------------------------------
# Settings contract
# -----------------------------------------------------------------------------

def test_rapidfire_is_an_allowed_view_mode():
    import app

    assert "rapidfire" in app.CUE_VIEW_MODES
    assert app.CUE_VIEW_MODES == ("classic", "timeline", "rapidfire")


@pytest.mark.parametrize("value,expected", [
    ("classic", "classic"),
    ("timeline", "timeline"),
    ("rapidfire", "rapidfire"),
    ("  RapidFire  ".strip().lower(), "rapidfire"),
    ("nonsense", "classic"),
    ("", "classic"),
    (None, "classic"),
])
def test_view_mode_normalisation(value, expected):
    import app

    out = app._normalize_cue_editor_settings({"view_mode": value}, {})
    assert out["view_mode"] == expected


def test_view_mode_survives_a_settings_read_without_payload():
    """The no-payload branch must not downgrade a stored rapidfire view."""
    import app

    out = app._normalize_cue_editor_settings(None, {"view_mode": "rapidfire"})
    assert out["view_mode"] == "rapidfire"


def test_settings_get_exposes_the_stored_view_mode(monkeypatch):
    import app

    monkeypatch.setitem(app.SETTINGS, "cue_editor", {
        "view_mode": "rapidfire", "timeline_priority_mode": "top", "zoom_x": 120.0, "zoom_y": 88.0,
    })
    client = app.app.test_client()
    data = client.get("/api/settings").get_json()
    assert data["cue_editor"]["view_mode"] == "rapidfire"


# -----------------------------------------------------------------------------
# Static wiring: template ↔ JS ↔ CSS
# -----------------------------------------------------------------------------

def test_template_has_the_toggle_and_the_grid():
    html = _read("templates/index.html")
    assert 'id="cue-view-rapidfire"' in html
    assert 'data-view="rapidfire"' in html
    assert 'id="rapidfire-grid"' in html
    assert 'id="rapidfire-pads"' in html
    assert 'id="rapidfire-stop-all"' in html
    assert 'id="rapidfire-refresh"' in html
    # Loaded after cues.js/timeline.js, whose globals it uses.
    assert "rapidfire.js" in html
    assert html.index("cues.js") < html.index("rapidfire.js")
    assert html.index("timeline.js") < html.index("rapidfire.js")


def test_rapidfire_module_exposes_its_api():
    src = _read("static/rapidfire.js")
    for name in ("isRapidFireMode", "renderRapidFireGrid", "refreshRapidFirePads", "invalidateRapidFireCache"):
        assert f"window.{name}" in src, f"{name} is not exported"


def test_cue_table_switches_between_the_three_views():
    src = _read("static/cues.js")
    start = src.index("function renderCueTable")
    body = src[start:start + 1600]
    assert "isRapidFireMode" in body
    assert "rapidfire-grid" in body
    # The classic table hides for BOTH alternative views.
    assert "timelineMode || rapidFireMode" in body


def test_backend_sequence_accepts_a_forced_pipeline():
    """The pipeline can be forced instead of following the active view."""
    src = _read("static/cues.js")
    assert "async function runBackendSequence(seq, startIndex = 0, options = {})" in src
    assert "options.timeline" in src
    start = src.index("async function runBackendSequence")
    body = src[start:start + 1200]
    assert "if (useTimeline)" in body


def test_pads_run_as_a_cue_list_unless_the_list_cannot_be_one():
    """A pad plays the file as a cue list, from step 1, whatever view is active.

    The single exception is a list the cue-list mode cannot render at all --
    cues on top of each other, or holes between them. Those are fired in
    timeline mode without asking, because a pad is meant to be one click.
    """
    src = _read("static/rapidfire.js")
    body = src[src.index("async function firePad"):src.index("async function togglePad")]
    assert "runBackendSequence(sequence, 0, {" in body
    assert "timeline: asTimeline" in body
    assert "meta.timelineOnly" in body
    # The old "fire every timeline-authored file through the timeline" branch,
    # which keyed off the mere presence of timeline data, stays gone.
    assert "hasTimeline" not in src
    # The decision comes from the timing analysis, not from a stored flag.
    assert "window.analyzeCueListTiming" in src


def test_loop_option_is_wired_end_to_end():
    rf = _read("static/rapidfire.js")
    assert "function isLoopEnabled" in rf
    assert "function setLoopEnabled" in rf
    assert "rapidfire_loop" in rf
    fire = rf[rf.index("async function firePad"):rf.index("async function togglePad")]
    assert "loop:" in fire and "loopCount:" in fire

    # The toggle exists in the panel and is bound.
    html = _read("templates/index.html")
    assert 'id="rapidfire-loop"' in html
    assert 'data-i18n="cues.rapidFireLoopOption"' in html
    assert 'getElementById("rapidfire-loop")' in rf

    # The request carries it, and the endpoint forwards it to the engine.
    cues = _read("static/cues.js")
    payload = cues[cues.index('mode: "classic",'):]
    payload = payload[:payload.index("}),")]
    assert "loop:" in payload and "loop_count:" in payload
    app_src = _read("app.py")
    run = app_src[app_src.index('def api_playback_run'):]
    run = run[:run.index("@app.route", 10)]
    assert "loop=loop" in run and "loop_count=loop_count" in run


def test_loop_setting_survives_a_read(monkeypatch):
    """/api/settings GET rebuilds cue_editor key by key — the flag must be in it,
    or the toggle silently resets on every restart."""
    import app

    monkeypatch.setitem(app.SETTINGS, "cue_editor", {
        "view_mode": "rapidfire", "timeline_priority_mode": "top",
        "zoom_x": 120.0, "zoom_y": 88.0, "rapidfire_loop": True,
    })
    data = app.app.test_client().get("/api/settings").get_json()
    assert data["cue_editor"]["rapidfire_loop"] is True


def test_loop_setting_round_trips():
    import app

    out = app._normalize_cue_editor_settings({"rapidfire_loop": True}, {})
    assert out["rapidfire_loop"] is True
    # Kept when a later save omits it.
    out = app._normalize_cue_editor_settings({"view_mode": "rapidfire"}, out)
    assert out["rapidfire_loop"] is True
    # And it is a real boolean, whatever the client sent.
    assert app._normalize_cue_editor_settings({"rapidfire_loop": 0}, {})["rapidfire_loop"] is False
    assert "rapidfire_loop" in _read("static/timeline.js")


# -----------------------------------------------------------------------------
# Whole-sequence looping in the engine (the part Rapid Fire relies on)
# -----------------------------------------------------------------------------

_LOOP_SEQ = [
    {"name": "A", "sleep": "40", "duration": "0", "devices": {"1": {"channels": {"Universe": 0, "1": 255}}}},
    {"name": "B", "sleep": "40", "duration": "0", "devices": {"1": {"channels": {"Universe": 0, "1": 0}}}},
]


def _engine():
    from dmx_engine import DMXRenderEngine

    engine = DMXRenderEngine()
    engine.register_rig_devices(
        [{"device_id": "1", "universe": 0, "address": 1, "fixture": "x", "attr_map": {"dimmer": 0}}],
        replace=True,
    )
    return engine


def _watch(engine, timeout=4.0):
    """Run until playback goes idle; returns (passes seen, last snapshot)."""
    import time

    start = time.time()
    max_pass = 0
    snap = {}
    while time.time() - start < timeout:
        with engine._lock:
            snap = engine._playback_state_snapshot_locked()
        max_pass = max(max_pass, int(snap.get("loop_pass") or 0))
        if not snap.get("active") and time.time() - start > 0.1:
            break
        time.sleep(0.005)
    return max_pass, snap


def test_sequence_runs_once_without_loop():
    engine = _engine()
    try:
        engine.run_sequence(_LOOP_SEQ, mode="classic")
        passes, snap = _watch(engine)
        assert passes == 1
        assert snap["active"] is False
        assert snap["loop"] is False
    finally:
        engine.stop()


@pytest.mark.parametrize("count", [2, 3])
def test_loop_count_plays_exactly_that_many_passes(count):
    engine = _engine()
    try:
        engine.run_sequence(_LOOP_SEQ, mode="classic", loop=True, loop_count=count)
        passes, snap = _watch(engine)
        assert passes == count, f"expected {count} passes, saw {passes}"
        assert snap["active"] is False, "playback should stop after the last pass"
        assert snap["loop"] is False, "the loop flag must be cleared when it ends"
    finally:
        engine.stop()


def test_loop_without_count_keeps_going_until_stopped():
    import time

    engine = _engine()
    try:
        engine.run_sequence(_LOOP_SEQ, mode="classic", loop=True)
        time.sleep(0.5)  # ~6 passes of 80ms
        with engine._lock:
            snap = engine._playback_state_snapshot_locked()
        assert snap["active"] is True
        assert snap["loop"] is True
        assert snap["loop_count"] == 0, "0 means forever"
        assert snap["loop_pass"] >= 2, f"only reached pass {snap['loop_pass']}"

        engine.stop_playback()
        with engine._lock:
            snap = engine._playback_state_snapshot_locked()
        assert snap["active"] is False
        assert snap["loop"] is False
        assert snap["loop_pass"] == 0
    finally:
        engine.stop()


def test_loop_state_is_published_to_clients():
    engine = _engine()
    try:
        with engine._lock:
            snap = engine._playback_state_snapshot_locked()
        for key in ("loop", "loop_count", "loop_pass"):
            assert key in snap
    finally:
        engine.stop()


def test_timeline_module_knows_the_third_mode():
    src = _read("static/timeline.js")
    assert '"rapidfire"' in src
    assert "cue-view-rapidfire" in src
    assert "cue-editor-mode-rapidfire" in src
    # isTimelineEditorMode must stay false in Rapid Fire.
    assert 'return cueEditorSettings.view_mode === "timeline";' in src


def test_settings_modal_offers_the_third_mode():
    src = _read("static/sync_video.js")
    assert 'data-value="rapidfire"' in src


def test_css_places_the_grid_in_the_cue_panel_slot():
    css = _read("static/app.css")
    assert ".cues-panel > #rapidfire-grid:not(.hidden)" in css
    assert ".rapidfire-pads" in css
    assert ".rapidfire-pad.is-playing" in css
    assert ".rapidfire-pad.is-paused" in css
    # The global button compaction block uses !important, so the pads must too.
    assert "min-height: 64px !important" in css


def test_i18n_applied_event_exists_for_js_built_labels():
    assert 'CustomEvent("i18n:applied"' in _read("static/i18n.js")
    assert 'addEventListener("i18n:applied"' in _read("static/rapidfire.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available on PATH")
def test_rapidfire_js_syntax():
    result = subprocess.run(
        ["node", "-c", os.path.join(_REPO_ROOT, "static", "rapidfire.js")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


# -----------------------------------------------------------------------------
# i18n catalogues
# -----------------------------------------------------------------------------

def _rapidfire_keys_used_in_js() -> set:
    src = _read("static/rapidfire.js")
    return set(re.findall(r'tr\(\s*"(cues\.[A-Za-z0-9_]+)"', src))


def test_every_key_used_by_the_module_exists_in_english():
    catalogue = json.loads(_read("static/lang/en.json"))
    missing = sorted(k for k in _rapidfire_keys_used_in_js() if k not in catalogue)
    assert not missing, f"keys used by rapidfire.js but absent from en.json: {missing}"


def test_all_languages_carry_the_rapidfire_keys():
    reference = json.loads(_read("static/lang/en.json"))
    expected = {k for k in reference if k.startswith("cues.rapidFire")} | {"cues.viewRapidFire"}
    assert len(expected) >= 20, "the English catalogue lost its Rapid Fire keys"

    for path in sorted(glob.glob(os.path.join(_REPO_ROOT, "static", "lang", "*.json"))):
        with open(path, "r", encoding="utf-8") as fh:
            catalogue = json.load(fh)
        missing = sorted(expected - set(catalogue))
        assert not missing, f"{os.path.basename(path)} misses {missing}"
        empty = sorted(k for k in expected if not str(catalogue.get(k, "")).strip())
        assert not empty, f"{os.path.basename(path)} has empty values for {empty}"


def test_template_i18n_keys_resolve():
    html = _read("templates/index.html")
    catalogue = json.loads(_read("static/lang/en.json"))
    block = html[html.index('id="rapidfire-grid"'):html.index('id="rapidfire-pads"') + 200]
    keys = re.findall(r'data-i18n="([^"]+)"', block) + ["cues.viewRapidFire"]
    missing = sorted(k for k in keys if k not in catalogue)
    assert not missing, f"template keys absent from en.json: {missing}"
