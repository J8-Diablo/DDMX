"""Unit tests for autolight_show.ShowRenderer — fake devices, no engine."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from autolight_brain import Directive, SILENCE, CALM, GROOVE, BUILD, DROP  # noqa: E402
from autolight_show import (  # noqa: E402
    ShowRenderer, assign_roles, hsv_to_rgb,
    ROLE_STROBE, ROLE_MOVER, ROLE_WASH, ROLE_ACCENT,
)


class Dev:
    def __init__(self, universe=0, caps=None, attr_map=None,
                 home_pan=None, home_tilt=None, invert_pan=False, invert_tilt=False,
                 x=None, y=None):
        self.universe = universe
        self.capabilities = caps or {}
        self.attr_map = attr_map or {}
        self.home_pan = home_pan
        self.home_tilt = home_tilt
        self.invert_pan = invert_pan
        self.invert_tilt = invert_tilt
        self.x = x
        self.y = y


def _mover(universe=0, **kw):
    caps = {"has_movement": True, "has_dimmer": True, "has_color": True,
            "dimmer_channel": 0, "pan_channel": 1, "tilt_channel": 2,
            "red_channel": 3, "green_channel": 4, "blue_channel": 5,
            "strobe_friendly": False}
    return Dev(universe=universe, caps=caps,
               attr_map={"pan": 1, "tilt": 2, "red": 3, "green": 4, "blue": 5,
                         "dimmer": 0}, **kw)


def _wash(universe=0, base=10, **kw):
    caps = {"has_color": True, "has_dimmer": True,
            "dimmer_channel": base, "red_channel": base + 1,
            "green_channel": base + 2, "blue_channel": base + 3,
            "has_movement": False, "strobe_friendly": False}
    return Dev(universe=universe, caps=caps, **kw)


def _strobe(universe=0, base=20, **kw):
    caps = {"has_dimmer": True, "dimmer_channel": base, "strobe_friendly": True,
            "has_color": False, "has_movement": False}
    return Dev(universe=universe, caps=caps,
               attr_map={"dimmer": base, "strobe": base + 1, "focus": base + 2}, **kw)


def _drct(**kw):
    d = Directive()
    for k, v in kw.items():
        setattr(d, k, v)
    if "palette" not in kw:
        d.palette = {"scheme": "analogous", "saturation": 0.8, "change_rate": "slow"}
    return d


# --------------------------------------------------------------- basics ----

def test_hsv_to_rgb_primaries():
    assert hsv_to_rgb(0, 1, 1) == (255, 0, 0)
    assert hsv_to_rgb(120, 1, 1) == (0, 255, 0)
    assert hsv_to_rgb(240, 1, 1) == (0, 0, 255)


def test_role_assignment():
    devs = {"0": _strobe(), "1": _mover(), "2": _wash()}
    roles = assign_roles(devs)
    assert roles["0"] == ROLE_STROBE
    assert roles["1"] == ROLE_MOVER
    assert roles["2"] == ROLE_WASH


# --------------------------------------------------------------- dimmer ----

def test_drop_lights_rig_bright():
    devs = {"0": _wash(base=0), "1": _wash(base=10)}
    r = ShowRenderer()
    d = _drct(intent=DROP, energy=1.0, groove_on_kick=True, beat_phase=0.0,
              palette={"scheme": "complementary", "saturation": 1.0, "change_rate": "sharp"})
    w = r.render(1.0, d, devs)
    # Dimmer channels (0 and 10) should be near full at beat onset.
    assert w[0][0] > 200
    assert w[0][10] > 200


def test_silence_keeps_rig_mostly_dark():
    devs = {str(i): _wash(base=i * 10) for i in range(5)}
    r = ShowRenderer()
    d = _drct(intent=SILENCE, energy=0.04)
    w = r.render(1.0, d, devs)
    # Intensity is carried by the dimmer channels (0,10,20,30,40). In silence
    # only a couple of ambient washes breathe dimly; the rest are fully dark.
    dimmers = {base: w[0].get(base, 0) for base in (0, 10, 20, 30, 40)}
    lit = [v for v in dimmers.values() if v > 40]
    assert len(lit) <= 2
    # The non-ambient washes (idx >= 2) must be hard zero.
    assert dimmers[20] == 0 and dimmers[30] == 0 and dimmers[40] == 0


def test_build_ramps_with_progress():
    devs = {"0": _wash(base=0)}
    r = ShowRenderer()
    low = r.render(1.0, _drct(intent=BUILD, energy=0.6, build_progress=0.1), devs)
    r2 = ShowRenderer()
    high = r2.render(1.0, _drct(intent=BUILD, energy=0.6, build_progress=0.9), devs)
    assert high[0][0] > low[0][0]


# --------------------------------------------------------------- colour ----

def test_complementary_scheme_splits_hue():
    devs = {"0": _wash(base=0), "1": _wash(base=10)}
    r = ShowRenderer()
    d = _drct(intent=DROP, energy=1.0,
              palette={"scheme": "complementary", "saturation": 1.0, "change_rate": "sharp"})
    w = r.render(1.0, d, devs)
    rgb0 = (w[0][1], w[0][2], w[0][3])
    rgb1 = (w[0][11], w[0][12], w[0][13])
    assert rgb0 != rgb1  # opposite hues → different colours


def test_color_only_bakes_intensity_when_no_dimmer():
    caps = {"has_color": True, "has_dimmer": False, "red_channel": 0,
            "green_channel": 1, "blue_channel": 2, "has_movement": False,
            "strobe_friendly": False}
    devs = {"0": Dev(caps=caps)}
    r = ShowRenderer()
    dark = r.render(1.0, _drct(intent=CALM, energy=0.05), devs)
    bright = ShowRenderer().render(1.0, _drct(intent=DROP, energy=1.0,
              palette={"scheme": "complementary", "saturation": 1.0, "change_rate": "sharp"}), devs)
    sum_dark = sum(dark[0].values())
    sum_bright = sum(bright[0].values())
    assert sum_bright > sum_dark


# ------------------------------------------------------------ movement ----

def test_movement_settles_home_when_calm():
    devs = {"0": _mover(home_pan=128, home_tilt=100)}
    r = ShowRenderer()
    d = _drct(intent=CALM, energy=0.05, beat_in_bar=1, beat_phase=0.0)
    w = r.render(1.0, d, devs)
    # Calm → small deviation around home.
    assert abs(w[0][1] - 128) < 20
    assert abs(w[0][2] - 100) < 20


def test_invert_pan_flips_direction():
    # idx 0, beat_in_bar=1, beat_phase=0 → bar_phase=0.25 → sin=+1, side=+1.
    base = _mover(home_pan=128, invert_pan=False)
    inv = _mover(home_pan=128, invert_pan=True)
    d = _drct(intent=DROP, energy=1.0, beat_in_bar=1, beat_phase=0.0)
    wb = ShowRenderer().render(1.0, d, {"0": base})
    wi = ShowRenderer().render(1.0, d, {"0": inv})
    # Non-inverted swings above home, inverted swings below (mirror).
    assert wb[0][1] > 128
    assert wi[0][1] < 128


def test_energy_increases_movement_amplitude():
    devs = {"0": _mover(home_pan=128)}
    d_lo = _drct(intent=GROOVE, energy=0.2, beat_in_bar=1, beat_phase=0.0)
    d_hi = _drct(intent=GROOVE, energy=1.0, beat_in_bar=1, beat_phase=0.0)
    lo = ShowRenderer().render(1.0, d_lo, devs)
    hi = ShowRenderer().render(1.0, d_hi, devs)
    assert abs(hi[0][1] - 128) > abs(lo[0][1] - 128)


# -------------------------------------------------------------- strobe ----

def test_strobe_focus_opens_over_build():
    devs = {"0": _strobe(base=20)}
    r1 = ShowRenderer()
    early = r1.render(1.0, _drct(intent=BUILD, energy=0.6, build_progress=0.0,
                                 allow_strobe=True), devs)
    r2 = ShowRenderer()
    late = r2.render(1.0, _drct(intent=BUILD, energy=0.9, build_progress=1.0,
                                allow_strobe=True), devs)
    # Focus channel (base+2 = 22) opens up as the build progresses.
    assert late[0][22] > early[0][22]


def test_phrase_variation_avoids_loop():
    # Same intent + energy but different phrase index must NOT render an
    # identical frame (this is the anti-"same thing in a loop" guarantee).
    devs = {str(i): _wash(base=i * 4) for i in range(6)}
    d1 = _drct(intent=GROOVE, energy=0.5, phrase_index=0, beat_in_bar=1, beat_phase=0.2)
    d2 = _drct(intent=GROOVE, energy=0.5, phrase_index=2, beat_in_bar=1, beat_phase=0.2)
    w1 = ShowRenderer().render(1.0, d1, devs)
    w2 = ShowRenderer().render(1.0, d2, devs)
    assert w1 != w2


def test_movement_direction_varies_by_phrase():
    devs = {"0": _mover(home_pan=128), "1": _mover(home_pan=128)}
    d_even = _drct(intent=GROOVE, energy=1.0, phrase_index=0, beat_in_bar=1, beat_phase=0.25)
    d_odd = _drct(intent=GROOVE, energy=1.0, phrase_index=1, beat_in_bar=1, beat_phase=0.25)
    we = ShowRenderer().render(1.0, d_even, devs)
    wo = ShowRenderer().render(1.0, d_odd, devs)
    # Different phrase → different chase pattern → different pan values.
    assert we != wo


def test_strobe_silent_when_not_allowed():
    devs = {"0": _strobe(base=20)}
    r = ShowRenderer()
    w = r.render(1.0, _drct(intent=GROOVE, energy=0.6, allow_strobe=False), devs)
    # Strobe channel (21) should be off when strobe isn't permitted.
    assert w[0].get(21, 0) == 0
