#!/usr/bin/env python3
"""Compositional "moves" for the AutoLight director.

The per-fixture agent system in ``autolight_director`` is good at
*constant texture* — every fixture has a role, a personality, and emits
something every frame. That's the floor. What it can't do alone is the
*creative restraint* a human VJ uses: kill 90 % of the rig and let one
strobe carry the drop, snap everyone to a single saturated colour for
two bars, freeze the whole picture mid-motion. Those are **moves** —
short, distinctive, intentional gestures that override the agents
during the moment.

This module adds a ``MoveScheduler`` that picks moves at musical
turning points (intent transitions into PEAK / RELEASE / BREATH, plus
periodic refresh during sustained intents) and applies them per-fixture
on top of the agent output. Each move has its own per-fixture logic so
it can target by capability (strobe-only) or by role (lead-only).

Adaptation: the scheduler accumulates the satisfaction-slider values
captured during each move's lifetime, averages them at the end, and
biases future picks toward moves the user rated well. Scores persist to
``autolight_move_scores.json`` so the system gets smarter across runs.
"""

from __future__ import annotations

import colorsys
import json
import logging
import math
import os
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from runtime_paths import DATA_DIR


log = logging.getLogger(__name__)


# How many recent firings of each move feed into its score. 5 is enough
# to smooth out single-rating noise but small enough to track preference
# drift over a session.
_SCORE_WINDOW = 5

# Score multiplier headroom. ``final_weight = base × (1 + ALPHA·score)``.
# 0.6 means a perfectly-loved move is 1.6× more likely than baseline,
# a hated one 0.4×. Never zero — even bad-rated moves get to re-prove
# themselves so we don't over-fit to one bad mood.
_SCORE_ALPHA = 0.6

# Interval between auto-saves of the score file. Cheap (small JSON) so
# we save aggressively to survive crashes.
_SAVE_INTERVAL_S = 30.0


# =============================================================================
# Channel-write helpers (re-implemented locally to avoid coupling to
# ``autolight_effects``'s internals; the logic is the same).
# =============================================================================


def _write_color(writes: Dict[int, int], caps: Dict[str, Any], r: int, g: int, b: int, brightness: int) -> None:
    """Master-dimmer fixtures: dim ch carries brightness, RGB stays saturated.
    Par fixtures (no master dimmer): RGB pre-multiplied by the brightness."""
    brightness = max(0, min(255, int(brightness)))
    dim_ch = caps.get("dimmer_channel")
    r_ch = caps.get("red_channel")
    g_ch = caps.get("green_channel")
    b_ch = caps.get("blue_channel")
    if dim_ch is not None:
        writes[int(dim_ch)] = brightness
        if r_ch is not None: writes[int(r_ch)] = max(0, min(255, int(r)))
        if g_ch is not None: writes[int(g_ch)] = max(0, min(255, int(g)))
        if b_ch is not None: writes[int(b_ch)] = max(0, min(255, int(b)))
    else:
        factor = brightness / 255.0
        if r_ch is not None: writes[int(r_ch)] = max(0, min(255, int(r * factor)))
        if g_ch is not None: writes[int(g_ch)] = max(0, min(255, int(g * factor)))
        if b_ch is not None: writes[int(b_ch)] = max(0, min(255, int(b * factor)))


def _hold_movers(writes: Dict[int, int], caps: Dict[str, Any]) -> None:
    """Centre any mover not already in writes — prevents pan/tilt jitter
    when a move clears agent output without setting movement explicitly."""
    pan_ch = caps.get("pan_channel")
    tilt_ch = caps.get("tilt_channel")
    if pan_ch is not None and int(pan_ch) not in writes:
        writes[int(pan_ch)] = 128
    if tilt_ch is not None and int(tilt_ch) not in writes:
        writes[int(tilt_ch)] = 128


# =============================================================================
# Context passed to each move per frame
# =============================================================================


@dataclass
class MoveContext:
    """Bundle the scheduler hands to ``Move.apply`` each fixture-frame.

    Contains everything a move needs: time, beat phase, palette, intent,
    and progress through its own lifetime. Decouples moves from the
    director module proper.
    """
    now: float
    elapsed: float
    duration_s: float
    progress: float           # elapsed / duration
    beat_period_s: float
    beats_elapsed: float      # within this move
    bar_position: float       # within current bar [0,1)
    kick_env: float
    snare_env: float
    hat_env: float
    energy: float
    palette_base_hue: float
    palette_lead_hue: float
    intent_name: str
    activation_seed: int      # stable hash for randomised pickups (e.g. mono colour)


