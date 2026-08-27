"""Tests for the Python-only renderer.

The contract:
  - Python is the only renderer. It re-emits every universe at a fixed rate
    (dmx_runtime.emit_hz, 500 Hz), changed or not, like a DMX interface.
  - The browser sends attribute intents ({device, attr, value}); the engine
    resolves the DMX channel from the device's attr_map.
  - The browser only listens: universe values arrive over SSE as diffs plus a
    periodic keyframe, and nothing in the UI computes DMX.
  - Effects keep running as designed, timed in milliseconds.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import time

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_NODE = shutil.which("node")


def _read(rel: str) -> str:
    with open(os.path.join(_REPO_ROOT, rel), "r", encoding="utf-8") as fh:
        return fh.read()


def _rig(engine, count=6, per_universe=3):
    """A small rig with attribute maps, as the UI registers them."""
    devices = []
    for i in range(count):
        uni = i // per_universe
        base = (i % per_universe) * 10
        devices.append({
            "device_id": str(i + 1),
            "universe": uni,
            "address": base,
            "fixture": "test.fixture.json",
            "attr_map": {
                "dimmer.main.level": base,
                "dimmer": base,                      # historic alias
                "color.main.red": base + 1,
                "color.main.green": base + 2,
                "position.main.pan": base + 5,
            },
        })
    engine.register_rig_devices(devices, replace=True)
    return devices


@pytest.fixture()
def engine():
    from dmx_engine import DMXRenderEngine

    eng = DMXRenderEngine(artnet_ip="127.0.0.1")
    _rig(eng)
    yield eng
    try:
        eng.stop()
    except Exception:
        pass


# -----------------------------------------------------------------------------
# The output stage
# -----------------------------------------------------------------------------

def test_emitter_streams_every_universe_at_the_configured_rate(engine):
    engine.set_emit_hz(200)
    engine.start()
    time.sleep(0.3)
    before = engine.get_emit_stats()
    start = time.perf_counter()
    time.sleep(1.0)
    elapsed = time.perf_counter() - start
    after = engine.get_emit_stats()

    sweeps = after["sweeps"] - before["sweeps"]
    achieved = sweeps / elapsed
    assert 170 <= achieved <= 230, f"expected ~200 Hz, got {achieved:.0f} Hz"
    # Two universes for six devices at 3 per universe.
    assert after["universes"] == 2
    packets = after["packets"] - before["packets"]
    assert packets == pytest.approx(sweeps * 2, rel=0.05)
    assert after["late_sweeps"] == 0, "the emitter fell behind its own schedule"


def test_emitter_keeps_sending_when_nothing_changes(engine):
    """A real interface re-emits its buffer; silence is not an option."""
    engine.set_emit_hz(100)
    engine.start()
    time.sleep(0.5)
    first = engine.get_emit_stats()["packets"]
    time.sleep(0.5)
    assert engine.get_emit_stats()["packets"] > first


def test_compute_never_touches_the_socket():
    """The frame builder publishes snapshots; only the emitter sends."""
    src = _read("dmx_engine.py")
    start = src.index("def _render_frame(self)")
    body = src[start:src.index("def _emit_loop")]
    assert "artnet.send" not in body, "the compute frame must not send"
    assert "self._emit_frames[uni_num] = frame" in body
    emit = src[src.index("def _emit_loop"):src.index("def set_emit_hz")]
    assert "self.artnet.send_universe" in emit


def test_emit_rate_is_clamped(engine):
    engine.set_emit_hz(100000)
    assert engine._emit_hz == 1000.0
    engine.set_emit_hz(0)
    assert engine._emit_hz == 1.0
    engine.set_emit_hz("nonsense")
    assert engine._emit_hz == 1.0


# -----------------------------------------------------------------------------
# Universe bounds (the bug a 500 Hz emitter would hit 500 times a second)
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (0, 0), (32767, 32767), (32768, None), (70000, None), (-1, None), ("x", None), (None, None),
])
def test_universe_validation(value, expected):
    from dmx_engine import DMXRenderEngine

    assert DMXRenderEngine.valid_universe(value) == expected


def test_out_of_range_universe_never_enters_the_engine(engine):
    engine.start()
    engine.set_channel("bad", 70000, 5, 255)
    engine.set_channels_multi("bad", {99999: {1: 255}, 1: {1: 255}})
    time.sleep(0.3)
    assert 70000 not in engine._universes
    assert 99999 not in engine._universes
    assert 1 in engine._universes
    # And the stream is unharmed.
    stats = engine.get_emit_stats()
    assert stats["packets"] > 0


# -----------------------------------------------------------------------------
# The manual attribute layer
# -----------------------------------------------------------------------------

def test_attribute_intent_lands_on_the_mapped_channel(engine):
    applied = engine.set_manual_attrs([
        {"device_id": "1", "attr": "dimmer.main.level", "value": 211},
        {"device_id": "2", "attr": "color.main.red", "value": 90},
    ])
    assert applied == 2
    engine._render_frame()
    assert engine._universes[0][0] == 211          # device 1, dimmer at base+0
    assert engine._universes[0][11] == 90          # device 2, red at base+1


def test_attribute_intent_is_clamped_and_case_insensitive(engine):
    engine.set_manual_attrs([{"device_id": "1", "attr": "DIMMER.Main.Level", "value": 999}])
    engine._render_frame()
    assert engine._universes[0][0] == 255


def test_unknown_attribute_or_device_is_refused(engine):
    assert engine.set_manual_attrs([{"device_id": "1", "attr": "nope.nope", "value": 10}]) == 0
    assert engine.set_manual_attrs([{"device_id": "999", "attr": "dimmer", "value": 10}]) == 0
    assert engine.set_manual_attrs("not a list") == 0
    assert engine.get_manual_attrs() == {}


def test_null_value_releases_one_attribute(engine):
    engine.set_manual_attrs([
        {"device_id": "1", "attr": "dimmer.main.level", "value": 200},
        {"device_id": "1", "attr": "color.main.red", "value": 100},
    ])
    engine.set_manual_attrs([{"device_id": "1", "attr": "dimmer.main.level", "value": None}])
    held = engine.get_manual_attrs()
    assert held == {"1": {"color.main.red": 100}}


def test_release_drops_the_hold(engine):
    engine.set_manual_attrs([
        {"device_id": "1", "attr": "dimmer", "value": 200},
        {"device_id": "2", "attr": "dimmer", "value": 200},
    ])
    assert engine.release_manual_attrs(["1"]) == 1
    assert list(engine.get_manual_attrs()) == ["2"]
    assert engine.release_manual_attrs(None) == 1
    assert engine.get_manual_attrs() == {}


def test_manual_layer_wins_over_the_cue_base(engine):
    """A fader held on the desk beats the value the cue left behind."""
    with engine._lock:
        engine._devices["1"].channels[0] = 40
    engine._render_frame()
    assert engine._universes[0][0] == 40
    engine.set_manual_attrs([{"device_id": "1", "attr": "dimmer", "value": 250}])
    engine._render_frame()
    assert engine._universes[0][0] == 250


def test_manual_layer_follows_a_re_address(engine):
    """Attribute-keyed, so re-patching a fixture moves its held value with it."""
    engine.set_manual_attrs([{"device_id": "1", "attr": "dimmer.main.level", "value": 180}])
    engine._render_frame()
    assert engine._universes[0][0] == 180

    engine.register_rig_devices([{
        "device_id": "1", "universe": 0, "address": 100, "fixture": "test.fixture.json",
        "attr_map": {"dimmer.main.level": 100, "dimmer": 100},
    }], replace=False)
    engine._render_frame()
    assert engine._universes[0][100] == 180, "the hold did not follow the new address"


def test_attrs_endpoint(monkeypatch):
    import app

    engine = app.RENDER_ENGINE
    if engine is None:
        from dmx_engine import DMXRenderEngine

        engine = DMXRenderEngine(artnet_ip="127.0.0.1")
        monkeypatch.setattr(app, "RENDER_ENGINE", engine)
    _rig(engine)
    try:
        client = app.app.test_client()
        res = client.post("/api/live/attrs", json={
            "updates": [{"device_id": "1", "attr": "dimmer.main.level", "value": 123}],
        })
        assert res.status_code == 200
        assert res.get_json()["applied"] == 1

        res = client.post("/api/live/attrs", json={"release": ["1"]})
        assert res.get_json()["released"] == 1

        assert client.post("/api/live/attrs", json={"release_all": True}).status_code == 200
        assert client.post("/api/live/attrs", data="nope",
                           content_type="application/json").status_code == 400
    finally:
        engine.stop()


# -----------------------------------------------------------------------------
# Effects: still Python, still in milliseconds
# -----------------------------------------------------------------------------

def test_effects_animate_over_ms_time():
    import intelligent_fx as FX

    defn = FX.get_effect_def("breathing")
    assert defn, "the breathing effect must exist in the Python engine"
    params = {"speed": 1.0, "min": 0, "max": 255, "phase": "0"}
    values = []
    # 70 ms steps: not a harmonic of the 1 s period, so the samples land all
    # over the curve instead of on the same four points.
    for t_ms in range(0, 2000, 70):
        ctx = {
            "params": params, "group": dict(params, deviceIds=["1"]),
            "t_ms": float(t_ms), "device_index": 0, "device_count": 1,
            "device_id": "1", "member_id": "1", "target": "dimmer", "base": 200,
        }
        values.append(int(FX.apply_effect_value(defn, 200, FX.eval_effect("breathing", ctx), scale=1.0)))
    assert len(set(values)) > 12, f"a 1 Hz breath should sweep, got {values}"
    assert min(values) < 40 and max(values) > 215, values


def test_every_browser_effect_has_a_python_twin():
    """Nothing may be renderable in the browser only."""
    import intelligent_fx as FX

    js_effects = {
        os.path.basename(p)[:-3]
        for p in glob.glob(os.path.join(_REPO_ROOT, "intelligent_effects", "*.js"))
    }
    missing = sorted(name for name in js_effects if FX.get_effect_def(name) is None)
    assert not missing, f"effects the engine cannot render: {missing}"


def test_effect_member_resolution_is_memoised():
    """It used to be resolved once per device — quadratic, 41 ms per frame."""
    src = _read("dmx_engine.py")
    assert "def _effect_runtime_for_group" in src
    body = src[src.index("def _apply_effect_group_to_device"):src.index("def _apply_effects_for_device")]
    assert "_effect_runtime_for_group(group)" in body
    assert "self._resolve_effect_members(group)" not in body
    frame = src[src.index("def _render_frame(self)"):src.index("def _emit_loop")]
    assert "_effect_runtime_cache.clear()" in frame


# -----------------------------------------------------------------------------
# The preview feed
# -----------------------------------------------------------------------------

def test_preview_sends_a_keyframe_then_diffs(engine):
    pushes = []
    engine.add_state_callback(lambda state: pushes.append(state))

    engine._render_frame()
    engine._broadcast_state(include_universes=True)
    assert pushes[-1]["preview_full"] is True
    assert "universes" in pushes[-1]

    engine._broadcast_state(include_universes=True)
    assert pushes[-1]["preview_full"] is False
    assert "universes" not in pushes[-1], "unchanged universes must not be re-sent whole"

    engine.set_manual_attrs([{"device_id": "1", "attr": "dimmer", "value": 77}])
    engine._render_frame()
    engine._broadcast_state(include_universes=True)
    diff = pushes[-1].get("universes_diff") or {}
    assert diff.get("0", {}).get("0") == 77
    assert sum(len(v) for v in diff.values()) < 10, "a diff must carry only what moved"


def test_preview_rate_is_a_setting(engine):
    engine.set_preview_hz(15)
    with engine._lock:
        engine._playback_state = {"active": False}
        assert engine._effective_state_broadcast_sec_locked() == pytest.approx(1 / 15.0)
    engine.set_preview_hz(500)
    assert engine._preview_hz == 120.0


# -----------------------------------------------------------------------------
# The browser no longer renders
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("gone", [
    "applyUniverseState",     # channel writes from the browser
    "dmxNetworkPump",         # the 50 Hz DMX push
    "dmxUniverseBuffers",
    "sendToEngineWithEffects",
    "effectTick",             # the 50 Hz JS effect evaluation
    "startEffectRunner",
    "setRenderMode",          # there is only one renderer now
])
def test_browser_dmx_pipeline_is_gone(gone):
    for path in glob.glob(os.path.join(_REPO_ROOT, "static", "*.js")):
        src = open(path, "r", encoding="utf-8").read()
        assert gone not in src, f"{os.path.basename(path)} still references {gone}"


def test_browser_writes_through_attributes():
    core = _read("static/core.js")
    assert "/api/live/attrs" in core
    assert "function buildDeviceAttrUpdates" in core
    assert "window.queueDeviceAttrs" in core
    # No channel-level endpoint left in the UI.
    for path in glob.glob(os.path.join(_REPO_ROOT, "static", "*.js")):
        src = open(path, "r", encoding="utf-8").read()
        assert "/api/live/channels" not in src, f"{os.path.basename(path)} still writes raw channels"


def test_manual_control_goes_through_the_attribute_path():
    rig = _read("static/rig.js")
    body = rig[rig.index("async function applySelectionToEngine"):]
    body = body[:body.index("\n}\n")]
    assert "applyDevicesToEngine" in body
    assert "perU" not in body, "the per-universe channel map should be gone"


def test_render_mode_setting_is_replaced_by_the_rates():
    app_src = _read("app.py")
    assert '"emit_hz"' in app_src and '"preview_hz"' in app_src
    assert 'out.pop("render_mode", None)' in app_src
    engine_src = _read("dmx_engine.py")
    assert "_render_mode" not in engine_src
    assert "_render_ui_frame" not in engine_src
    modal = _read("static/sync_video.js")
    assert 'id="rt-emit-hz"' in modal and 'id="rt-preview-hz"' in modal


@pytest.mark.skipif(_NODE is None, reason="node not available on PATH")
def test_touched_js_still_parses():
    for path in sorted(glob.glob(os.path.join(_REPO_ROOT, "static", "*.js"))):
        result = subprocess.run([_NODE, "-c", path], capture_output=True, text=True)
        assert result.returncode == 0, f"{os.path.basename(path)}: {result.stderr}"


# -----------------------------------------------------------------------------
# Loading a cue vs the manual layer
# -----------------------------------------------------------------------------

def test_loading_a_cue_takes_over_the_channels_it_addresses(engine):
    """The layer that lets a fader win must not outlive the next cue.

    The manual layer is applied on top of the cue base, so a held value wins --
    but a device that had been touched once (or merely selected, which pushes
    its values as intents) used to ignore every cue afterwards: the blackout
    cue left it lit and the next look left it dark.
    """
    engine.set_manual_attrs([
        {"device_id": "1", "attr": "dimmer", "value": 255},
        {"device_id": "2", "attr": "dimmer", "value": 255},
    ])
    engine._render_frame()
    assert (engine._universes[0][0], engine._universes[0][10]) == (255, 255)

    engine.go_cue({"devices": {
        "1": {"channels": {"Universe": 0, "0": 0}},
        "2": {"channels": {"Universe": 0, "10": 0}},
    }, "fade": "0"}, ["1", "2"])
    engine._render_frame()

    assert (engine._universes[0][0], engine._universes[0][10]) == (0, 0), "the blackout must land"
    assert engine.get_manual_attrs() == {}

    # And the other way round: a look after a hand-held blackout.
    engine.set_manual_attrs([{"device_id": "1", "attr": "dimmer", "value": 0}])
    engine.go_cue({"devices": {"1": {"channels": {"Universe": 0, "0": 200}}}, "fade": "0"}, ["1"])
    engine._render_frame()
    assert engine._universes[0][0] == 200


def test_a_cue_only_releases_the_channels_it_writes(engine):
    """A pan held by hand survives a cue that only speaks about dimmers."""
    engine.set_manual_attrs([
        {"device_id": "1", "attr": "dimmer", "value": 255},
        {"device_id": "1", "attr": "position.main.pan", "value": 128},
    ])
    engine.go_cue({"devices": {"1": {"channels": {"Universe": 0, "0": 0}}}, "fade": "0"}, ["1"])
    engine._render_frame()

    assert engine._universes[0][0] == 0, "the dimmer belongs to the cue now"
    assert engine._universes[0][5] == 128, "the pan is still held"
    assert engine.get_manual_attrs() == {"1": {"position.main.pan": 128}}


def test_a_cue_leaves_devices_it_never_mentions_alone(engine):
    engine.set_manual_attrs([{"device_id": "3", "attr": "dimmer", "value": 200}])
    engine.go_cue({"devices": {"1": {"channels": {"Universe": 0, "0": 0}}}, "fade": "0"}, ["1"])
    engine._render_frame()

    assert engine._universes[0][20] == 200, "device 3 was not addressed"
    assert engine.get_manual_attrs() == {"3": {"dimmer": 200}}


def test_an_alias_on_the_same_channel_is_released_too(engine):
    """attr_map holds both "dimmer" and "dimmer.main.level" on one channel."""
    engine.set_manual_attrs([{"device_id": "1", "attr": "dimmer.main.level", "value": 255}])
    engine.go_cue({"devices": {"1": {"channels": {"Universe": 0, "0": 12}}}, "fade": "0"}, ["1"])
    engine._render_frame()
    assert engine._universes[0][0] == 12


def test_raw_channel_overrides_are_released_by_a_cue(engine):
    engine.start()
    engine.set_channel("probe", 0, 0, 255)
    engine._render_frame()
    assert engine._universes[0][0] == 255

    engine.go_cue({"devices": {"1": {"channels": {"Universe": 0, "0": 30}}}, "fade": "0"}, ["1"])
    engine._render_frame()
    assert engine._universes[0][0] == 30
    assert not engine._direct_channels.get(0)
