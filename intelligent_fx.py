#!/usr/bin/env python3
"""
intelligent_fx.py - Intelligent effects engine (Python port of UI JS effects)
"""

from __future__ import annotations

import json
import math
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from runtime_paths import DATA_DIR

INTELLIGENT_EFFECTS_DIR = os.path.join(DATA_DIR, "intelligent_effects")


def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def normalize_targets(targets: Any) -> List[str]:
    if targets is None:
        return []
    raw_list = targets if isinstance(targets, list) else [targets]
    out: List[str] = []
    for raw in raw_list:
        if raw is None:
            continue
        key = str(raw).strip().lower()
        if not key:
            continue
        if key in ("color", "rgb"):
            out.extend(["r", "g", "b"])
        else:
            out.append(key)
    return list(dict.fromkeys(out))


def _group_index_for_device(group: Dict[str, Any], device_id: str) -> Tuple[int, int]:
    effect_members = group.get("effect_member_ids") or group.get("effectMemberIds") or []
    if isinstance(effect_members, list) and effect_members:
        order = [str(x) for x in effect_members]
        idx = order.index(str(device_id)) if str(device_id) in order else 0
        total = len(order) if order else 1
        return idx, total

    order = [str(x) for x in group.get("deviceIds") or group.get("device_ids") or []]
    selection_groups = group.get("selection_groups")
    if isinstance(selection_groups, list) and selection_groups:
        for gi, grp in enumerate(selection_groups):
            if isinstance(grp, list) and str(device_id) in [str(x) for x in grp]:
                return gi, len(selection_groups)
        idx = order.index(str(device_id)) if str(device_id) in order else 0
        return idx, max(len(selection_groups), 1)
    idx = order.index(str(device_id)) if str(device_id) in order else 0
    total = len(order) if order else 1
    return idx, total


def phase_offset_ms(group: Dict[str, Any], device_id: str) -> float:
    ph = str(group.get("phase", "0")).strip()
    idx, n = _group_index_for_device(group, device_id)

    op = None
    parts: List[float] = []

    if "><" in ph:
        op = "><"
        parts = [float(x.strip() or 0) for x in ph.split("><")]
    elif "<>" in ph:
        op = "<>"
        parts = [float(x.strip() or 0) for x in ph.split("<>")]
    elif "||" in ph:
        op = "||"
        parts = [float(x.strip() or 0) for x in ph.split("||")]
    elif "|" in ph:
        op = "|"
        parts = [float(x.strip() or 0) for x in ph.split("|")]
    elif ">" in ph:
        op = ">"
        parts = [float(x.strip() or 0) for x in ph.split(">")]
    elif "<" in ph:
        op = "<"
        parts = [float(x.strip() or 0) for x in ph.split("<")]
    elif "?" in ph:
        op = "?"
        parts = [float(x.strip() or 0) for x in ph.split("?")]

    if not op or len(parts) < 2:
        try:
            return float(ph)
        except Exception:
            return 0.0

    base_ms = parts[0] if parts else 0.0
    spread_ms = parts[1] if len(parts) > 1 else 0.0

    if n <= 1:
        return base_ms

    rank = 0
    denom = max(n - 1, 1)

    if op == ">":
        rank = idx
    elif op == "<":
        rank = n - 1 - idx
    elif op == "><":
        rank = idx if idx < n / 2 else (n - 1 - idx)
    elif op == "<>":
        mid = (n - 1) / 2.0
        dist = abs(idx - mid)
        max_dist = mid
        rank = int(round((1 - (dist / max_dist)) * (n - 1))) if max_dist > 0 else 0
    elif op == "|":
        rank = idx % 2
        denom = 1
    elif op == "||":
        rank = 0 if idx < n / 2 else 1
        denom = 1
    elif op == "?":
        rank = int(math.floor((((idx * 1234567) % (n * 100)) / 100.0) * n)) % n
    else:
        rank = idx

    return base_ms + (spread_ms * rank) / denom


def tri_wave(x: float) -> float:
    return (x * 4 - 1) if x < 0.5 else (3 - x * 4)


def saw_wave(x: float) -> float:
    return x * 2 - 1


def sqr_wave(x: float) -> float:
    return 1.0 if x < 0.5 else -1.0


def apply_fade_curve(t: float, curve: str) -> float:
    curve = (curve or "linear").lower()
    if curve == "linear":
        return t
    if curve == "easein":
        return t * t
    if curve == "easeout":
        return 1 - (1 - t) * (1 - t)
    if curve == "easeinout":
        return t * t * (3 - 2 * t)
    if curve == "snap":
        return 1.0 if t > 0.5 else 0.0
    if curve == "smooth":
        return t * t * t * (t * (t * 6 - 15) + 10)
    return t


def wave(kind: str, t_ms: float, freq: float, phase_ms: float = 0.0) -> float:
    f = max(0.0, float(freq or 0.0))
    if f <= 0:
        return 0.0
    w = ((t_ms + float(phase_ms or 0.0)) / 1000.0) * f
    frac = w - math.floor(w)
    key = str(kind or "sinus").lower()
    if key == "triangle":
        return tri_wave(frac)
    if key == "sawtooth":
        return saw_wave(frac)
    if key in ("rectangle", "square"):
        return sqr_wave(frac)
    return math.sin(2 * math.pi * frac)