# =============================================================================
# Move base class
# =============================================================================


class Move:
    """Base class for compositional moves.

    Each subclass overrides :meth:`apply` (and optionally
    :meth:`describe_for_ui`). Class-level metadata describes when the
    scheduler should consider firing the move.
    """
    name: str = "move"
    description: str = ""
    eligible_intents: Tuple[str, ...] = ()
    min_duration_beats: float = 4.0
    max_duration_beats: float = 8.0
    cooldown_bars: float = 12.0
    base_weight: float = 1.0
    # ``override`` clears the agent output entirely — the move dictates
    # every channel. ``modulate`` post-processes the agent output (rarely
    # used in the initial roster but useful for e.g. global tints).
    style: str = "override"

    def apply(
        self,
        dev_id: str,
        caps: Dict[str, Any],
        topo_fixture: Any,
        role_name: str,
        ctx: MoveContext,
        writes: Dict[int, int],
    ) -> None:
        raise NotImplementedError


# =============================================================================
# Concrete moves
# =============================================================================


class StrobeOnlyDrop(Move):
    """The user's example: kill everything except strobe-friendly fixtures
    on a drop. 12 Hz square wave on the strobers, hard blackout elsewhere.
    """
    name = "strobe_only_drop"
    description = "Strobe-friendly fixtures only, 12 Hz white. Everything else dark."
    eligible_intents = ("PEAK",)
    min_duration_beats = 4.0
    max_duration_beats = 8.0
    cooldown_bars = 16.0
    base_weight = 1.4
    style = "override"

    def apply(self, dev_id, caps, topo, role_name, ctx, writes):
        writes.clear()
        if caps.get("strobe_friendly"):
            on = int(ctx.now * 24.0) % 2 == 0
            _write_color(writes, caps, 255, 255, 255, 255 if on else 0)
        else:
            _write_color(writes, caps, 0, 0, 0, 0)
        _hold_movers(writes, caps)


class MonoSaturated(Move):
    """All fixtures snap to one saturated colour (picked at activation),
    brightness pulses with the live kick. No chasing, no role variation.
    """
    name = "mono_saturated"
    description = "Single saturated colour across the entire rig, brightness rides the kick."
    eligible_intents = ("BUILD", "PEAK")
    min_duration_beats = 8.0
    max_duration_beats = 12.0
    cooldown_bars = 14.0
    base_weight = 1.2
    style = "override"

    def apply(self, dev_id, caps, topo, role_name, ctx, writes):
        writes.clear()
        # Pick from a small palette of "iconic" hues seeded once per
        # activation so the colour is consistent for the move's lifetime.
        iconic = [0.0, 0.08, 0.55, 0.66, 0.85]  # red, orange, cyan, blue, magenta
        hue = iconic[ctx.activation_seed % len(iconic)]
        rf, gf, bf = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
        brightness = int(140 + 115 * ctx.kick_env)
        _write_color(writes, caps, int(rf * 255), int(gf * 255), int(bf * 255), brightness)
        _hold_movers(writes, caps)


class LeadOnly(Move):
    """Only LEAD-role fixtures stay lit (saturated palette colour, full
    bright). Everything else hard-cut. Lasts a bar or two — punchy."""
    name = "lead_only"
    description = "Only the LEAD fixtures lit. Brutal contrast for short moments."
    eligible_intents = ("BUILD", "PEAK")
    min_duration_beats = 2.0
    max_duration_beats = 4.0
    cooldown_bars = 10.0
    base_weight = 1.0
    style = "override"

    def apply(self, dev_id, caps, topo, role_name, ctx, writes):
        writes.clear()
        if role_name == "LEAD":
            rf, gf, bf = colorsys.hsv_to_rgb(ctx.palette_lead_hue, 1.0, 1.0)
            brightness = int(200 + 55 * ctx.kick_env)
            _write_color(writes, caps, int(rf * 255), int(gf * 255), int(bf * 255), brightness)
        else:
            _write_color(writes, caps, 0, 0, 0, 0)
        _hold_movers(writes, caps)


