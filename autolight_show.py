"""autolight_show.py — the show layer: turns a Directive into DMX values.

Consumes the brain's :class:`~autolight_brain.Directive` plus the rig (device
capabilities + pan/tilt calibration) and the spatial topology, and produces a
per-fixture set of channel writes. Implements the visual vocabulary from the
interview (RELEASE-0.4.0-SPEC.md §2.4-2.6):

  * Hybrid fixture roles (strobe / mover / wash / accent), overridable on peaks.
  * Active-subset-by-energy (few fixtures when calm, whole rig on the drop).
  * Dimmer driven by energy + kick groove + downbeat impact + build ramp.
  * Colour from the palette hint: hue (optionally from musical key), harmony
    scheme (analogous calm → complementary peak), saturation ∝ energy.
  * Movement returns to a per-fixture "audience" home position when calm,
    oscillates on energy; mirror pairs move antisymmetrically; invert flags
    handle upside-down / differently-oriented fixtures.
  * Strobe focus opens progressively over the build.

Pure Python + math. Testable with lightweight fake devices (any object exposing
``universe``, ``capabilities``, ``attr_map`` and the calibration attributes).
``render()`` returns ``{universe: {channel: value}}`` so it can be unit-tested
without the engine, then applied into the live universe buffers by the overlay.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from autolight_brain import Directive, SILENCE, CALM, GROOVE, BUILD, DROP, RELEASE

ROLE_STROBE = "strobe"
ROLE_MOVER = "mover"
ROLE_WASH = "wash"
ROLE_ACCENT = "accent"

# Ambient priority: which roles stay lit when energy is low (contrast). Lower =
# lit earlier (kept on in calm sections).
_ROLE_PRIORITY = {ROLE_WASH: 0, ROLE_MOVER: 1, ROLE_ACCENT: 2, ROLE_STROBE: 3}

# Pitch-class → hue (degrees) so colour can follow the musical key when known.
_KEY_HUE = {
    "c": 0, "g": 30, "d": 60, "a": 90, "e": 120, "b": 150,
    "f#": 180, "gb": 180, "db": 210, "c#": 210, "ab": 240, "g#": 240,
    "eb": 270, "d#": 270, "bb": 300, "a#": 300, "f": 330,
}

_MAX_PAN_AMP = 70.0   # DMX units of pan swing at full energy
_MAX_TILT_AMP = 45.0


def hsv_to_rgb(h: float, s: float, v: float) -> tuple:
    """h in [0,360), s,v in [0,1] → (r,g,b) 0-255."""
    h = h % 360.0
    c = v * s
    x = c * (1 - abs((h / 60.0) % 2 - 1))
    m = v - c
    if h < 60:
        r, g, b = c, x, 0
    elif h < 120:
        r, g, b = x, c, 0
    elif h < 180:
        r, g, b = 0, c, x
    elif h < 240:
        r, g, b = 0, x, c
    elif h < 300:
        r, g, b = x, 0, c
    else:
        r, g, b = c, 0, x
    return (int(round((r + m) * 255)), int(round((g + m) * 255)), int(round((b + m) * 255)))


def _clamp8(v: float) -> int:
    return max(0, min(255, int(round(v))))


def _attr_channel(attr_map: Dict[str, int], *needles: str) -> Optional[int]:
    """Find an absolute channel whose attr key contains any of ``needles``."""
    if not isinstance(attr_map, dict):
        return None
    for key, ch in attr_map.items():
        k = str(key).lower()
        if any(n in k for n in needles):
            try:
                return int(ch)
            except Exception:
                continue
    return None


def assign_roles(devices: Dict[str, Any]) -> Dict[str, str]:
    """Hybrid base role per fixture, derived from its capabilities."""
    roles: Dict[str, str] = {}
    for dev_id, dev in devices.items():
        caps = getattr(dev, "capabilities", None) or {}
        if caps.get("strobe_friendly"):
            roles[str(dev_id)] = ROLE_STROBE
        elif caps.get("has_movement"):
            roles[str(dev_id)] = ROLE_MOVER
        elif caps.get("has_color"):
            roles[str(dev_id)] = ROLE_WASH
        else:
            roles[str(dev_id)] = ROLE_ACCENT
    return roles


class ShowRenderer:
    """Stateless-ish renderer (only caches role assignment per rig)."""

    def __init__(self) -> None:
        self._roles: Dict[str, str] = {}
        self._order: List[str] = []
        self._hue_phase: float = 200.0  # current base hue, evolves over time
        self._last_now: Optional[float] = None
        self._phrase: int = 0            # current phrase index (drives variation)

    def on_rig_changed(self, devices: Dict[str, Any], topo: Any = None) -> None:
        self._roles = assign_roles(devices)
        if topo is not None and getattr(topo, "order_by_x", None):
            self._order = list(topo.order_by_x)
        else:
            self._order = sorted(devices.keys())

    # ------------------------------------------------------------- render
    def render(self, now: float, directive: Directive, devices: Dict[str, Any],
               topo: Any = None) -> Dict[int, Dict[int, int]]:
        if not devices:
            return {}
        if not self._roles or set(self._roles) != set(map(str, devices)):
            self.on_rig_changed(devices, topo)

        self._advance_hue(now, directive)
        n = len(self._order) or len(devices)
        # Phrase-driven variation: the look evolves every phrase instead of
        # looping identically at steady energy (hue family, active subset and
        # movement direction all rotate with the phrase index).
        self._phrase = int(getattr(directive, "phrase_index", 0) or 0)
        base_hue = (self._hue_for(directive) + self._phrase * 47.0) % 360.0

        writes: Dict[int, Dict[int, int]] = {}
        for idx, dev_id in enumerate(self._order):
            dev = devices.get(dev_id)
            if dev is None:
                continue
            role = self._roles.get(dev_id, ROLE_ACCENT)
            active = self._is_active(role, idx, n, directive)
            uni = int(getattr(dev, "universe", 0) or 0)
            uw = writes.setdefault(uni, {})
            self._render_fixture(uw, dev, dev_id, idx, n, role, active,
                                 directive, base_hue, now)
        return writes

    # ----------------------------------------------------------- per-fixture
    def _render_fixture(self, uw, dev, dev_id, idx, n, role, active,
                        d: Directive, base_hue, now) -> None:
        caps = getattr(dev, "capabilities", None) or {}
        attr_map = getattr(dev, "attr_map", None) or {}

        level = self._dimmer_level(d, role, idx, n, active, now) if active else 0.0

        # --- Strobe role / strobe channel --------------------------------
        if role == ROLE_STROBE:
            strobe_ch = _attr_channel(attr_map, "strobe", "shutter")
            focus_ch = _attr_channel(attr_map, "focus", "zoom")
            if d.allow_strobe and active:
                # Square-wave gate ~12 Hz; brighter on the downbeat impact.
                rate = 12.0
                gate = 1.0 if (math.sin(now * rate * math.pi) > 0) else 0.0
                level = 255.0 * gate
                if strobe_ch is not None:
                    uw[strobe_ch] = _clamp8(200)
            else:
                if strobe_ch is not None:
                    uw[strobe_ch] = 0
            if focus_ch is not None:
                # Focus opens progressively over the build, then wide on drop.
                if d.intent == BUILD:
                    uw[focus_ch] = _clamp8(60 + 195 * d.build_progress)
                elif d.intent in (DROP, RELEASE):
                    uw[focus_ch] = 255
                else:
                    uw[focus_ch] = _clamp8(40)

        # --- Colour -------------------------------------------------------
        if caps.get("has_color"):
            hue = self._fixture_hue(base_hue, idx, n, dev, d)
            sat = float(d.palette.get("saturation", 0.8))
            r_ch = caps.get("red_channel")
            g_ch = caps.get("green_channel")
            b_ch = caps.get("blue_channel")
            has_dimmer = caps.get("has_dimmer")
            # Active fixture with a separate dimmer: colour stays full, the
            # dimmer carries intensity. Otherwise (no dimmer, or inactive) bake
            # the level into the RGB value so inactive fixtures go truly dark.
            val = 1.0 if (has_dimmer and active) else (level / 255.0)
            r, g, b = hsv_to_rgb(hue, sat, val)
            if r_ch is not None:
                uw[int(r_ch)] = r
            if g_ch is not None:
                uw[int(g_ch)] = g
            if b_ch is not None:
                uw[int(b_ch)] = b

        # --- Dimmer -------------------------------------------------------
        dim_ch = caps.get("dimmer_channel")
        if dim_ch is not None:
            uw[int(dim_ch)] = _clamp8(level)

        # --- Movement -----------------------------------------------------
        if caps.get("has_movement"):
            self._render_movement(uw, dev, idx, n, role, d, now, active)

    def _render_movement(self, uw, dev, idx, n, role, d: Directive, now, active) -> None:
        caps = getattr(dev, "capabilities", None) or {}
        pan_ch = caps.get("pan_channel")
        tilt_ch = caps.get("tilt_channel")
        home_pan = getattr(dev, "home_pan", None)
        home_tilt = getattr(dev, "home_tilt", None)
        hp = 128 if home_pan is None else int(home_pan)
        ht = 128 if home_tilt is None else int(home_tilt)
        inv_p = -1.0 if getattr(dev, "invert_pan", False) else 1.0
        inv_t = -1.0 if getattr(dev, "invert_tilt", False) else 1.0

        # Calm → settle toward the audience home; energy → oscillate.
        amp = d.energy if d.intent not in (SILENCE, CALM) else (d.energy * 0.25)
        pan_amp = _MAX_PAN_AMP * amp
        tilt_amp = _MAX_TILT_AMP * amp
        # Phase: spread across the rig (chase) + bar-level oscillation. The
        # spread span and chase direction rotate with the phrase so the spatial
        # movement pattern changes phrase-to-phrase (1, 2 or 3 lobes; L→R / R→L).
        bar_phase = (d.beat_in_bar + d.beat_phase) / 4.0 if d.beat_in_bar >= 0 else 0.0
        lobes = 1.0 + (self._phrase % 3)
        chase_dir = 1.0 if (self._phrase % 2 == 0) else -1.0
        spread = (idx / max(1, n)) * 2.0 * math.pi * lobes * chase_dir
        wave = math.sin(2.0 * math.pi * bar_phase + spread)
        # Mirror pairs: right side moves opposite.
        side = -1.0 if (idx % 2 == 1) else 1.0

        if pan_ch is not None:
            uw[int(pan_ch)] = _clamp8(hp + inv_p * side * pan_amp * wave)
        if tilt_ch is not None:
            cosw = math.cos(2.0 * math.pi * bar_phase + spread)
            uw[int(tilt_ch)] = _clamp8(ht + inv_t * tilt_amp * cosw)

    # ----------------------------------------------------------- helpers
    def _is_active(self, role, idx, n, d: Directive) -> bool:
        if d.intent == SILENCE:
            # Keep one or two ambient washes breathing.
            return _ROLE_PRIORITY.get(role, 3) == 0 and idx < 2
        if role == ROLE_STROBE and not d.allow_strobe:
            return False
        # Activation threshold spread by ambient priority + order so low energy
        # lights few fixtures (contrast), full energy lights the whole rig. The
        # order index is rotated by the phrase so a *different* subset stays lit
        # each phrase (avoids the same fixtures always being the ambient ones).
        prio = _ROLE_PRIORITY.get(role, 2)
        rot = (idx + self._phrase * 3) % max(1, n)
        threshold = (prio * 0.18) + (rot / max(1, n)) * 0.35
        return d.energy >= threshold * 0.9

    def _dimmer_level(self, d: Directive, role, idx, n, active, now) -> float:
        base = d.energy * 255.0
        # Downbeat impact: full punch on the "1" of a drop.
        if d.want_impact and role in (ROLE_WASH, ROLE_ACCENT, ROLE_STROBE):
            return 255.0
        # Kick groove: pulse on the beat for drops.
        if d.groove_on_kick:
            env = max(0.0, 1.0 - (d.beat_phase * 1.6))  # bright at beat, decays
            base = 255.0 * (0.45 + 0.55 * env) * d.energy
        # Build ramp.
        if d.intent == BUILD:
            base = 255.0 * (0.35 + 0.65 * d.build_progress) * max(0.4, d.energy)
        # Calm breathing.
        if d.intent in (SILENCE, CALM):
            breathe = 0.5 + 0.5 * math.sin(now * 0.7 + idx)
            base = 255.0 * d.energy * (0.5 + 0.5 * breathe)
        return base

    def _advance_hue(self, now: float, d: Directive) -> None:
        if self._last_now is None:
            self._last_now = now
        dt = max(0.0, now - self._last_now)
        self._last_now = now
        # Slow drift when calm, faster shifts when energetic.
        rate = 4.0 if d.palette.get("change_rate") == "sharp" else 0.8
        self._hue_phase = (self._hue_phase + rate * dt * 6.0) % 360.0

    def _hue_for(self, d: Directive) -> float:
        key = str(d.palette.get("musical_key", "")).strip().lower()
        if key:
            root = key.split()[0] if " " in key else key.rstrip("m")
            root = root.replace("min", "").replace("maj", "").strip()
            if root in _KEY_HUE:
                return float(_KEY_HUE[root])
        return self._hue_phase

    def _fixture_hue(self, base_hue, idx, n, dev, d: Directive) -> float:
        scheme = d.palette.get("scheme", "analogous")
        if scheme == "complementary":
            # Split the rig: left side base, right side complement.
            side = idx % 2
            return (base_hue + (180.0 if side else 0.0)) % 360.0
        if scheme == "warm_analogous":
            return (base_hue + (idx / max(1, n)) * 30.0) % 360.0
        # analogous: gentle spread across the rig.
        return (base_hue + (idx / max(1, n)) * 50.0 - 25.0) % 360.0