def hsv_to_rgb(h: float, s: float, v: float) -> Dict[str, int]:
    hh = ((h % 360.0) + 360.0) % 360.0
    ss = clamp(s, 0.0, 1.0)
    vv = clamp(v, 0.0, 1.0)
    c = vv * ss
    x = c * (1 - abs(((hh / 60.0) % 2) - 1))
    m = vv - c
    r1 = g1 = b1 = 0.0
    if hh < 60:
        r1, g1, b1 = c, x, 0.0
    elif hh < 120:
        r1, g1, b1 = x, c, 0.0
    elif hh < 180:
        r1, g1, b1 = 0.0, c, x
    elif hh < 240:
        r1, g1, b1 = 0.0, x, c
    elif hh < 300:
        r1, g1, b1 = x, 0.0, c
    else:
        r1, g1, b1 = c, 0.0, x
    return {
        "r": int(round((r1 + m) * 255)),
        "g": int(round((g1 + m) * 255)),
        "b": int(round((b1 + m) * 255)),
    }


def hue_to_rgb_with_bw(h: float, s: float, v: float) -> Dict[str, int]:
    hh = float(h) if h is not None else float("nan")
    ss = clamp(s, 0.0, 1.0)
    vv = clamp(v, 0.0, 1.0)
    if not math.isfinite(hh):
        return {"r": 0, "g": 0, "b": 0}
    if hh <= 0:
        return {"r": 0, "g": 0, "b": 0}
    if hh >= 360:
        w = int(round(255 * vv))
        return {"r": w, "g": w, "b": w}
    return hsv_to_rgb(hh, ss, vv)



def _map_chaser_step(base_step: int, num_steps: int, play_mode: str, cycle_num: int, group_id: str) -> int:
    effective_step = base_step
    mode = str(play_mode or "normal").lower()

    if mode == "reverse":
        effective_step = num_steps - 1 - base_step
    elif mode == "bounce":
        if cycle_num % 2 == 1:
            effective_step = num_steps - 1 - base_step
    elif mode == "in":
        half = int(math.ceil(num_steps / 2.0))
        effective_step = base_step if base_step < half else num_steps - 1 - (base_step - half)
    elif mode == "out":
        half = int(math.ceil(num_steps / 2.0))
        center = int(math.floor(num_steps / 2.0))
        if base_step < half:
            effective_step = center - base_step
        else:
            effective_step = center + (base_step - half + 1)
        effective_step = max(0, min(effective_step, num_steps - 1))
    elif mode == "inout":
        quarter = int(math.ceil(num_steps / 4.0))
        phase = int(math.floor(base_step / max(quarter, 1))) % 4
        pos_in_phase = base_step % max(quarter, 1)
        if phase == 0:
            effective_step = pos_in_phase
        elif phase == 1:
            effective_step = num_steps - 1 - pos_in_phase
        elif phase == 2:
            effective_step = num_steps - 1 - pos_in_phase
        else:
            effective_step = pos_in_phase
    elif mode == "random":
        key = f"{group_id}:{num_steps}"
        if key not in _RANDOM_SEEDS:
            seq = list(range(num_steps))
            _rand_shuffle(seq, seed=hash(key))
            _RANDOM_SEEDS[key] = seq
        seq = _RANDOM_SEEDS[key]
        effective_step = seq[base_step % num_steps]
    elif mode == "switch":
        effective_step = base_step // 2 if base_step % 2 == 0 else num_steps - 1 - (base_step // 2)

    return max(0, min(effective_step, num_steps - 1))


_RANDOM_SEEDS: Dict[str, List[int]] = {}


def _rand_shuffle(seq: List[int], seed: int) -> None:
    x = seed & 0xFFFFFFFF
    for i in range(len(seq) - 1, 0, -1):
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        j = x % (i + 1)
        seq[i], seq[j] = seq[j], seq[i]


def chaser_edge_fade(ctx: Dict[str, Any], params: Dict[str, Any]) -> float:
    device_count = max(1, int(ctx.get("device_count") or 1))
    device_index = max(0, int(ctx.get("device_index") or 0))
    t_ms = float(ctx.get("t_ms") or 0.0)
    group_id = str(ctx.get("group", {}).get("id") or ctx.get("effect", {}).get("id") or "chaser")

    fade_ms = max(0.0, float(params.get("fade", 100) or 0))
    duration = max(0.0, float(params.get("duration", 0) or 0))
    size = max(1, int(params.get("size", 1) or 1))
    step_size = max(1, int(params.get("stepSize", 1) or 1))
    break_step = int(params.get("breakStep", 0) or 0)
    break_size = max(0.0, float(params.get("breakSize", 500) or 0))
    play_mode = params.get("playMode", "Normal")

    step_duration = fade_ms + duration + fade_ms
    if step_duration <= 0:
        return 0.0

    effective_size = min(size, device_count)
    num_steps = 1 if effective_size >= device_count else max(1, int(math.ceil((device_count - effective_size + 1) / step_size)))

    breaks_count = int(math.floor((num_steps - 1) / break_step)) if break_step > 0 else 0
    total_break_time = breaks_count * break_size
    cycle_duration = (num_steps * step_duration) + total_break_time
    if cycle_duration <= 0:
        return 0.0

    cycle_num = int(math.floor(t_ms / cycle_duration))
    cycle_time = t_ms % cycle_duration

    current_step = 0
    acc_time = 0.0
    for s in range(num_steps):
        next_time = acc_time + step_duration
        if cycle_time < next_time:
            current_step = s
            break
        acc_time = next_time
        if break_step > 0 and (s + 1) % break_step == 0:
            if cycle_time < acc_time + break_size:
                return 0.0
            acc_time += break_size
        current_step = s + 1
    current_step = min(current_step, num_steps - 1)

    step_start_time = 0.0
    for s in range(current_step):
        step_start_time += step_duration
        if break_step > 0 and (s + 1) % break_step == 0:
            step_start_time += break_size
    time_in_step = cycle_time - step_start_time

    max_start = max(0, device_count - effective_size)

    def start_pos_for(base_step: int, cycle_offset: int = 0) -> int:
        effective_step = _map_chaser_step(base_step, num_steps, play_mode, cycle_num + cycle_offset, group_id)
        start_pos = effective_step * step_size
        return max(0, min(start_pos, max_start))

    def is_in_window(idx: int, start_pos: int) -> bool:
        return idx >= start_pos and idx < start_pos + effective_size

    had_break_before = break_step > 0 and current_step > 0 and current_step % break_step == 0
    break_after = break_step > 0 and (current_step + 1) % break_step == 0

    prev_step = current_step - 1
    prev_cycle_offset = 0
    if prev_step < 0:
        prev_step = num_steps - 1
        prev_cycle_offset = -1

    next_step = current_step + 1
    next_cycle_offset = 0
    if next_step >= num_steps:
        next_step = 0
        next_cycle_offset = 1

    cur_start = start_pos_for(current_step)
    prev_start = None if had_break_before else start_pos_for(prev_step, prev_cycle_offset)
    next_start = None if break_after else start_pos_for(next_step, next_cycle_offset)

    cur_on = 1.0 if is_in_window(device_index, cur_start) else 0.0
    prev_on = 0.0 if prev_start is None else (1.0 if is_in_window(device_index, prev_start) else 0.0)
    next_on = 0.0 if next_start is None else (1.0 if is_in_window(device_index, next_start) else 0.0)

    if fade_ms <= 0:
        return cur_on

    if time_in_step < fade_ms:
        t = time_in_step / fade_ms
        t = apply_fade_curve(t, params.get("fadeCurve", "Linear"))
        return clamp(prev_on * (1 - t) + cur_on * t, 0.0, 1.0)

    if time_in_step < fade_ms + duration:
        return cur_on

    t = (time_in_step - (fade_ms + duration)) / fade_ms
    t = apply_fade_curve(t, params.get("fadeCurve", "Linear"))
    return clamp(cur_on * (1 - t) + next_on * t, 0.0, 1.0)