class MoverSpotlight(Move):
    """Movers only — pars and statics blackout. Movers freeze on a tight
    centre pose, white at full brightness. Forces the eye to the kinetic
    fixtures."""
    name = "mover_spotlight"
    description = "Only movers lit at full white. Pars dark."
    eligible_intents = ("CHORUS", "HIGH", "PEAK")
    min_duration_beats = 6.0
    max_duration_beats = 10.0
    cooldown_bars = 12.0
    base_weight = 0.9
    style = "override"

    def apply(self, dev_id, caps, topo, role_name, ctx, writes):
        writes.clear()
        if caps.get("has_movement"):
            _write_color(writes, caps, 255, 255, 255, 240)
            # Lock pose: small breathing pan around centre, fixed tilt.
            phase = ctx.beats_elapsed * 0.5
            pan = caps.get("pan_channel")
            tilt = caps.get("tilt_channel")
            if pan is not None:
                writes[int(pan)] = max(0, min(255, 128 + int(20 * math.sin(phase))))
            if tilt is not None:
                writes[int(tilt)] = 100
        else:
            _write_color(writes, caps, 0, 0, 0, 0)


class MirrorAntiPhase(Move):
    """Left + right mirror sides flash inverse to each other on every
    beat. Whole rig participates — overrides the role-driven brightness."""
    name = "mirror_antiphase"
    description = "Left/right mirror pairs alternate every beat. Sharp."
    eligible_intents = ("CHORUS", "HIGH")
    min_duration_beats = 4.0
    max_duration_beats = 8.0
    cooldown_bars = 10.0
    base_weight = 1.0
    style = "override"

    def apply(self, dev_id, caps, topo, role_name, ctx, writes):
        writes.clear()
        side = getattr(topo, "mirror_side", None) if topo else None
        beat_idx = int(ctx.beats_elapsed * 2)  # twice per beat for snappier feel
        on_phase = beat_idx % 2 == 0
        rf, gf, bf = colorsys.hsv_to_rgb(ctx.palette_lead_hue, 1.0, 1.0)
        if side == "left":
            level = 240 if on_phase else 20
        elif side == "right":
            level = 20 if on_phase else 240
        else:
            # Centre fixtures: middle ground at constant 100 so they don't strobe.
            level = 100
        _write_color(writes, caps, int(rf * 255), int(gf * 255), int(bf * 255), level)
        _hold_movers(writes, caps)


class SilenceVoid(Move):
    """Global blackout but for a 3 % ember on every fixture. Held for
    the whole move duration — no kick reaction. Creates anticipation
    before a build returns."""
    name = "silence_void"
    description = "Near-total blackout, 3 % ember on every fixture. Held still."
    eligible_intents = ("BREATH", "RELEASE")
    min_duration_beats = 4.0
    max_duration_beats = 8.0
    cooldown_bars = 14.0
    base_weight = 0.9
    style = "override"

    def apply(self, dev_id, caps, topo, role_name, ctx, writes):
        writes.clear()
        # 3 % brightness with the palette base hue — barely visible but not
        # a hard zero, so the eye stays anchored.
        rf, gf, bf = colorsys.hsv_to_rgb(ctx.palette_base_hue, 0.7, 1.0)
        _write_color(writes, caps, int(rf * 255), int(gf * 255), int(bf * 255), 8)
        _hold_movers(writes, caps)


class WaveFadeOut(Move):
    """Brightness fades out left-to-right across the rig over the move's
    duration. Cinematic ending for RELEASE phases."""
    name = "wave_fade_out"
    description = "Brightness fades out left-to-right across the rig."
    eligible_intents = ("RELEASE", "BREATH")
    min_duration_beats = 8.0
    max_duration_beats = 12.0
    cooldown_bars = 16.0
    base_weight = 0.8
    style = "override"

    def apply(self, dev_id, caps, topo, role_name, ctx, writes):
        writes.clear()
        order = getattr(topo, "order_index", 0) if topo else 0
        n = max(1, getattr(topo, "_chaser_len", 16) if topo else 16)
        pos = (order % n) / max(1, n - 1)
        # Each fixture goes dark when the wave passes its position.
        threshold = ctx.progress
        if pos > threshold:
            level = int(200 * (1.0 - (threshold - pos + 1.0) % 1.0))
            level = max(20, min(220, level))
        else:
            level = 0
        rf, gf, bf = colorsys.hsv_to_rgb(ctx.palette_base_hue, 0.85, 1.0)
        _write_color(writes, caps, int(rf * 255), int(gf * 255), int(bf * 255), level)
        _hold_movers(writes, caps)


