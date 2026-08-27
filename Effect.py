#!/usr/bin/env python3
# Effect.py - small effect/LFO engine for DMXController
#
# Each effect spec is a dict like:
#   {"type":"Sinus","amplitude":60,"phase":"0 > 200","frequency":1.5, ...}
#
# eval_effects(effects, t_s, idx, count) -> normalized value in [-1,1]
# The backend interprets this as an OFFSET added to the base DMX value.

from __future__ import annotations
import json
import math
import os
import re
from typing import Any, Dict, List, Tuple

# --------- Load effect definitions from JSON ----------
def _load_effects_definitions() -> List[Dict[str, Any]]:
    """Load effect definitions from JSON (reloads every time for dev)."""
    try:
        json_path = os.path.join(os.path.dirname(__file__), "effects_definitions.json")
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("effects", [])
    except Exception as e:
        print(f"[Effect.py] Failed to load effects_definitions.json: {e}")
        return []

def list_effects() -> List[Dict[str, Any]]:
    """Return effect metadata for the UI (loaded from JSON)."""
    return _load_effects_definitions()

# --------- helpers ----------
def _to_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip()
        if not s:
            return default
        return float(s)
    except Exception:
        return default

def _parse_phase_field(phase_field: Any, idx: int, count: int) -> float:
    """
    phase_field:
      - number => milliseconds offset
      - "a > b" => spread linearly per selection order (ms)
    returns seconds offset.
    """
    if phase_field is None:
        return 0.0
    if isinstance(phase_field, (int, float)):
        return float(phase_field) / 1000.0
    s = str(phase_field).strip()
    if not s:
        return 0.0
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*>\s*(-?\d+(?:\.\d+)?)\s*$", s)
    if m:
        a = float(m.group(1))
        b = float(m.group(2))
        if count <= 1:
            return a / 1000.0
        ratio = idx / (count - 1)
        ms = a + (b - a) * ratio
        return ms / 1000.0
    return _to_float(s, 0.0) / 1000.0

def _lfo_time(t_s: float, phase_s: float, freq_hz: float) -> Tuple[float, float]:
    """Return (u in [0,1), omega_t)."""
    if freq_hz <= 0:
        return 0.0, 0.0
    omega = 2 * math.pi * freq_hz
    omega_t = omega * (t_s + phase_s)
    u = (freq_hz * (t_s + phase_s)) % 1.0
    return u, omega_t

def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v

# --------- waveform implementations (normalized -1..1) ----------
def _sinus(u: float, omega_t: float) -> float:
    return math.sin(omega_t)

def _triangle(u: float, omega_t: float) -> float:
    return 4 * abs(u - 0.5) - 1

def _sawtooth(u: float, omega_t: float) -> float:
    return 2 * u - 1

def _rectangle(u: float, omega_t: float, duty: float = 0.5) -> float:
    duty = max(0.01, min(0.99, duty))
    return 1.0 if u < duty else -1.0

def _trapezoid(u: float, omega_t: float, rise: float = 0.2, high: float = 0.3, fall: float = 0.2) -> float:
    rise = max(0.0, rise)
    high = max(0.0, high)
    fall = max(0.0, fall)
    total = rise + high + fall
    if total >= 1.0:
        rise, high, fall = rise/total, high/total, fall/total
        total = 1.0
    low = 1.0 - total
    if u < rise:
        return -1.0 + 2.0*(u/rise) if rise>0 else 1.0
    u2 = u - rise
    if u2 < high:
        return 1.0
    u3 = u2 - high
    if u3 < fall:
        return 1.0 - 2.0*(u3/fall) if fall>0 else -1.0
    return -1.0

def _bump(u: float, omega_t: float, width: float = 0.1) -> float:
    width = max(0.01, min(0.99, width))
    if u < width:
        x = u / width
        return math.sin(math.pi * x)
    return 0.0

def _capacitor(u: float, omega_t: float, charge: float = 0.2, discharge: float = 0.2) -> float:
    charge = max(0.01, min(0.99, charge))
    discharge = max(0.01, min(0.99, discharge))
    if u < charge:
        x = u/charge
        return -1.0 + 2.0*(1.0 - math.exp(-5*x))
    if u > 1.0 - discharge:
        x = (u - (1.0 - discharge))/discharge
        return 1.0 - 2.0*(1.0 - math.exp(-5*x))
    return 1.0