def _ctx_fields(ctx: Dict[str, Any]) -> Tuple[Dict[str, Any], float, int, int, str, str]:
    params = ctx.get("params") or ctx.get("group") or {}
    t_ms = float(ctx.get("t_ms") or 0.0)
    device_index = int(ctx.get("device_index") or 0)
    device_count = max(1, int(ctx.get("device_count") or 1))
    device_id = str(ctx.get("member_id") or ctx.get("device_id") or "")
    target = str(ctx.get("target") or "")
    return params, t_ms, device_index, device_count, device_id, target


def _apply_intelligent_value(defn: Dict[str, Any], base: int, raw: float, scale: float = 1.0) -> int:
    mode = str(defn.get("mode", "delta")).lower()
    if not math.isfinite(raw):
        return int(clamp(float(base), 0.0, 255.0))
    if mode == "absolute":
        raw_val = clamp(round(float(raw)), 0.0, 255.0)
        return int(clamp(round(base + (raw_val - base) * scale), 0.0, 255.0))
    return int(clamp(round(base + raw * scale), 0.0, 255.0))


def _param_default(defn: Dict[str, Any], key: str, fallback: Any = 0) -> Any:
    for param in defn.get("params") or []:
        if str(param.get("key") or "").strip() == str(key):
            return param.get("default", fallback)
    return fallback


def _get_param_number(params: Dict[str, Any], defn: Dict[str, Any], key: str, fallback: float) -> float:
    raw = params.get(key, _param_default(defn, key, fallback))
    try:
        return float(raw)
    except Exception:
        return float(fallback)