class FreezeFrame(Move):
    """Snaps every fixture to a *random* but stable per-fixture state and
    HOLDS for 1-2 beats. Reads as a deliberate pause."""
    name = "freeze_frame"
    description = "Everyone snaps to a fixed pose, no motion, for 1-2 beats."
    eligible_intents = ("CHORUS", "HIGH", "PEAK", "BUILD")
    min_duration_beats = 1.0
    max_duration_beats = 2.0
    cooldown_bars = 6.0
    base_weight = 0.7
    style = "override"

    def apply(self, dev_id, caps, topo, role_name, ctx, writes):
        writes.clear()
        # Each fixture gets a deterministic on/off + colour from a hash of
        # (dev_id, activation_seed). Stable for the move's lifetime.
        h = abs(hash((dev_id, ctx.activation_seed)))
        on = (h % 4) != 0  # 75 % of fixtures lit
        if on:
            hue = (h % 1000) / 1000.0
            rf, gf, bf = colorsys.hsv_to_rgb(hue, 0.95, 1.0)
            _write_color(writes, caps, int(rf * 255), int(gf * 255), int(bf * 255), 220)
        else:
            _write_color(writes, caps, 0, 0, 0, 0)
        _hold_movers(writes, caps)


class WhiteChase(Move):
    """A bright white head sweeps across the rig at 1 chase per bar; non-head
    fixtures sit at a low warm baseline. Reads as "spotlight running"."""
    name = "white_chase"
    description = "Bright white head sweeps across the rig, others nearly dark."
    eligible_intents = ("BUILD", "CHORUS", "HIGH")
    min_duration_beats = 8.0
    max_duration_beats = 12.0
    cooldown_bars = 12.0
    base_weight = 0.9
    style = "override"

    def apply(self, dev_id, caps, topo, role_name, ctx, writes):
        writes.clear()
        n = max(1, getattr(topo, "_chaser_len", 16) if topo else 16)
        order = getattr(topo, "order_index", 0) if topo else 0
        pos = (order % n) / n
        head = (ctx.beats_elapsed / 4.0) % 1.0  # one full pass per bar
        d = abs(pos - head)
        d = min(d, 1.0 - d)
        width = 1.0 / max(2, n) * 1.5
        is_head = d < width
        if is_head:
            level = int(200 + 55 * (1.0 - d / width))
            _write_color(writes, caps, 255, 240, 220, level)
        else:
            _write_color(writes, caps, 255, 200, 120, 25)  # warm ember background
        _hold_movers(writes, caps)


class SoloBeacon(Move):
    """One single fixture (deterministic per activation) lit at full
    saturated colour. All others dark. Lasts 2-4 beats — punchy reset."""
    name = "solo_beacon"
    description = "One random fixture lit, all others dark. Hard contrast."
    eligible_intents = ("BREATH", "VERSE", "RELEASE")
    min_duration_beats = 2.0
    max_duration_beats = 4.0
    cooldown_bars = 8.0
    base_weight = 0.7
    style = "override"

    def apply(self, dev_id, caps, topo, role_name, ctx, writes):
        writes.clear()
        # Pick the soloed fixture by hashing the activation seed against
        # the device id space. Stable for the move's lifetime.
        h = abs(hash(dev_id)) % 997
        is_solo = (h ^ ctx.activation_seed) % 8 == 0
        if is_solo:
            rf, gf, bf = colorsys.hsv_to_rgb(ctx.palette_lead_hue, 1.0, 1.0)
            _write_color(writes, caps, int(rf * 255), int(gf * 255), int(bf * 255), 255)
        else:
            _write_color(writes, caps, 0, 0, 0, 0)
        _hold_movers(writes, caps)


# Insertion order is cosmetic.
_MOVES_REGISTRY: List[Move] = [
    StrobeOnlyDrop(),
    MonoSaturated(),
    LeadOnly(),
    MoverSpotlight(),
    MirrorAntiPhase(),
    SilenceVoid(),
    WaveFadeOut(),
    FreezeFrame(),
    WhiteChase(),
    SoloBeacon(),
]


