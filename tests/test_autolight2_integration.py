"""Integration test: AutoLight 2.0 pipeline wired into _AutoLightRenderer.

Drives the renderer's _render_dj path with a fake audio analyzer + service +
devices (no hardware, no engine), asserting it writes DMX, respects the
guardrail ceiling, and honours write-disabled modes.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import autolight  # noqa: E402
from autolight import _AutoLightRenderer  # noqa: E402


class FakeAudio:
    def __init__(self, **over):
        self.over = over

    def snapshot(self):
        snap = {"available": True, "active": True, "rms": 0.2,
                "kick_count": 0, "snare_count": 0, "beat_intensity": 1.0,
                "bpm": 128.0, "bpm_source": "auto",
                "structure": {"level": 4, "drop_score": 2.0,
                              "long_rms": 0.2, "short_rms": 0.45}}
        snap.update(self.over)
        return snap


class FakeMusic:
    def metadata_for_current(self):
        return None

    def status(self):
        return {}


class Dev:
    def __init__(self, uni, caps):
        self.universe = uni
        self.capabilities = caps
        self.attr_map = {}
        self.x = None
        self.y = None
        self.home_pan = None
        self.home_tilt = None
        self.invert_pan = False
        self.invert_tilt = False
        self.cname = ""
        self.fixture_template = ""
        self.base_address = 0


class FakeService:
    def __init__(self, settings):
        self._music = FakeMusic()
        self._engine = None
        self._settings = settings
        self._devs = {
            "1": Dev(0, {"has_dimmer": True, "dimmer_channel": 0, "has_color": True,
                         "red_channel": 1, "green_channel": 2, "blue_channel": 3,
                         "has_movement": False, "strobe_friendly": False}),
            "2": Dev(0, {"has_dimmer": True, "dimmer_channel": 10, "has_color": False,
                         "has_movement": True, "pan_channel": 11, "tilt_channel": 12,
                         "strobe_friendly": False}),
        }

    def get_settings(self):
        return self._settings

    def _engine_devices_snapshot_locked(self):
        return dict(self._devs)

    def is_identifying(self, dev_id):
        return False

    def _media_probe_best_track(self):
        return None


def _renderer(settings):
    svc = FakeService(settings)
    r = _AutoLightRenderer(FakeAudio(), svc)
    r.on_rig_changed(svc._devs)
    return r, svc


def _run(r, frames=20, start=1.0, dt=0.025):
    universes = {0: [0] * 512}
    for i in range(frames):
        r(universes, start + i * dt)
    return universes


def test_pipeline_writes_dmx_on_drop():
    r, _ = _renderer({"enabled": True, "mode": "live", "render_mode": "director",
                      "intensity_ceiling": 1.0, "contrast": 1.0, "allow_strobe": True})
    uni = _run(r)
    assert uni[0][0] > 200          # dev1 dimmer near full on drop
    assert uni[0][10] > 200         # dev2 dimmer
    assert r._dj_diag.get("intent") == "drop"
    assert r._diag_last_frame_wrote > 0


def test_effects_mode_routes_to_new_pipeline():
    r, _ = _renderer({"enabled": True, "mode": "live", "render_mode": "effects",
                      "intensity_ceiling": 1.0, "contrast": 1.0, "allow_strobe": True})
    uni = _run(r)
    assert uni[0][0] > 200
    assert r._dj_diag.get("intent") == "drop"


def test_intensity_ceiling_caps_output():
    r, _ = _renderer({"enabled": True, "mode": "live", "render_mode": "director",
                      "intensity_ceiling": 0.4, "contrast": 1.0, "allow_strobe": True})
    uni = _run(r)
    # Dimmer must respect the 40 % ceiling (≈102/255), allow some headroom.
    assert uni[0][0] <= 115


def test_off_mode_does_not_write():
    r, _ = _renderer({"enabled": True, "mode": "off", "render_mode": "director"})
    uni = _run(r)
    assert all(v == 0 for v in uni[0])


def test_disabled_releases_rig():
    r, _ = _renderer({"enabled": False, "mode": "live", "render_mode": "director"})
    uni = _run(r)
    assert all(v == 0 for v in uni[0])


def test_snapshot_exposes_dj_diagnostics():
    r, _ = _renderer({"enabled": True, "mode": "live", "render_mode": "director",
                      "intensity_ceiling": 1.0, "contrast": 1.0, "allow_strobe": True})
    _run(r)
    snap = r.last_snapshot()
    assert "dj" in snap
    assert "intent" in snap["dj"]
    assert "grid" in snap["dj"]