def _cardinalsinus(u: float, omega_t: float) -> float:
    x = omega_t
    if abs(x) < 1e-3:
        return 1.0
    return _clamp(math.sin(x)/x * 3.0)

def _pows(u: float, omega_t: float, power: float = 2.0) -> float:
    s = math.sin(omega_t)
    sign = 1.0 if s >= 0 else -1.0
    return sign * (abs(s) ** max(0.1, power))

def _swing(u: float, omega_t: float, hold: float = 0.1) -> float:
    hold = max(0.0, min(0.5, hold))
    s = math.sin(omega_t)
    k = 3.0 + hold*10.0
    return math.tanh(k*s)

def _swing_up(u: float, omega_t: float) -> float:
    return _clamp(_swing(u, omega_t, 0.15)*0.7 + 0.3, -1.0, 1.0)

def _chaser(u: float, _omega_t: float, idx: int = 0, count: int = 1, width: float = 0.2) -> float:
    """Chaser: one lit window of `width` sweeping across `count` devices."""
    width = max(0.01, min(1.0, width))
    if count <= 0:
        return 0.0
    center = u * count
    dist = abs((idx + 0.5) - center)
    on = dist <= (width * count / 2.0)
    return 1.0 if on else -1.0

def _eval_one(effect: Dict[str, Any], t_s: float, idx: int, count: int) -> float:
    etype = str(effect.get("type") or "").strip().lower()
    amp_pct = _to_float(effect.get("amplitude", 100.0), 100.0)
    freq = _to_float(effect.get("frequency", 1.0), 1.0)
    phase_s = _parse_phase_field(effect.get("phase", 0.0), idx, count)

    u, omega_t = _lfo_time(t_s, phase_s, freq)

    if etype in ("sinus", "sine"):
        w = _sinus(u, omega_t)
    elif etype in ("triangle", "tri"):
        w = _triangle(u, omega_t)
    elif etype in ("sawtooth", "saw"):
        w = _sawtooth(u, omega_t)
    elif etype in ("rectangle", "square"):
        duty = _to_float(effect.get("duty", 0.5), 0.5)
        w = _rectangle(u, omega_t, duty=duty)
    elif etype in ("trapezoid", "trap"):
        rise = _to_float(effect.get("rise", 0.2), 0.2)
        high = _to_float(effect.get("high", 0.3), 0.3)
        fall = _to_float(effect.get("fall", 0.2), 0.2)
        w = _trapezoid(u, omega_t, rise=rise, high=high, fall=fall)
    elif etype in ("bump", "pulse"):
        width = _to_float(effect.get("width", 0.1), 0.1)
        w = _bump(u, omega_t, width=width)
    elif etype in ("capacitor", "cap"):
        charge = _to_float(effect.get("charge", 0.2), 0.2)
        discharge = _to_float(effect.get("discharge", 0.2), 0.2)
        w = _capacitor(u, omega_t, charge=charge, discharge=discharge)
    elif etype in ("cardinalsinus", "cardinal"):
        w = _cardinalsinus(u, omega_t)
    elif etype in ("pows", "pow"):
        power = _to_float(effect.get("power", 2.0), 2.0)
        w = _pows(u, omega_t, power=power)
    elif etype in ("swing",):
        hold = _to_float(effect.get("hold", 0.1), 0.1)
        w = _swing(u, omega_t, hold=hold)
    elif etype in ("swing up", "swing_up", "swingup"):
        w = _swing_up(u, omega_t)
    elif etype in ("chaser", "chase"):
        width = _to_float(effect.get("width", 0.2), 0.2)
        w = _chaser(u, omega_t, idx=idx, count=count, width=width)
    else:
        w = 0.0

    amp = max(-1.0, min(1.0, amp_pct / 100.0))
    return _clamp(w * amp)

def eval_effects(effects: Any, t_s: float, idx: int = 0, count: int = 1) -> float:
    """
    effects: list[effectSpec] or single spec.
    returns summed normalized output clamped to [-1,1].
    """
    if effects is None:
        return 0.0
    if isinstance(effects, dict):
        effects_list = [effects]
    elif isinstance(effects, list):
        effects_list = [e for e in effects if isinstance(e, dict)]
    else:
        return 0.0

    out = 0.0
    for e in effects_list:
        out += _eval_one(e, t_s, idx=idx, count=count)
    return _clamp(out)