def list_moves_meta() -> List[Dict[str, Any]]:
    return [
        {
            "name": m.name,
            "description": m.description,
            "eligible_intents": list(m.eligible_intents),
            "min_duration_beats": float(m.min_duration_beats),
            "max_duration_beats": float(m.max_duration_beats),
            "cooldown_bars": float(m.cooldown_bars),
            "base_weight": float(m.base_weight),
            "style": m.style,
        }
        for m in _MOVES_REGISTRY
    ]


# =============================================================================
# Active move bookkeeping + Scheduler
# =============================================================================


@dataclass
class _ActiveMove:
    move: Move
    start_ts: float
    duration_s: float
    activation_seed: int


def _scores_path() -> str:
    return os.path.join(DATA_DIR, "autolight_move_scores.json")


def _load_scores() -> Dict[str, List[float]]:
    path = _scores_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        out: Dict[str, List[float]] = {}
        for k, v in (data or {}).items():
            if isinstance(v, list):
                out[str(k)] = [float(x) for x in v if isinstance(x, (int, float))][-_SCORE_WINDOW:]
        return out
    except Exception as exc:
        log.warning("move scores load failed: %s", exc)
        return {}


def _save_scores(scores: Dict[str, deque]) -> None:
    path = _scores_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {k: list(v) for k, v in scores.items()}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
    except Exception as exc:
        log.warning("move scores save failed: %s", exc)