def _normalize_runtime_effect(defn: Dict[str, Any], file: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if not isinstance(defn, dict):
        return None
    effect_id = str(defn.get("id") or defn.get("name") or defn.get("label") or "").strip().lower()
    if not effect_id:
        return None
    runtime = defn.get("runtime")
    if not isinstance(runtime, dict) or not runtime.get("kind"):
        return None
    return {
        **defn,
        "id": effect_id,
        "label": defn.get("label") or defn.get("name") or effect_id,
        "targets": normalize_targets(defn.get("targets") or ["dimmer"]),
        "params": defn.get("params") if isinstance(defn.get("params"), list) else [],
        "mode": str(defn.get("mode") or "delta").lower(),
        "runtime": dict(runtime),
        "file": file or defn.get("file"),
    }


_IMPORTED_EFFECT_CACHE: Dict[str, Any] = {
    "stamp": 0.0,
    "signature": None,
    "effects": [],
    "by_id": {},
}


def _load_imported_runtime_effects(force: bool = False) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    now = time.monotonic()
    if not force and now - float(_IMPORTED_EFFECT_CACHE.get("stamp") or 0.0) < 0.5:
        return _IMPORTED_EFFECT_CACHE["effects"], _IMPORTED_EFFECT_CACHE["by_id"]

    files: List[Tuple[str, int, int]] = []
    try:
        for name in os.listdir(INTELLIGENT_EFFECTS_DIR):
            if not str(name).lower().endswith(".json"):
                continue
            path = os.path.join(INTELLIGENT_EFFECTS_DIR, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            files.append((name, int(stat.st_mtime_ns), int(stat.st_size)))
    except OSError:
        files = []

    signature = tuple(sorted(files))
    if not force and signature == _IMPORTED_EFFECT_CACHE.get("signature"):
        _IMPORTED_EFFECT_CACHE["stamp"] = now
        return _IMPORTED_EFFECT_CACHE["effects"], _IMPORTED_EFFECT_CACHE["by_id"]

    imported: List[Dict[str, Any]] = []
    by_id: Dict[str, Dict[str, Any]] = {}
    for name, _mtime_ns, _size in sorted(files):
        path = os.path.join(INTELLIGENT_EFFECTS_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception:
            continue
        normalized = _normalize_runtime_effect(raw, file=name)
        if not normalized:
            continue
        by_id[normalized["id"]] = normalized

    imported = list(by_id.values())
    imported.sort(key=lambda item: str(item.get("label") or item.get("id") or ""))
    _IMPORTED_EFFECT_CACHE.update({
        "stamp": now,
        "signature": signature,
        "effects": imported,
        "by_id": by_id,
    })
    return imported, by_id


def _eval_runtime_effect(defn: Dict[str, Any], ctx: Dict[str, Any]) -> float:
    runtime = defn.get("runtime") or {}
    kind = str(runtime.get("kind") or "").strip().lower()
    params, t_ms, device_index, device_count, _device_id, target = _ctx_fields(ctx)

    if kind == "split_toggle":
        allowed_targets = normalize_targets(runtime.get("targets") or defn.get("targets") or ["dimmer"])
        if target not in allowed_targets:
            return 0.0

        intensity_key = str(runtime.get("intensity_param") or "dimmer")
        ratio_key = str(runtime.get("ratio_param") or "ratio")
        on_key = str(runtime.get("on_param") or "on_ms")
        off_key = str(runtime.get("off_param") or "off_ms")

        intensity = clamp(_get_param_number(params, defn, intensity_key, 255.0), 0.0, 255.0)
        ratio = clamp(_get_param_number(params, defn, ratio_key, 0.5), 0.0, 1.0)
        on_ms = max(0.0, _get_param_number(params, defn, on_key, 500.0))
        off_ms = max(0.0, _get_param_number(params, defn, off_key, 500.0))
        cycle_ms = on_ms + off_ms

        if device_count <= 1:
            if cycle_ms <= 0:
                return round(intensity)
            active = (t_ms % cycle_ms) < on_ms if on_ms > 0 else False
            return round(intensity if active else 0.0)

        split_count = int(round(device_count * ratio))
        split_count = max(0, min(device_count, split_count))
        in_top_group = device_index < split_count

        if cycle_ms <= 0:
            active = in_top_group
        else:
            cycle_pos = t_ms % cycle_ms
            top_active = cycle_pos < on_ms if on_ms > 0 else False
            bottom_active = cycle_pos >= on_ms
            active = top_active if in_top_group else bottom_active
        return round(intensity if active else 0.0)

    if kind == "pulse_toggle":
        allowed_targets = normalize_targets(runtime.get("targets") or defn.get("targets") or ["dimmer"])
        if target not in allowed_targets:
            return 0.0

        intensity_key = str(runtime.get("intensity_param") or "dimmer")
        ratio_key = str(runtime.get("ratio_param") or "ratio")
        on_key = str(runtime.get("on_param") or "on_ms")
        off_key = str(runtime.get("off_param") or "off_ms")

        high_level = clamp(_get_param_number(params, defn, intensity_key, 255.0), 0.0, 255.0)
        low_ratio = clamp(_get_param_number(params, defn, ratio_key, 0.0), 0.0, 1.0)
        on_ms = max(0.0, _get_param_number(params, defn, on_key, 500.0))
        off_ms = max(0.0, _get_param_number(params, defn, off_key, 500.0))
        low_level = round(high_level * low_ratio)
        cycle_ms = on_ms + off_ms

        if cycle_ms <= 0:
            return round(high_level)
        cycle_pos = t_ms % cycle_ms
        is_high = cycle_pos < on_ms if on_ms > 0 else False
        return round(high_level if is_high else low_level)

    return 0.0


# ---------------------------------------------------------------------------
# Effect implementations (port from JS)
# ---------------------------------------------------------------------------


def _fx_breathing(ctx: Dict[str, Any]) -> float:
    params, t_ms, device_index, device_count, device_id, target = _ctx_fields(ctx)
    phase_ms = phase_offset_ms(params, device_id)
    t_ms += phase_ms

    speed = float(params.get("speed", 0) or 0)
    color_mode_raw = str(params.get("colorMode", "Single color"))
    color_mode = "alternate" if "altern" in color_mode_raw.lower() else "single"
    alt_mode_raw = str(params.get("alternateMode", "Each full cycle"))
    alt_mode = "half" if "half" in alt_mode_raw.lower() else "cycle"
    hue_a = float(params.get("hue", 0) or 0)
    hue_b = float(params.get("hue2", 0) or 0)
    sat = clamp(float(params.get("saturation", 0) or 0), 0.0, 1.0)
    min_val = clamp(float(params.get("min", 0) or 0), 0.0, 255.0)
    max_val = clamp(float(params.get("max", 255) or 0), 0.0, 255.0)
    span = max(0.0, max_val - min_val)

    w = wave("sinus", t_ms, speed)
    level = w * 0.5 + 0.5
    intensity = min_val + span * level
    val = intensity / 255.0

    def clamp01(x: float) -> float:
        return 0.0 if x < 0 else 1.0 if x > 1 else x

    def smoothstep(a: float, b: float, x: float) -> float:
        t = clamp01((x - a) / (b - a))
        return t * t * (3 - 2 * t)

    def lerp_hue(a: float, b: float, t: float) -> float:
        d = ((b - a + 540) % 360) - 180
        return (a + d * t + 360) % 360

    hue = hue_a
    if color_mode == "alternate" and speed > 0:
        period_ms = 1000.0 / speed
        u = (t_ms / period_ms) + 0.25
        cycle_idx = int(math.floor(u))
        p = u - cycle_idx
        fade_ms = 200.0
        fade = min(0.25, fade_ms / period_ms)

        if alt_mode == "cycle":
            cur = hue_a if cycle_idx % 2 == 0 else hue_b
            prev = hue_a if (cycle_idx - 1) % 2 == 0 else hue_b
            if p < fade:
                t = smoothstep(0, fade, p)
                hue = lerp_hue(prev, cur, t)
            else:
                hue = cur
        else:
            mid = 0.5
            a_start = max(0.0, mid - fade)
            a_end = min(1.0, mid + fade)
            if p < a_start:
                hue = hue_a
            elif p > a_end:
                hue = hue_b
            else:
                t = smoothstep(a_start, a_end, p)
                hue = lerp_hue(hue_a, hue_b, t)

    rgb = hue_to_rgb_with_bw(hue, sat, val)
    if target == "r":
        return rgb["r"]
    if target == "g":
        return rgb["g"]
    if target == "b":
        return rgb["b"]
    if target == "dimmer":
        return round(intensity)
    return 0.0


def _fx_chaser(ctx: Dict[str, Any]) -> float:
    params, t_ms, device_index, device_count, device_id, target = _ctx_fields(ctx)
    amp = max(0.0, min(255.0, float(params.get("amplitude", 255) or 0)))
    phase_ms = phase_offset_ms(params, device_id)
    local_ctx = dict(ctx)
    local_ctx["t_ms"] = t_ms + phase_ms
    local_ctx["device_index"] = device_index
    local_ctx["device_count"] = device_count
    on = chaser_edge_fade(local_ctx, params)
    return round(amp * on)


def _fx_color_cycle(ctx: Dict[str, Any]) -> float:
    params, t_ms, device_index, device_count, device_id, target = _ctx_fields(ctx)
    phase_ms = phase_offset_ms(params, device_id)
    t_ms += phase_ms
    speed = float(params.get("speed", 0) or 0)
    sat = clamp(float(params.get("saturation", 0) or 0), 0.0, 1.0)
    intensity = clamp(float(params.get("intensity", 0) or 0), 0.0, 255.0)
    hue = ((t_ms * speed * 360.0) / 1000.0) % 360.0
    val = intensity / 255.0
    rgb = hsv_to_rgb(hue, sat, val)
    if target == "r":
        return rgb["r"]
    if target == "g":
        return rgb["g"]
    if target == "b":
        return rgb["b"]
    if target == "dimmer":
        return round(intensity)
    return 0.0


def _fx_color_wheel(ctx: Dict[str, Any]) -> float:
    params, t_ms, device_index, device_count, device_id, target = _ctx_fields(ctx)
    phase_ms = phase_offset_ms(params, device_id)
    t_ms += phase_ms
    speed = float(params.get("speed", 0) or 0)
    steps = max(2, int(float(params.get("steps", 8) or 0)))
    spread = float(params.get("spread", 0) or 0)
    sat = clamp(float(params.get("saturation", 0) or 0), 0.0, 1.0)
    intensity = clamp(float(params.get("intensity", 0) or 0), 0.0, 255.0)
    t = (t_ms / 1000.0) * speed
    step_index = int(math.floor(t)) % steps
    hue_base = (step_index * (360.0 / steps)) % 360.0
    hue = (hue_base + device_index * spread) % 360.0
    val = intensity / 255.0
    rgb = hsv_to_rgb(hue, sat, val)
    if target == "r":
        return rgb["r"]
    if target == "g":
        return rgb["g"]
    if target == "b":
        return rgb["b"]
    if target == "dimmer":
        return round(intensity)
    return 0.0


def _fx_comet(ctx: Dict[str, Any]) -> float:
    params, t_ms, device_index, device_count, device_id, target = _ctx_fields(ctx)
    count = max(1, device_count)
    phase_ms = phase_offset_ms(params, device_id)
    t_ms += phase_ms
    speed = float(params.get("speed", 0) or 0)
    length = max(1.0, float(params.get("length", 1) or 1))
    reverse = str(params.get("reverse", "Off")).lower() == "on"
    hue = float(params.get("hue", 0) or 0)
    sat = clamp(float(params.get("saturation", 0) or 0), 0.0, 1.0)
    intensity = clamp(float(params.get("intensity", 0) or 0), 0.0, 255.0)

    head = (t_ms / 1000.0) * speed * count
    pos = ((-head if reverse else head) % count + count) % count
    delta = pos - device_index
    if delta < 0:
        delta += count

    level = 0.0
    if delta <= length:
        level = 1.0 - delta / max(1.0, length)

    val = (intensity / 255.0) * level
    rgb = hue_to_rgb_with_bw(hue, sat, val)
    if target == "r":
        return rgb["r"]
    if target == "g":
        return rgb["g"]
    if target == "b":
        return rgb["b"]
    if target == "dimmer":
        return round(intensity * level)
    return 0.0


def _fx_pulse(ctx: Dict[str, Any]) -> float:
    params, t_ms, device_index, device_count, device_id, target = _ctx_fields(ctx)
    phase_ms = phase_offset_ms(params, device_id)
    t_ms += phase_ms
    speed = float(params.get("speed", 0) or 0)
    duty = clamp(float(params.get("duty", 0.5) or 0.5), 0.05, 0.95)
    hue = float(params.get("hue", 0) or 0)
    sat = clamp(float(params.get("saturation", 0) or 0), 0.0, 1.0)
    intensity = clamp(float(params.get("intensity", 0) or 0), 0.0, 255.0)

    w = wave("rectangle", t_ms, speed)
    on = 1.0 if w > (1 - 2 * duty) else 0.0
    val = (intensity / 255.0) * on
    rgb = hue_to_rgb_with_bw(hue, sat, val)
    if target == "r":
        return rgb["r"]
    if target == "g":
        return rgb["g"]
    if target == "b":
        return rgb["b"]
    if target == "dimmer":
        return round(intensity * on)
    return 0.0


def _fx_rainbow(ctx: Dict[str, Any]) -> float:
    params, t_ms, device_index, device_count, device_id, target = _ctx_fields(ctx)
    phase_ms = phase_offset_ms(params, device_id)
    t_ms += phase_ms
    speed = float(params.get("speed", 0) or 0)
    spread = float(params.get("spread", 0) or 0)
    sat = clamp(float(params.get("saturation", 0) or 0), 0.0, 1.0)
    intensity = clamp(float(params.get("intensity", 0) or 0), 0.0, 255.0)
    hue = ((t_ms * speed * 360.0) / 1000.0 + device_index * spread) % 360.0
    val = intensity / 255.0
    rgb = hsv_to_rgb(hue, sat, val)
    if target == "r":
        return rgb["r"]
    if target == "g":
        return rgb["g"]
    if target == "b":
        return rgb["b"]
    if target == "dimmer":
        return round(intensity)
    return 0.0


def _fx_sparkle(ctx: Dict[str, Any]) -> float:
    params, t_ms, device_index, device_count, device_id, target = _ctx_fields(ctx)
    phase_ms = phase_offset_ms(params, device_id)
    t_ms += phase_ms
    density = clamp(float(params.get("density", 0) or 0), 0.0, 1.0)
    speed = float(params.get("speed", 1) or 1)
    hue = float(params.get("hue", 0) or 0)
    sat = clamp(float(params.get("saturation", 0) or 0), 0.0, 1.0)
    intensity = clamp(float(params.get("intensity", 0) or 0), 0.0, 255.0)

    tick = int(math.floor((t_ms / 1000.0) * speed))
    seed = (device_index + 1) * 13.37 + tick * 7.13
    r = math.sin(seed) * 43758.5453123
    r = r - math.floor(r)
    on = 1.0 if r < density else 0.0
    val = (intensity / 255.0) * on
    rgb = hue_to_rgb_with_bw(hue, sat, val)
    if target == "r":
        return rgb["r"]
    if target == "g":
        return rgb["g"]
    if target == "b":
        return rgb["b"]
    if target == "dimmer":
        return round(intensity * on)
    return 0.0


def _fx_strobe(ctx: Dict[str, Any]) -> float:
    params, t_ms, device_index, device_count, device_id, target = _ctx_fields(ctx)
    phase_ms = phase_offset_ms(params, device_id)
    t_ms += phase_ms
    speed = float(params.get("speed", 0) or 0)
    duty = clamp(float(params.get("duty", 0.2) or 0.2), 0.05, 0.95)
    hue = float(params.get("hue", 0) or 0)
    sat = clamp(float(params.get("saturation", 0) or 0), 0.0, 1.0)
    intensity = clamp(float(params.get("intensity", 0) or 0), 0.0, 255.0)

    w = wave("rectangle", t_ms, speed)
    on = 1.0 if w > (1 - 2 * duty) else 0.0
    val = (intensity / 255.0) * on
    rgb = hue_to_rgb_with_bw(hue, sat, val)
    if target == "r":
        return rgb["r"]
    if target == "g":
        return rgb["g"]
    if target == "b":
        return rgb["b"]
    if target == "dimmer":
        return round(intensity * on)
    return 0.0


def _fx_visor(ctx: Dict[str, Any]) -> float:
    params, t_ms, device_index, device_count, device_id, target = _ctx_fields(ctx)
    count = max(1, device_count)
    phase_ms = phase_offset_ms(params, device_id)
    t_ms += phase_ms
    speed = float(params.get("speed", 0) or 0)
    width = max(1.0, float(params.get("width", 1) or 1))
    softness = clamp(float(params.get("softness", 0) or 0), 0.0, 1.0)
    hue = float(params.get("hue", 0) or 0)
    sat = clamp(float(params.get("saturation", 0) or 0), 0.0, 1.0)
    intensity = clamp(float(params.get("intensity", 0) or 0), 0.0, 255.0)

    head = (t_ms / 1000.0) * speed * count
    pos = head % count
    half = width / 2.0
    dist = abs(((device_index - pos + count / 2.0) % count) - count / 2.0)
    level = 0.0
    if dist <= half:
        t = 1.0 - dist / max(1e-6, half)
        level = math.pow(t, 1 + softness * 4) if softness > 0 else t

    val = (intensity / 255.0) * level
    rgb = hue_to_rgb_with_bw(hue, sat, val)
    if target == "r":
        return rgb["r"]
    if target == "g":
        return rgb["g"]
    if target == "b":
        return rgb["b"]
    if target == "dimmer":
        return round(intensity * level)
    return 0.0


EFFECTS: List[Dict[str, Any]] = [
    {
        "id": "breathing",
        "label": "Breathing",
        "targets": ["color", "dimmer"],
        "mode": "absolute",
        "params": [
            {"key": "phase", "label": "Phase (ms)", "type": "text", "default": "0", "hint": "0, 0>500, 0<500, 0><500, 0<>500, 0|500, 0||500, 0?500"},
            {"key": "speed", "label": "Speed (Hz)", "type": "range", "min": 0, "max": 3, "step": 0.05, "default": 0.3},
            {"key": "colorMode", "label": "Color Mode", "type": "select", "default": "Single color", "options": ["Single color", "Alternating colors"]},
            {"key": "alternateMode", "label": "Alternate On", "type": "select", "default": "Each full cycle", "options": ["Each full cycle", "Each half-cycle"], "hint": "full cycle = change every breathing cycle, half = change on inhale/exhale"},
            {"key": "hue", "label": "Hue (Color A)", "type": "range", "min": 0, "max": 360, "step": 1, "default": 200},
            {"key": "hue2", "label": "Hue (Color B)", "type": "range", "min": 0, "max": 360, "step": 1, "default": 20},
            {"key": "saturation", "label": "Saturation", "type": "range", "min": 0, "max": 1, "step": 0.05, "default": 1},
            {"key": "min", "label": "Min", "type": "range", "min": 0, "max": 255, "step": 1, "default": 10},
            {"key": "max", "label": "Max", "type": "range", "min": 0, "max": 255, "step": 1, "default": 255}
        ]
    },
    {
        "id": "chaser",
        "label": "Chaser",
        "targets": ["dimmer"],
        "mode": "absolute",
        "params": [
            {"key": "phase", "label": "Phase (ms)", "type": "text", "default": "0", "hint": "0, 0>500, 0<500, 0><500, 0<>500, 0|500, 0||500, 0?500"},
            {"key": "amplitude", "label": "Amplitude", "type": "number", "min": 0, "max": 255, "default": 255},
            {"key": "duration", "label": "Duration (ms)", "type": "number", "min": 0, "max": 60000, "default": 200},
            {"key": "fade", "label": "Fade (ms)", "type": "number", "min": 0, "max": 10000, "default": 100},
            {"key": "fadeCurve", "label": "Fade Curve", "type": "select", "options": ["Linear", "EaseIn", "EaseOut", "EaseInOut", "Snap", "Smooth"], "default": "Linear"},
            {"key": "breakStep", "label": "Break Step", "type": "number", "min": 0, "max": 100, "default": 0},
            {"key": "breakSize", "label": "Break Size (ms)", "type": "number", "min": 0, "max": 5000, "default": 500},
            {"key": "size", "label": "Size", "type": "number", "min": 1, "max": 100, "default": 1},
            {"key": "stepSize", "label": "Step Size", "type": "number", "min": 1, "max": 100, "default": 1},
            {"key": "playMode", "label": "Play Mode", "type": "select", "options": ["Normal", "Reverse", "Bounce", "In", "Out", "InOut", "Random", "Switch"], "default": "Normal"}
        ]
    },
    {
        "id": "color_cycle",
        "label": "Color Cycle",
        "targets": ["color", "dimmer"],
        "mode": "absolute",
        "params": [
            {"key": "phase", "label": "Phase (ms)", "type": "text", "default": "0", "hint": "0, 0>500, 0<500, 0><500, 0<>500, 0|500, 0||500, 0?500"},
            {"key": "speed", "label": "Speed (Hz)", "type": "range", "min": 0, "max": 3, "step": 0.05, "default": 0.4},
            {"key": "saturation", "label": "Saturation", "type": "range", "min": 0, "max": 1, "step": 0.05, "default": 1},
            {"key": "intensity", "label": "Intensity", "type": "range", "min": 0, "max": 255, "step": 1, "default": 255}
        ]
    },
    {
        "id": "color_wheel",
        "label": "Color Wheel",
        "targets": ["color", "dimmer"],
        "mode": "absolute",
        "params": [
            {"key": "phase", "label": "Phase (ms)", "type": "text", "default": "0", "hint": "0, 0>500, 0<500, 0><500, 0<>500, 0|500, 0||500, 0?500"},
            {"key": "speed", "label": "Speed (steps/s)", "type": "range", "min": 0, "max": 5, "step": 0.05, "default": 0.6},
            {"key": "steps", "label": "Steps", "type": "number", "min": 2, "max": 24, "default": 8},
            {"key": "spread", "label": "Spread (deg/device)", "type": "range", "min": 0, "max": 60, "step": 1, "default": 0},
            {"key": "saturation", "label": "Saturation", "type": "range", "min": 0, "max": 1, "step": 0.05, "default": 1},
            {"key": "intensity", "label": "Intensity", "type": "range", "min": 0, "max": 255, "step": 1, "default": 255}
        ]
    },
    {
        "id": "comet",
        "label": "Comet",
        "targets": ["color", "dimmer"],
        "mode": "absolute",
        "params": [
            {"key": "phase", "label": "Phase (ms)", "type": "text", "default": "0", "hint": "0, 0>500, 0<500, 0><500, 0<>500, 0|500, 0||500, 0?500"},
            {"key": "speed", "label": "Speed (cycles/s)", "type": "range", "min": 0, "max": 3, "step": 0.05, "default": 0.5},
            {"key": "length", "label": "Tail (devices)", "type": "range", "min": 1, "max": 30, "step": 1, "default": 8},
            {"key": "reverse", "label": "Reverse", "type": "select", "options": ["Off", "On"], "default": "Off"},
            {"key": "hue", "label": "Hue", "type": "range", "min": 0, "max": 360, "step": 1, "default": 40},
            {"key": "saturation", "label": "Saturation", "type": "range", "min": 0, "max": 1, "step": 0.05, "default": 1},
            {"key": "intensity", "label": "Intensity", "type": "range", "min": 0, "max": 255, "step": 1, "default": 255}
        ]
    },
    {
        "id": "pulse",
        "label": "Pulse",
        "targets": ["color", "dimmer"],
        "mode": "absolute",
        "params": [
            {"key": "phase", "label": "Phase (ms)", "type": "text", "default": "0", "hint": "0, 0>500, 0<500, 0><500, 0<>500, 0|500, 0||500, 0?500"},
            {"key": "speed", "label": "Speed (Hz)", "type": "range", "min": 0, "max": 5, "step": 0.05, "default": 1},
            {"key": "duty", "label": "Duty", "type": "range", "min": 0.05, "max": 0.95, "step": 0.05, "default": 0.5},
            {"key": "hue", "label": "Hue", "type": "range", "min": 0, "max": 360, "step": 1, "default": 0},
            {"key": "saturation", "label": "Saturation", "type": "range", "min": 0, "max": 1, "step": 0.05, "default": 1},
            {"key": "intensity", "label": "Intensity", "type": "range", "min": 0, "max": 255, "step": 1, "default": 255}
        ]
    },
    {
        "id": "rainbow",
        "label": "Rainbow",
        "targets": ["color", "dimmer"],
        "mode": "absolute",
        "params": [
            {"key": "phase", "label": "Phase (ms)", "type": "text", "default": "0", "hint": "0, 0>500, 0<500, 0><500, 0<>500, 0|500, 0||500, 0?500"},
            {"key": "speed", "label": "Speed (Hz)", "type": "range", "min": 0, "max": 3, "step": 0.05, "default": 0.2},
            {"key": "spread", "label": "Spread (deg/device)", "type": "range", "min": 0, "max": 60, "step": 1, "default": 10},
            {"key": "saturation", "label": "Saturation", "type": "range", "min": 0, "max": 1, "step": 0.05, "default": 1},
            {"key": "intensity", "label": "Intensity", "type": "range", "min": 0, "max": 255, "step": 1, "default": 255}
        ]
    },
    {
        "id": "sparkle",
        "label": "Sparkle",
        "targets": ["color", "dimmer"],
        "mode": "absolute",
        "params": [
            {"key": "phase", "label": "Phase (ms)", "type": "text", "default": "0", "hint": "0, 0>500, 0<500, 0><500, 0<>500, 0|500, 0||500, 0?500"},
            {"key": "density", "label": "Density", "type": "range", "min": 0, "max": 1, "step": 0.05, "default": 0.25},
            {"key": "speed", "label": "Speed", "type": "range", "min": 0.1, "max": 10, "step": 0.1, "default": 3},
            {"key": "hue", "label": "Hue", "type": "range", "min": 0, "max": 360, "step": 1, "default": 50},
            {"key": "saturation", "label": "Saturation", "type": "range", "min": 0, "max": 1, "step": 0.05, "default": 1},
            {"key": "intensity", "label": "Intensity", "type": "range", "min": 0, "max": 255, "step": 1, "default": 255}
        ]
    },
    {
        "id": "strobe",
        "label": "Strobe",
        "targets": ["color", "dimmer"],
        "mode": "absolute",
        "params": [
            {"key": "phase", "label": "Phase (ms)", "type": "text", "default": "0", "hint": "0, 0>500, 0<500, 0><500, 0<>500, 0|500, 0||500, 0?500"},
            {"key": "speed", "label": "Speed (Hz)", "type": "range", "min": 0, "max": 15, "step": 0.1, "default": 8},
            {"key": "duty", "label": "Duty", "type": "range", "min": 0.05, "max": 0.95, "step": 0.05, "default": 0.2},
            {"key": "hue", "label": "Hue", "type": "range", "min": 0, "max": 360, "step": 1, "default": 0},
            {"key": "saturation", "label": "Saturation", "type": "range", "min": 0, "max": 1, "step": 0.05, "default": 1},
            {"key": "intensity", "label": "Intensity", "type": "range", "min": 0, "max": 255, "step": 1, "default": 255}
        ]
    },
    {
        "id": "visor",
        "label": "Visor",
        "targets": ["color", "dimmer"],
        "mode": "absolute",
        "params": [
            {"key": "phase", "label": "Phase (ms)", "type": "text", "default": "0", "hint": "0, 0>500, 0<500, 0><500, 0<>500, 0|500, 0||500, 0?500"},
            {"key": "speed", "label": "Speed (cycles/s)", "type": "range", "min": 0, "max": 3, "step": 0.05, "default": 0.6},
            {"key": "width", "label": "Width (devices)", "type": "range", "min": 1, "max": 20, "step": 1, "default": 5},
            {"key": "softness", "label": "Softness", "type": "range", "min": 0, "max": 1, "step": 0.05, "default": 0.4},
            {"key": "hue", "label": "Hue", "type": "range", "min": 0, "max": 360, "step": 1, "default": 200},
            {"key": "saturation", "label": "Saturation", "type": "range", "min": 0, "max": 1, "step": 0.05, "default": 1},
            {"key": "intensity", "label": "Intensity", "type": "range", "min": 0, "max": 255, "step": 1, "default": 255}
        ]
    }
]


EFFECTS_BY_ID = {e["id"]: e for e in EFFECTS}


_EVAL_MAP = {
    "breathing": _fx_breathing,
    "chaser": _fx_chaser,
    "color_cycle": _fx_color_cycle,
    "color_wheel": _fx_color_wheel,
    "comet": _fx_comet,
    "pulse": _fx_pulse,
    "rainbow": _fx_rainbow,
    "sparkle": _fx_sparkle,
    "strobe": _fx_strobe,
    "visor": _fx_visor,
}


def list_effects() -> List[Dict[str, Any]]:
    imported, by_id = _load_imported_runtime_effects()
    merged = {e["id"]: dict(e) for e in EFFECTS}
    for effect_id, effect_def in by_id.items():
        merged[effect_id] = dict(effect_def)
    return sorted(merged.values(), key=lambda item: str(item.get("label") or item.get("id") or ""))


def get_effect_def(effect_id: str) -> Optional[Dict[str, Any]]:
    key = str(effect_id or "").strip().lower()
    if not key:
        return None
    imported, by_id = _load_imported_runtime_effects()
    if key in by_id:
        return by_id[key]
    return EFFECTS_BY_ID.get(key)


def eval_effect(effect_id: str, ctx: Dict[str, Any]) -> float:
    key = str(effect_id or "").strip().lower()
    fn = _EVAL_MAP.get(key)
    if fn:
        return fn(ctx)
    defn = get_effect_def(key)
    if not defn:
        return 0.0
    return _eval_runtime_effect(defn, ctx)


def apply_effect_value(defn: Dict[str, Any], base: int, raw: float, scale: float = 1.0) -> int:
    return _apply_intelligent_value(defn, base, raw, scale)