class MoveScheduler:
    """Picks compositional moves at musical turning points, scores them
    from the user's satisfaction signal, and persists those scores.

    Trigger logic (read more, code less):
    - On any intent transition INTO PEAK / RELEASE / BREATH / BUILD: a
      probability roll picks whether to fire a move and which one.
    - During a sustained intent: every ~16 bars another roll, slightly
      lower probability.
    - Cooldown per move: the move's ``cooldown_bars`` × current bar
      period.

    Score = average of the last ``_SCORE_WINDOW`` firings' average
    satisfaction. Pick weight = ``base_weight × (1 + ALPHA × score)``.
    """

    _PICK_PROB_ON_INTENT = {
        "PEAK":    0.75,
        "BUILD":   0.40,
        "RELEASE": 0.45,
        "BREATH":  0.40,
        "DRIFT":   0.10,
        "CHORUS":  0.20,
        "HIGH":    0.30,
    }
    _PICK_PROB_REFRESH = 0.30  # roll once per 16 bars within a sustained intent

    def __init__(self) -> None:
        self._active: Optional[_ActiveMove] = None
        self._last_trigger_ts: Dict[str, float] = {}
        self._scores: Dict[str, deque] = {
            k: deque(v, maxlen=_SCORE_WINDOW) for k, v in _load_scores().items()
        }
        for m in _MOVES_REGISTRY:
            self._scores.setdefault(m.name, deque(maxlen=_SCORE_WINDOW))

        self._satisfaction_buffer: List[float] = []
        self._last_seen_intent: str = "REST"
        self._last_refresh_check_bar: int = -1
        self._rng = random.Random()
        self._history: List[Tuple[float, str, float]] = []  # (ts, name, avg_satisfaction)
        self._last_save_ts: float = 0.0
        self._dirty = False

    # ------------------------------------------------------------------

    def step(
        self,
        intent_name: str,
        beat_period_s: float,
        bar_count: int,
        audio_active: bool,
        now: float,
    ) -> Optional[_ActiveMove]:
        # Expire any active move past its lifetime — finalise its score.
        if self._active is not None and (now - self._active.start_ts) >= self._active.duration_s:
            self._finalize(now)

        if not audio_active:
            self._last_seen_intent = intent_name
            return self._active

        intent_changed = intent_name != self._last_seen_intent
        prev_intent = self._last_seen_intent
        self._last_seen_intent = intent_name

        # Don't pick a new one while one is still running.
        if self._active is None:
            should_pick = False
            if intent_changed:
                prob = self._PICK_PROB_ON_INTENT.get(intent_name, 0.0)
                should_pick = self._rng.random() < prob
            elif bar_count != self._last_refresh_check_bar and bar_count % 16 == 0 and bar_count > 0:
                self._last_refresh_check_bar = bar_count
                should_pick = self._rng.random() < self._PICK_PROB_REFRESH

            if should_pick:
                cand = self._pick(intent_name, beat_period_s, now)
                if cand is not None:
                    self._activate(cand, now, beat_period_s)

        # Periodic score save — small file, but save on a clock not every
        # frame so we don't thrash the disk.
        if self._dirty and (now - self._last_save_ts) >= _SAVE_INTERVAL_S:
            _save_scores(self._scores)
            self._dirty = False
            self._last_save_ts = now

        return self._active

    def record_satisfaction(self, value: float) -> None:
        """Forwarded by the training pipeline. Accumulated until the
        active move finishes; then averaged into the move's score."""
        if self._active is None:
            return
        try:
            v = max(-1.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return
        self._satisfaction_buffer.append(v)

    def get_score(self, move_name: str) -> float:
        scores = self._scores.get(move_name)
        if not scores:
            return 0.0
        return sum(scores) / len(scores)

    def force_save(self) -> None:
        if self._dirty:
            _save_scores(self._scores)
            self._dirty = False

    def diagnostics(self) -> Dict[str, Any]:
        active = None
        if self._active is not None:
            elapsed = max(0.0, time.monotonic() - self._active.start_ts)
            active = {
                "name": self._active.move.name,
                "description": self._active.move.description,
                "elapsed_s": round(elapsed, 2),
                "duration_s": round(self._active.duration_s, 2),
                "progress": round(min(1.0, elapsed / max(0.001, self._active.duration_s)), 3),
                "score": round(self.get_score(self._active.move.name), 3),
                "samples_so_far": len(self._satisfaction_buffer),
            }
        return {
            "active": active,
            "scores": {name: round(self.get_score(name), 3) for name in self._scores},
            "score_samples": {name: len(s) for name, s in self._scores.items()},
            "history": [
                {"ts": round(ts, 2), "name": n, "satisfaction": round(s, 3)}
                for ts, n, s in self._history[-12:]
            ],
        }

    # ==================================================================
    # Internals
    # ==================================================================

    def _pick(self, intent_name: str, beat_period_s: float, now: float) -> Optional[Move]:
        bar_period = beat_period_s * 4.0
        pool: List[Tuple[Move, float]] = []
        for m in _MOVES_REGISTRY:
            if intent_name not in m.eligible_intents:
                continue
            last = self._last_trigger_ts.get(m.name, -1e9)
            if (now - last) < m.cooldown_bars * bar_period:
                continue
            score = self.get_score(m.name)
            weight = m.base_weight * (1.0 + _SCORE_ALPHA * score)
            if weight <= 0.0:
                continue
            pool.append((m, weight))

        if not pool:
            return None
        total = sum(w for _, w in pool)
        if total <= 0.0:
            return None
        r = self._rng.random() * total
        acc = 0.0
        for m, w in pool:
            acc += w
            if r <= acc:
                return m
        return pool[-1][0]

    def _activate(self, move: Move, now: float, beat_period_s: float) -> None:
        beats = self._rng.uniform(move.min_duration_beats, move.max_duration_beats)
        duration = beats * beat_period_s
        seed = self._rng.randint(0, 2_000_000_000)
        self._active = _ActiveMove(move=move, start_ts=now, duration_s=duration, activation_seed=seed)
        self._satisfaction_buffer = []

    def _finalize(self, now: float) -> None:
        if self._active is None:
            return
        active = self._active
        if self._satisfaction_buffer:
            avg = sum(self._satisfaction_buffer) / len(self._satisfaction_buffer)
        else:
            avg = 0.0  # no rating given — neutral, doesn't move score much
        # Only update the score when we actually got user input. Empty
        # buffer means the user didn't move the slider — treat that as
        # "no signal" rather than "score=0", to avoid scores drifting to
        # zero just because the user didn't engage.
        if self._satisfaction_buffer:
            self._scores.setdefault(active.move.name, deque(maxlen=_SCORE_WINDOW)).append(avg)
            self._dirty = True
        self._last_trigger_ts[active.move.name] = active.start_ts + active.duration_s
        self._history.append((active.start_ts, active.move.name, avg))
        if len(self._history) > 60:
            self._history = self._history[-60:]
        self._active = None
        self._satisfaction_buffer = []


def all_moves_meta() -> List[Dict[str, Any]]:
    """Public helper for UI / API surfaces."""
    return list_moves_meta()
