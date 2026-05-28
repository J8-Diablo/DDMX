#!/usr/bin/env python3
"""Per-fixture intelligent overlay for AutoLight.

This module is the alternative to the scene-engine + effect-roulette pipeline.
Instead of picking pre-made effects from a registry, it runs:

    AudioAnalyzer -> MusicDirector -> FixtureAgent[per-device] -> DMX writes

The director maintains multi-timescale audio understanding (kick/snare/hat
onsets, energy trends, build/release detection) and short + long term memory.
It decides on a global *intent* (REST / DRIFT / BUILD / PEAK / RELEASE /
BREATH), a colour palette, a movement style, and assigns each fixture a
*role* (LEAD / SUPPORT / WASH / ACCENT / REST).

Each FixtureAgent then makes its own per-frame decision based on its role,
its assigned palette slot, the audio context (kick env, snare env, etc.) and
its own *personality* (lazy / steady / twitchy / rebel) which controls its
smoothing rate and reactivity profile. Personalities are sticky — assigned
once at rig-registration and never changed — so the rig builds up a sense of
each fixture having a "character".

Optional persistent memory (DATA_DIR/autolight_memory.json) records per-track
signatures. On replay the director can pre-position itself with prior
knowledge of where build-ups land.
"""

from __future__ import annotations

import colorsys
import json
import logging
import math
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from autolight_compositions import MoveContext, MoveScheduler
from autolight_structure import StructureTracker
from autolight_topology import TopologySnapshot, compute_topology
from runtime_paths import DATA_DIR


log = logging.getLogger(__name__)


# =============================================================================
# Data structures
# =============================================================================


@dataclass
class Palette:
    """A small set of harmonised hues + a global saturation.

    The director rebuilds this on intent changes and drifts the base hue
    slowly the rest of the time. Agents pick into ``hues[role.palette_index]``
    rather than computing their own colour, which is what keeps the rig
    visually coherent rather than rainbow-chaotic.
    """
    base_hue: float = 0.6
    hues: List[float] = field(default_factory=lambda: [0.6])
    saturation: float = 0.85
    scheme: str = "monochrome"

    def get(self, idx: int) -> float:
        if not self.hues:
            return self.base_hue
        return self.hues[idx % len(self.hues)]


@dataclass
class IntentState:
    """The director's current high-level decision.

    ``since_ts`` lets us compute "how long have we been here" so agents
    (and the role-assignment policy) can react to dwell rather than just
    instantaneous state. ``intensity`` carries the structural-phase
    multiplier (drop2 hotter than drop1) so agent energy can vary across
    phases that share the same intent name.
    """
    name: str = "REST"
    since_ts: float = 0.0
    energy_ceiling: float = 0.30
    movement_style: str = "STATIC"
    intensity: float = 1.0


@dataclass
class FixtureRole:
    """Per-fixture directive issued by the director, consumed by the agent."""
    role: str = "WASH"
    palette_index: int = 0
    energy_factor: float = 0.5


@dataclass
class AudioCtx:
    """Snapshot of audio + intent passed to each agent every frame.

    Pre-computed so agents don't each repeat the same maths. ``kick_env`` is
    the live beat envelope (decays exp from each detected beat); ``snare_env``
    and ``hat_env`` are mid/treble onset envelopes computed by the director.
    """
    now: float
    bpm: float
    bass_norm: float
    mid_norm: float
    treble_norm: float
    energy: float
    kick_env: float
    snare_env: float
    hat_env: float
    bar_position: float
    beats_in_intent: float
    intent: IntentState


# =============================================================================
# Colour theory: palette schemes
# =============================================================================


_PALETTE_OFFSETS: Dict[str, List[float]] = {
    "monochrome":   [0.0],
    "analogous":    [0.0, +0.08, -0.08],
    "triadic":      [0.0, +0.333, +0.667],
    "complementary": [0.0, +0.5],
    "split_comp":   [0.0, +0.42, +0.58],
    "tetradic":     [0.0, +0.25, +0.5, +0.75],
}


def build_palette(base_hue: float, scheme: str, saturation: float) -> Palette:
    base_hue = base_hue % 1.0
    offsets = _PALETTE_OFFSETS.get(scheme, _PALETTE_OFFSETS["analogous"])
    hues = [(base_hue + off) % 1.0 for off in offsets]
    return Palette(
        base_hue=base_hue,
        hues=hues,
        saturation=max(0.0, min(1.0, saturation)),
        scheme=scheme,
    )


def _lerp_hue(current: float, target: float, alpha: float) -> float:
    """Shortest-path interpolation around the hue circle (handles 0/1 wrap)."""
    diff = (target - current + 0.5) % 1.0 - 0.5
    return (current + alpha * diff) % 1.0


# =============================================================================
# Track memory (persistent)
# =============================================================================


@dataclass
class TrackMemory:
    """Per-track learned signature, persisted across runs.

    The director writes one of these per recognised track. On replay it can
    use ``section_log`` (a list of intent transitions and their timestamps)
    to anticipate — e.g. if last time PEAK fired at 1m32s, we can bias toward
    PEAK earlier in the same window on the next play instead of waiting for
    the live audio cues to be unambiguous.

    ``satisfaction_log`` accumulates user feedback from the training UI —
    a continuous time series of -1..+1 values sampled at ~10 Hz while the
    user drags the satisfaction slider. Used to tell good director
    decisions from bad ones on later replays.
    """
    track_id: str
    title: str = ""
    artist: str = ""
    duration_ms: int = 0
    listen_count: int = 1
    peak_bpm: float = 0.0
    peak_energy: float = 0.0
    section_log: List[List[float]] = field(default_factory=list)  # [[elapsed_s, intent_index], ...]
    satisfaction_log: List[List[float]] = field(default_factory=list)  # [[elapsed_s, value_-1..1], ...]
    is_in_library: bool = False  # True when added via the training UI; transient — recomputed on load
    last_listened_ms: int = 0


_INTENT_INDEX = {"REST": 0, "DRIFT": 1, "BUILD": 2, "PEAK": 3, "RELEASE": 4, "BREATH": 5}
_INTENT_NAMES = {v: k for k, v in _INTENT_INDEX.items()}


def _memory_path() -> str:
    return os.path.join(DATA_DIR, "autolight_memory.json")


def load_memory() -> Dict[str, TrackMemory]:
    path = _memory_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        out: Dict[str, TrackMemory] = {}
        for k, v in (raw or {}).items():
            if not isinstance(v, dict):
                continue
            try:
                out[str(k)] = TrackMemory(
                    track_id=str(v.get("track_id") or k),
                    title=str(v.get("title") or ""),
                    artist=str(v.get("artist") or ""),
                    duration_ms=int(v.get("duration_ms") or 0),
                    listen_count=int(v.get("listen_count") or 1),
                    peak_bpm=float(v.get("peak_bpm") or 0.0),
                    peak_energy=float(v.get("peak_energy") or 0.0),
                    section_log=list(v.get("section_log") or []),
                    satisfaction_log=list(v.get("satisfaction_log") or []),
                    last_listened_ms=int(v.get("last_listened_ms") or 0),
                )
            except Exception:
                continue
        return out
    except Exception as exc:
        log.warning("autolight memory load failed: %s", exc)
        return {}


def save_memory(memory: Dict[str, TrackMemory]) -> None:
    path = _memory_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Truncate per-track logs to keep the file small. section_log caps
        # at 200 transitions per track (way more than any reasonable EDM
        # song produces). satisfaction_log caps at 5000 samples ≈ 8 min at
        # 10 Hz which covers any single-track session.
        data = {}
        for k, v in memory.items():
            d = dict(v.__dict__)
            d.pop("is_in_library", None)  # transient flag, never persisted
            d["section_log"] = list(d.get("section_log") or [])[-200:]
            d["satisfaction_log"] = list(d.get("satisfaction_log") or [])[-5000:]
            data[k] = d
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
    except Exception as exc:
        log.warning("autolight memory save failed: %s", exc)


# =============================================================================
# MusicDirector — multi-timescale audio understanding + intent state machine
# =============================================================================


class MusicDirector:
    """Tracks audio context, decides intent, builds palette, assigns roles.

    Designed to be called every render frame. All decisions are heuristic —
    no ML — but the multi-timescale memory (instant kick env / 4-bar groove /
    16-bar arc / track signature) gives it enough context to feel intentional
    rather than reactive.
    """

    PALETTE_SCHEMES = {
        "REST":    "monochrome",
        "DRIFT":   "analogous",
        "BUILD":   "triadic",
        "PEAK":    "triadic",
        "RELEASE": "split_comp",
        "BREATH":  "monochrome",
    }
    PALETTE_SATURATION = {
        "REST":    0.55,
        "DRIFT":   0.85,
        "BUILD":   0.95,
        "PEAK":    1.00,
        "RELEASE": 0.85,
        "BREATH":  0.45,
    }
    ENERGY_CEILINGS = {
        "REST":    0.30,
        "DRIFT":   0.55,
        "BUILD":   0.78,
        # PEAK sits at 0.95 (not 1.00) so the structural intensity multiplier
        # has actual headroom on phases like EDM drop2 (intensity=1.10).
        # Without this room, drop1 and drop2 would both clamp to identical
        # ceilings and drop2's "this is the bigger climax" cue would be lost.
        "PEAK":    0.95,
        "RELEASE": 0.65,
        "BREATH":  0.20,
    }
    MOVEMENT_STYLES = {
        "REST":    "STATIC",
        "DRIFT":   "DRIFT",
        "BUILD":   "SWEEP",
        "PEAK":    "SCATTER",
        "RELEASE": "MIRROR",
        "BREATH":  "STATIC",
    }
    # Fractional distribution of fixture roles per intent. Sums approximately
    # to 1.0; the rounder pads with WASH if a rig is too small to honour the
    # ratio exactly.
    ROLE_DISTROS: Dict[str, Dict[str, float]] = {
        "REST":    {"WASH": 1.0},
        "DRIFT":   {"WASH": 0.55, "SUPPORT": 0.45},
        "BUILD":   {"WASH": 0.30, "SUPPORT": 0.40, "LEAD": 0.30},
        "PEAK":    {"LEAD": 0.20, "SUPPORT": 0.30, "ACCENT": 0.30, "WASH": 0.20},
        "RELEASE": {"SUPPORT": 0.50, "WASH": 0.50},
        "BREATH":  {"WASH": 0.70, "REST": 0.30},
    }
    ENERGY_FACTORS = {
        "LEAD":    1.00,
        "ACCENT":  0.85,
        "SUPPORT": 0.65,
        "WASH":    0.45,
        "REST":    0.10,
    }
    PALETTE_INDICES = {
        "LEAD":    0,
        "ACCENT":  0,  # accent stabs reuse the lead colour for cohesion
        "SUPPORT": 1,
        "WASH":    2,
        "REST":    0,
    }

    def __init__(self, memory_enabled: bool = False) -> None:
        self.intent = IntentState(name="REST")
        self.palette = build_palette(0.6, "monochrome", 0.55)
        self.fixture_roles: Dict[str, FixtureRole] = {}

        # Structural prior — knows where we are in the song. Blended into
        # _decide_intent only when audio is ambiguous; never overrides a
        # real audio drop.
        self._structure = StructureTracker()
        self._structural_prior_mode: str = "auto"  # "auto" | "off"
        self._genre_preset: str = "auto"

        # Compositional moves: short, distinctive overrides ("strobe-only
        # drop", "blackout-but-lead", etc.) layered on top of the per-fixture
        # agent system. Owned here so satisfaction signals route correctly
        # without an extra hop through the overlay.
        self._move_scheduler = MoveScheduler()

        # Memory layers
        self._snare_env = 0.0
        self._hat_env = 0.0
        self._mid_baseline = 0.0
        self._treble_baseline = 0.0

        # ~4 s of recent RMS for short-term trend (drop / rise detection beyond
        # what the AudioAnalyzer's structure level already gives us).
        self._short_rms_history: deque = deque(maxlen=200)

        # Timestamps for rate-limited decisions.
        self._last_step_ts: float = 0.0
        self._last_intent_change_ts: float = 0.0
        self._last_role_assign_ts: float = 0.0
        self._last_palette_jump_ts: float = 0.0

        # Track-level (persisted memory).
        self._memory_enabled = memory_enabled
        self._memory: Dict[str, TrackMemory] = load_memory() if memory_enabled else {}
        self._current_track_id: Optional[str] = None
        self._current_track: Optional[TrackMemory] = None
        self._track_started_at: float = 0.0
        self._silence_started_at: Optional[float] = None
        self._memory_dirty = False
        self._last_memory_save_ts: float = 0.0

    # -------------------------------------------------------------------------

    def set_memory_enabled(self, enabled: bool) -> None:
        if enabled and not self._memory_enabled:
            self._memory = load_memory()
            self._memory_enabled = True
        elif not enabled and self._memory_enabled:
            if self._memory_dirty:
                save_memory(self._memory)
            self._memory_enabled = False
            self._memory = {}
            self._current_track_id = None
            self._current_track = None

    def set_structural_prior_mode(self, mode: str) -> None:
        m = str(mode or "auto").strip().lower()
        self._structural_prior_mode = m if m in {"auto", "off"} else "auto"

    def set_genre_preset(self, name: str) -> None:
        self._genre_preset = str(name or "auto").strip().lower()

    def record_satisfaction(self, value: float, now: float) -> Optional[str]:
        """Append one satisfaction sample to the current track's log AND
        forward it to the move scheduler so the active composition's score
        gets the user's live feedback.

        ``value`` is clamped to ``[-1.0, +1.0]``. Returns the track id we
        wrote to, or ``None`` when there's no current track / memory is off
        (caller can show "no track to learn from" in the UI).

        The move scheduler ALSO receives the value even when memory is
        off — move scores are global (not per-track) and need feedback
        regardless of whether the user opted into per-track persistence.
        """
        v = max(-1.0, min(1.0, float(value)))
        # Always feed the move scheduler — it adapts globally, decoupled
        # from the per-track memory setting.
        self._move_scheduler.record_satisfaction(v)

        if not self._memory_enabled or self._current_track is None:
            return None
        elapsed = max(0.0, now - self._track_started_at)
        self._current_track.satisfaction_log.append([round(elapsed, 2), round(v, 3)])
        # Keep the in-memory list bounded so a long session doesn't grow
        # unbounded between flushes.
        if len(self._current_track.satisfaction_log) > 8000:
            del self._current_track.satisfaction_log[: len(self._current_track.satisfaction_log) - 5000]
        self._memory_dirty = True
        return self._current_track_id

    # -------------------------------------------------------------------------

    def step(
        self,
        audio: Dict[str, Any],
        now: float,
        topology: TopologySnapshot,
        devices: Dict[str, Any],
        track_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Update memory + decide intent + maintain palette + role assignment."""
        dt = max(0.0, now - self._last_step_ts) if self._last_step_ts > 0 else 0.025
        self._last_step_ts = now

        self._update_onsets(audio, dt)
        self._short_rms_history.append((now, float(audio.get("rms") or 0.0)))

        # Track recognition (when memory enabled).
        if self._memory_enabled:
            self._update_track_memory(track_meta, audio, now)

        # Structural awareness — must come AFTER track memory update so the
        # tracker sees a populated TrackMemory on replays.
        self._structure.update(
            track_meta=track_meta,
            audio_bpm=float(audio.get("bpm") or 0.0),
            now=now,
            track_memory=self._current_track,
            genre_preset=self._genre_preset,
            prior_mode=self._structural_prior_mode,
        )

        new_intent_name = self._decide_intent(audio, now)
        if new_intent_name != self.intent.name:
            self._enter_intent(new_intent_name, now)

        self._evolve_palette(audio, now, dt)

        # Re-assign roles every 4 s, plus immediately on intent change (handled
        # by clearing the timer in _enter_intent).
        if now - self._last_role_assign_ts >= 4.0:
            self._assign_roles(topology, devices, now)

        # Periodic memory flush.
        if self._memory_enabled and self._memory_dirty and (now - self._last_memory_save_ts) >= 30.0:
            save_memory(self._memory)
            self._memory_dirty = False
            self._last_memory_save_ts = now

    # -------------------------------------------------------------------------

    def get_role(self, dev_id: str) -> FixtureRole:
        return self.fixture_roles.get(dev_id, FixtureRole(role="WASH", palette_index=2, energy_factor=0.45))

    def audio_ctx(self, audio: Dict[str, Any], now: float) -> AudioCtx:
        bpm = float(audio.get("bpm") or 0.0)
        beat_period = 60.0 / bpm if bpm >= 50.0 else 0.5
        last_beat_ms = float(audio.get("last_beat_ms") or 0.0)

        if last_beat_ms > 0.0:
            sec_since = max(0.0, (now * 1000.0 - last_beat_ms) / 1000.0)
            kick_env = math.exp(-math.log(2.0) * sec_since / 0.18)
            beats_into = sec_since / max(0.001, beat_period)
            bar_position = (beats_into % 4.0) / 4.0
        else:
            kick_env = 0.0
            bar_position = 0.0

        bass_norm = min(1.0, float(audio.get("bass") or 0.0) / 0.025)
        mid_norm = min(1.0, float(audio.get("mid") or 0.0) / 0.020)
        treble_norm = min(1.0, float(audio.get("treble") or 0.0) / 0.010)

        beats_in_intent = max(0.0, (now - self.intent.since_ts) / max(0.001, beat_period))

        return AudioCtx(
            now=now,
            bpm=bpm,
            bass_norm=bass_norm,
            mid_norm=mid_norm,
            treble_norm=treble_norm,
            energy=max(bass_norm, mid_norm, treble_norm),
            kick_env=max(0.0, min(1.0, kick_env)),
            snare_env=max(0.0, min(1.0, self._snare_env)),
            hat_env=max(0.0, min(1.0, self._hat_env)),
            bar_position=bar_position,
            beats_in_intent=beats_in_intent,
            intent=self.intent,
        )

    # =========================================================================
    # Internal helpers
    # =========================================================================

    def _update_onsets(self, audio: Dict[str, Any], dt: float) -> None:
        """Cheap snare/hat onset detector built on top of mid/treble RMS.

        We can't run a separate FFT here without doubling CPU; the existing
        analyzer already exposes mid/treble. Comparing the live value against
        a slow EMA baseline gives a usable percussive accent signal that's
        good enough to drive ACCENT-role brightness pops without the cost of
        spectral flux.
        """
        mid = float(audio.get("mid") or 0.0)
        treble = float(audio.get("treble") or 0.0)

        # Slow EMA baselines (~5 s).
        alpha = 0.05
        self._mid_baseline += alpha * (mid - self._mid_baseline)
        self._treble_baseline += alpha * (treble - self._treble_baseline)

        # Decay the envelopes between hits.
        if dt > 0:
            self._snare_env *= math.exp(-math.log(2.0) * dt / 0.10)
            self._hat_env *= math.exp(-math.log(2.0) * dt / 0.04)

        if mid > self._mid_baseline * 1.5 and mid > 0.005:
            spike = (mid - self._mid_baseline) / max(1e-6, self._mid_baseline)
            self._snare_env = max(self._snare_env, min(1.0, spike / 2.0))
        if treble > self._treble_baseline * 1.7 and treble > 0.003:
            spike = (treble - self._treble_baseline) / max(1e-6, self._treble_baseline)
            self._hat_env = max(self._hat_env, min(1.0, spike / 2.0))

    def _decide_intent(self, audio: Dict[str, Any], now: float) -> str:
        active = bool(audio.get("active"))

        if not active:
            if self._silence_started_at is None:
                self._silence_started_at = now
            silence_duration = now - self._silence_started_at
            return "REST" if silence_duration > 5.0 else "BREATH"
        else:
            self._silence_started_at = None

        structure = audio.get("structure") or {}
        level = int(structure.get("level") or 0)
        drop_score = float(structure.get("drop_score") or 0.0)
        build_slope = float(structure.get("build_up_slope") or 0.0)
        long_rms = float(structure.get("long_rms") or 0.0)
        short_rms = float(structure.get("short_rms") or 0.0)

        # Hard rule: a real audio drop always wins. The structural prior
        # never gets to suppress an actual drop — that would be the worst
        # possible visual lie.
        if level == 4 or drop_score > 1.5:
            return "PEAK"

        # Audio-only candidate (the intent we'd pick without any prior).
        rms_ratio = short_rms / max(1e-6, long_rms) if long_rms > 0 else 1.0
        audio_intent = self._decide_intent_audio_only(
            level=level, drop_score=drop_score, build_slope=build_slope,
            rms_ratio=rms_ratio, audio=audio, now=now,
        )

        prior = self._structure.current_prior()
        if prior is None:
            return audio_intent

        prior_intent, prior_weight, _ = prior
        if prior_intent == audio_intent:
            return audio_intent

        # Audio decisiveness: how confident is the audio in its candidate?
        # When decisive, the prior gets ignored. When ambiguous, the prior
        # nudges us toward the structurally expected intent.
        audio_decisiveness = min(1.0, max(
            abs(rms_ratio - 1.0) * 2.0,
            drop_score / 1.5,
            abs(build_slope) * 1e4,
        ))
        effective_weight = prior_weight * (1.0 - audio_decisiveness)
        if effective_weight >= 0.5:
            return prior_intent
        return audio_intent

    def _decide_intent_audio_only(
        self,
        level: int,
        drop_score: float,
        build_slope: float,
        rms_ratio: float,
        audio: Dict[str, Any],
        now: float,
    ) -> str:
        """The pre-prior audio candidate. The hard PEAK override has already
        been applied by the caller, so this routine handles the ambiguous
        territory: BUILD vs RELEASE vs BREATH vs DRIFT."""

        # Coming down from a peak — short_rms dipping below long_rms is a
        # reliable post-drop sign.
        if self.intent.name == "PEAK" and rms_ratio < 0.85:
            return "RELEASE"

        # Rising energy + positive bass slope + at least chorus-level → BUILD.
        if level >= 2 and build_slope > 1e-5 and rms_ratio > 1.05:
            return "BUILD"

        # In RELEASE we hold for a few bars before relaxing back to DRIFT.
        if self.intent.name == "RELEASE":
            beats = (now - self.intent.since_ts) / max(0.001, 60.0 / max(50.0, float(audio.get("bpm") or 120.0)))
            if beats < 8.0:
                return "RELEASE"

        if level == 0:
            return "BREATH"

        return "DRIFT"

    def _enter_intent(self, name: str, now: float) -> None:
        # Pull intensity from the structural prior so drop2 / chorus3 / solo
        # phases get a brighter ceiling than their first-occurrence siblings.
        prior = self._structure.current_prior()
        intensity = float(prior[2]) if prior is not None else 1.0
        base_ceiling = self.ENERGY_CEILINGS.get(name, 0.5)
        # Clamp the multiplier so a runaway intensity can't blow past full.
        ceiling = max(0.0, min(1.0, base_ceiling * min(1.20, intensity)))
        self.intent = IntentState(
            name=name,
            since_ts=now,
            energy_ceiling=ceiling,
            movement_style=self.MOVEMENT_STYLES.get(name, "DRIFT"),
            intensity=intensity,
        )
        self._last_intent_change_ts = now
        # Force role re-assignment on intent change, and consider a palette
        # jump (handled in _evolve_palette by checking last_palette_jump_ts).
        self._last_role_assign_ts = 0.0

        # Log into track memory if persistent.
        if self._memory_enabled and self._current_track is not None:
            elapsed = max(0.0, now - self._track_started_at)
            self._current_track.section_log.append([round(elapsed, 2), _INTENT_INDEX.get(name, 1)])
            self._memory_dirty = True

    def _evolve_palette(self, audio: Dict[str, Any], now: float, dt: float) -> None:
        # Slow base-hue drift: golden-ratio bias, one full cycle per ~32 bars.
        bpm = max(50.0, float(audio.get("bpm") or 120.0))
        bar_period = 4.0 * 60.0 / bpm
        cycle_period = 32.0 * bar_period
        drift = (dt / cycle_period) * 0.382

        new_base = (self.palette.base_hue + drift) % 1.0
        scheme = self.PALETTE_SCHEMES.get(self.intent.name, "analogous")
        sat = self.PALETTE_SATURATION.get(self.intent.name, 0.85)

        # On intent change (since_ts == _last_intent_change_ts), jump the hue
        # by a golden step so the colour world genuinely refreshes between
        # sections rather than continuing the slow drift.
        if (
            self._last_intent_change_ts > 0
            and now - self._last_intent_change_ts < 0.5
            and now - self._last_palette_jump_ts > 4.0
        ):
            new_base = (new_base + 0.382) % 1.0
            self._last_palette_jump_ts = now

        self.palette = build_palette(new_base, scheme, sat)

    def _assign_roles(
        self,
        topology: TopologySnapshot,
        devices: Dict[str, Any],
        now: float,
    ) -> None:
        self._last_role_assign_ts = now
        if not devices:
            self.fixture_roles = {}
            return

        distro = self.ROLE_DISTROS.get(self.intent.name, {"WASH": 1.0})
        n = len(devices)

        # Compute integer counts that sum to n. Round each fraction; pad/trim
        # the largest bucket to exactly match.
        counts: Dict[str, int] = {}
        for role, frac in distro.items():
            counts[role] = max(0, int(round(frac * n)))
        delta = n - sum(counts.values())
        if delta != 0 and counts:
            # Adjust the role with the largest count to soak up the delta.
            top = max(counts.keys(), key=lambda k: counts[k])
            counts[top] = max(0, counts[top] + delta)

        # Sort fixtures: centre first (good LEAD candidates), with movers
        # promoted in the ACCENT bucket. Stable secondary sort by device id
        # so role assignment is deterministic across frames.
        def sort_key(item):
            dev_id, dev = item
            topo_fix = topology.fixtures.get(dev_id)
            cluster_x = topo_fix.cluster_x if topo_fix else 1
            return (abs(cluster_x - 1), str(dev_id))

        sorted_devs = sorted(devices.items(), key=sort_key)

        # Build the role list in priority order so the most "central" fixtures
        # get the high-status roles.
        role_list: List[str] = []
        for role in ("LEAD", "ACCENT", "SUPPORT", "WASH", "REST"):
            role_list.extend([role] * counts.get(role, 0))
        while len(role_list) < n:
            role_list.append("WASH")
        role_list = role_list[:n]

        # If any movers exist and ACCENT slots aren't currently movers, swap.
        # This makes accents feel kinetic rather than just bright.
        accent_indices = [i for i, r in enumerate(role_list) if r == "ACCENT"]
        for accent_i in accent_indices:
            dev_id, dev = sorted_devs[accent_i]
            caps = getattr(dev, "capabilities", None) or {}
            if caps.get("has_movement"):
                continue
            for j in range(len(sorted_devs)):
                if role_list[j] == "ACCENT":
                    continue
                cand_id, cand_dev = sorted_devs[j]
                cand_caps = getattr(cand_dev, "capabilities", None) or {}
                if cand_caps.get("has_movement"):
                    role_list[j], role_list[accent_i] = role_list[accent_i], role_list[j]
                    break

        new_roles: Dict[str, FixtureRole] = {}
        for (dev_id, _dev), role in zip(sorted_devs, role_list):
            new_roles[dev_id] = FixtureRole(
                role=role,
                palette_index=self.PALETTE_INDICES.get(role, 0),
                energy_factor=self.ENERGY_FACTORS.get(role, 0.5),
            )
        self.fixture_roles = new_roles

    # -------------------------------------------------------------------------

    def _update_track_memory(
        self,
        track_meta: Optional[Dict[str, Any]],
        audio: Dict[str, Any],
        now: float,
    ) -> None:
        """Recognise the current track and accumulate stats into ``self._memory``.

        ``track_meta`` is a dict with title/artist/duration_ms (from the
        Windows Media probe). When the (title, artist) pair changes we close
        out the previous track and open a new entry. When unrecognised
        (e.g. between songs) we let the current track linger for 30 s before
        closing — handles short pauses without losing context.
        """
        title = (track_meta or {}).get("title")
        artist = (track_meta or {}).get("artist")
        duration_ms = int((track_meta or {}).get("duration_ms") or 0)

        if title:
            tid = self._make_track_id(title, artist)
            if tid != self._current_track_id:
                # Save out the previous track first.
                if self._current_track is not None and self._memory_dirty:
                    save_memory(self._memory)
                self._current_track_id = tid
                self._track_started_at = now
                if tid in self._memory:
                    rec = self._memory[tid]
                    rec.listen_count += 1
                    rec.last_listened_ms = int(time.time() * 1000)
                else:
                    rec = TrackMemory(
                        track_id=tid,
                        title=title or "",
                        artist=artist or "",
                        duration_ms=duration_ms,
                        last_listened_ms=int(time.time() * 1000),
                    )
                    self._memory[tid] = rec
                self._current_track = rec
                self._memory_dirty = True

        # Update peaks for the current track.
        if self._current_track is not None:
            bpm = float(audio.get("bpm") or 0.0)
            if bpm > self._current_track.peak_bpm:
                self._current_track.peak_bpm = bpm
                self._memory_dirty = True
            energy = max(
                float(audio.get("bass") or 0.0),
                float(audio.get("mid") or 0.0),
                float(audio.get("treble") or 0.0),
            )
            if energy > self._current_track.peak_energy:
                self._current_track.peak_energy = energy
                self._memory_dirty = True

    @staticmethod
    def _make_track_id(title: Optional[str], artist: Optional[str]) -> str:
        t = (title or "").strip().lower()
        a = (artist or "").strip().lower()
        return f"{a}::{t}" if a else t

    # -------------------------------------------------------------------------

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "intent": self.intent.name,
            "intent_since": round(self.intent.since_ts, 3),
            "intent_intensity": round(self.intent.intensity, 2),
            "energy_ceiling": round(self.intent.energy_ceiling, 2),
            "movement_style": self.intent.movement_style,
            "palette_scheme": self.palette.scheme,
            "palette_base_hue": round(self.palette.base_hue, 3),
            "palette_hues": [round(h, 3) for h in self.palette.hues],
            "palette_saturation": round(self.palette.saturation, 2),
            "snare_env": round(self._snare_env, 3),
            "hat_env": round(self._hat_env, 3),
            "track_id": self._current_track_id,
            "track_known": (self._current_track_id in self._memory) if self._current_track_id else False,
            "track_listen_count": self._current_track.listen_count if self._current_track else 0,
            "memory_size": len(self._memory),
            "roles": {dev_id: r.role for dev_id, r in self.fixture_roles.items()},
            "structural": self._structure.diagnostics(),
            "compositions": self._move_scheduler.diagnostics(),
        }


# =============================================================================
# FixtureAgent — per-fixture state machine
# =============================================================================


# Personalities are picked once per fixture at rig registration and never
# change. They control how aggressively the fixture follows targets and how
# strongly it reacts to mid/treble onsets. The point is that two fixtures
# with the same role still don't behave identically — the rig develops a
# texture instead of feeling synchronous.
PERSONALITIES: Dict[str, Dict[str, float]] = {
    "lazy":    {"smoothing": 0.05, "movement_smoothing": 0.04, "snare_react": 0.30, "hat_react": 0.0},
    "steady":  {"smoothing": 0.15, "movement_smoothing": 0.10, "snare_react": 0.60, "hat_react": 0.20},
    "twitchy": {"smoothing": 0.40, "movement_smoothing": 0.30, "snare_react": 1.00, "hat_react": 0.80},
    "rebel":   {"smoothing": 0.20, "movement_smoothing": 0.15, "snare_react": -0.40, "hat_react": -0.20},
}


class FixtureAgent:
    """Per-fixture state + per-frame target-and-smooth.

    Reads its assigned role and the global palette/intent each frame, computes
    a target colour, brightness, pan and tilt, then smooths toward those
    targets at a rate set by its personality. The smoothing is what gives the
    rig a "settled" feel rather than each frame jumping to ideal values.
    """

    _BASE_BRIGHTNESS = {
        "LEAD":    0.70,
        "ACCENT":  0.35,
        "SUPPORT": 0.45,
        "WASH":    0.30,
        "REST":    0.05,
    }

    def __init__(self, dev_id: str, personality: str = "steady") -> None:
        self.dev_id = dev_id
        self.personality = personality if personality in PERSONALITIES else "steady"
        params = PERSONALITIES[self.personality]
        self.smoothing = float(params["smoothing"])
        self.movement_smoothing = float(params["movement_smoothing"])
        self.snare_react = float(params["snare_react"])
        self.hat_react = float(params["hat_react"])

        self.current_hue = 0.6
        self.current_brightness = 0.0
        self.current_pan = 128.0
        self.current_tilt = 128.0

    def step(
        self,
        role: FixtureRole,
        palette: Palette,
        ctx: AudioCtx,
        topo,
        caps: Dict[str, Any],
    ) -> Dict[int, int]:
        # ----- target hue -------------------------------------------------
        target_hue = palette.get(role.palette_index)
        # Rebels skew one slot off the prescribed palette index — adds a
        # tasteful colour breakup without leaving the harmonic family.
        if self.personality == "rebel" and len(palette.hues) > 1:
            target_hue = palette.hues[(role.palette_index + 1) % len(palette.hues)]

        # ----- target brightness -----------------------------------------
        base = self._BASE_BRIGHTNESS.get(role.role, 0.30)
        target = base * ctx.intent.energy_ceiling * role.energy_factor

        # Per-role audio modulation. Each role 'listens' to a different
        # element of the music — so they don't all pulse on the same cue.
        if role.role == "LEAD":
            target += ctx.kick_env * 0.30 * ctx.intent.energy_ceiling
            target += ctx.bass_norm * 0.10
        elif role.role == "ACCENT":
            target += ctx.snare_env * 0.55 * self.snare_react
            target += ctx.kick_env * 0.20
        elif role.role == "SUPPORT":
            target += ctx.bass_norm * 0.20
            target += ctx.kick_env * 0.10
        elif role.role == "WASH":
            target += ctx.energy * 0.15

        # Hi-hat shimmer (twitchy fixtures pop on hat hits, rebels invert).
        if abs(self.hat_react) > 0.01:
            target += ctx.hat_env * 0.20 * self.hat_react

        # Rebels invert brightness on alternating bars during energetic
        # intents — gives a "heretic" fixture that reads as deliberate.
        if self.personality == "rebel" and ctx.intent.name in ("BUILD", "PEAK"):
            bar_idx = int(ctx.beats_in_intent / 4.0)
            if bar_idx % 2 == 1:
                target = max(0.05, 0.7 - target * 0.6)

        target = max(0.0, min(1.0, target))

        # ----- smooth toward targets -------------------------------------
        self.current_hue = _lerp_hue(self.current_hue, target_hue, self.smoothing)
        self.current_brightness += self.smoothing * (target - self.current_brightness)

        # ----- movement ---------------------------------------------------
        if caps.get("has_movement"):
            tp, tt = self._compute_movement(ctx, topo, role)
            self.current_pan += self.movement_smoothing * (tp - self.current_pan)
            self.current_tilt += self.movement_smoothing * (tt - self.current_tilt)

        return self._compose(caps, palette.saturation)

    # -------------------------------------------------------------------------

    def _compute_movement(self, ctx: AudioCtx, topo, role: FixtureRole) -> Tuple[float, float]:
        order = getattr(topo, "order_index", 0) if topo else 0
        n = max(1, getattr(topo, "_chaser_len", 16) if topo else 16)
        side = getattr(topo, "mirror_side", None) if topo else None

        style = ctx.intent.movement_style

        if style == "STATIC":
            return 128.0, 128.0
        if style == "DRIFT":
            phase = ctx.beats_in_intent * 0.10 + order * 0.7
            return 128.0 + 25.0 * math.sin(phase), 128.0 + 15.0 * math.cos(phase * 0.7)
        if style == "SWEEP":
            # Slow scan with phase offset per fixture.
            phase = ctx.bar_position * 2.0 * math.pi
            offset = (order / max(1, n - 1)) - 0.5
            return (
                128.0 + 80.0 * math.sin(phase + offset * 0.8),
                128.0 + 30.0 * math.cos(phase * 0.5),
            )
        if style == "FOCUS":
            return 128.0, 90.0
        if style == "SCATTER":
            # Pseudo-random per half-beat; deterministic per fixture so it's
            # stable rather than jittery.
            seed = abs(hash((self.dev_id, int(ctx.beats_in_intent * 2.0))))
            return 60.0 + (seed % 130), 60.0 + ((seed >> 8) % 130)
        if style == "MIRROR":
            phase = ctx.bar_position * 2.0 * math.pi
            offset = math.sin(phase) * 60.0
            sign = 1 if side == "right" else (-1 if side == "left" else 0)
            return 128.0 + sign * offset, 128.0
        return 128.0, 128.0

    def _compose(self, caps: Dict[str, Any], saturation: float) -> Dict[int, int]:
        rf, gf, bf = colorsys.hsv_to_rgb(self.current_hue, saturation, 1.0)
        brightness = max(0, min(255, int(self.current_brightness * 255)))

        writes: Dict[int, int] = {}
        dim_ch = caps.get("dimmer_channel")
        r_ch = caps.get("red_channel")
        g_ch = caps.get("green_channel")
        b_ch = caps.get("blue_channel")

        if dim_ch is not None:
            writes[int(dim_ch)] = brightness
            if r_ch is not None: writes[int(r_ch)] = max(0, min(255, int(rf * 255)))
            if g_ch is not None: writes[int(g_ch)] = max(0, min(255, int(gf * 255)))
            if b_ch is not None: writes[int(b_ch)] = max(0, min(255, int(bf * 255)))
        else:
            # Par-style: bake brightness into RGB.
            if r_ch is not None: writes[int(r_ch)] = max(0, min(255, int(rf * brightness)))
            if g_ch is not None: writes[int(g_ch)] = max(0, min(255, int(gf * brightness)))
            if b_ch is not None: writes[int(b_ch)] = max(0, min(255, int(bf * brightness)))

        if caps.get("pan_channel") is not None:
            writes[int(caps["pan_channel"])] = max(0, min(255, int(round(self.current_pan))))
        if caps.get("tilt_channel") is not None:
            writes[int(caps["tilt_channel"])] = max(0, min(255, int(round(self.current_tilt))))

        return writes


# =============================================================================
# DirectorOverlay — render-loop entry point
# =============================================================================


class DirectorOverlay:
    """Top-level renderer when ``render_mode == 'director'``.

    Mirrors the surface area of ``_AutoLightRenderer`` so the parent class
    can delegate cleanly: ``on_rig_changed``, ``__call__``-style ``tick``,
    and ``last_snapshot`` for diagnostics.
    """

    def __init__(self, audio_analyzer, service, memory_enabled: bool = False) -> None:
        self._audio = audio_analyzer
        self._service = service
        self._director = MusicDirector(memory_enabled=memory_enabled)
        self._agents: Dict[str, FixtureAgent] = {}
        self._topology: TopologySnapshot = TopologySnapshot()
        self._owned_channels: Dict[int, set] = {}
        self._diag_last_frame_wrote = 0
        self._diag_last_frame_ts = 0
        self._diag_last_frame_mode = "off"

    # ------------------------------------------------------------------

    def set_memory_enabled(self, enabled: bool) -> None:
        self._director.set_memory_enabled(enabled)

    def on_rig_changed(self, devices: Dict[str, Any]) -> None:
        try:
            self._topology = compute_topology(devices)
            n = max(1, len(self._topology.order_by_x))
            for fix in self._topology.fixtures.values():
                fix._chaser_len = n
        except Exception as exc:
            log.warning("director topology compute failed: %s", exc)
            self._topology = TopologySnapshot()

        new_agents: Dict[str, FixtureAgent] = {}
        for dev_id, dev in (devices or {}).items():
            existing = self._agents.get(str(dev_id))
            if existing is not None:
                new_agents[str(dev_id)] = existing
            else:
                personality = self._pick_personality(str(dev_id), dev)
                new_agents[str(dev_id)] = FixtureAgent(str(dev_id), personality)
        self._agents = new_agents

    def tick(
        self,
        universes: Dict[int, List[int]],
        now: float,
        mode: str,
        track_meta: Optional[Dict[str, Any]] = None,
    ) -> int:
        audio = self._audio.snapshot()
        if not audio.get("available"):
            self._release(universes)
            self._diag_last_frame_wrote = 0
            self._diag_last_frame_mode = mode
            self._diag_last_frame_ts = int(time.time() * 1000)
            return 0

        devices = self._service._engine_devices_snapshot_locked()
        self._director.step(audio, now, self._topology, devices, track_meta=track_meta)
        ctx = self._director.audio_ctx(audio, now)

        # Tick the move scheduler — picks/expires the active move based on
        # intent transitions. Returned active move (if any) is applied per
        # fixture below; when None, agents drive the rig alone. The
        # scheduler lives on the director (it's a high-level decision, not
        # a render concern) so satisfaction signals flow naturally through
        # ``MusicDirector.record_satisfaction``.
        bar_count = int(audio.get("bar_count") or 0)
        beat_period_s = 60.0 / ctx.bpm if ctx.bpm >= 50.0 else 0.5
        active_move = self._director._move_scheduler.step(
            intent_name=self._director.intent.name,
            beat_period_s=beat_period_s,
            bar_count=bar_count,
            audio_active=bool(audio.get("active")),
            now=now,
        )

        # Build a MoveContext once per frame — the per-fixture apply() loop
        # below reuses it. Only build when a move is actually active.
        move_ctx: Optional[MoveContext] = None
        if active_move is not None:
            elapsed = max(0.0, now - active_move.start_ts)
            beats_in_move = elapsed / max(0.001, beat_period_s)
            palette = self._director.palette
            move_ctx = MoveContext(
                now=now,
                elapsed=elapsed,
                duration_s=active_move.duration_s,
                progress=min(1.0, elapsed / max(0.001, active_move.duration_s)),
                beat_period_s=beat_period_s,
                beats_elapsed=beats_in_move,
                bar_position=ctx.bar_position,
                kick_env=ctx.kick_env,
                snare_env=ctx.snare_env,
                hat_env=ctx.hat_env,
                energy=ctx.energy,
                palette_base_hue=palette.base_hue,
                palette_lead_hue=palette.get(0),
                intent_name=self._director.intent.name,
                activation_seed=active_move.activation_seed,
            )

        engine = getattr(self._service, "_engine", None)

        previously_owned = {uni: set(chs) for uni, chs in self._owned_channels.items()}
        self._owned_channels = {}
        wrote = 0

        # Cache the identify-active set lookup once per frame instead of
        # per fixture — TrainingService grabs the lock for each call so
        # this is a meaningful optimisation on large rigs.
        try:
            identifying = self._service.is_identifying  # bound method
        except AttributeError:
            identifying = lambda _id: False  # service older than this feature

        for dev_id, dev in devices.items():
            agent = self._agents.get(str(dev_id))
            if agent is None:
                continue
            caps = getattr(dev, "capabilities", None) or {}
            if not (caps.get("has_dimmer") or caps.get("has_color") or caps.get("has_movement")):
                continue

            # Skip fixtures the camera-calibration phase is currently
            # flashing — see the matching check in the effects renderer.
            if identifying(str(dev_id)):
                # Preserve ownership of this device's channels so the
                # cleanup at the end of this frame doesn't zero out the
                # flash identify_device just wrote.
                if mode == "live":
                    uni_num = int(getattr(dev, "universe", 0) or 0)
                    owned = self._owned_channels.setdefault(uni_num, set())
                    for role in ("dimmer_channel", "red_channel", "green_channel",
                                 "blue_channel", "pan_channel", "tilt_channel"):
                        ch = caps.get(role)
                        if ch is not None:
                            owned.add(int(ch))
                continue

            # Respect manual cue fades — never stamp on a fixture mid-fade.
            if engine is not None:
                try:
                    if engine.has_active_fade_for(str(dev_id)):
                        continue
                except Exception:
                    pass

            role = self._director.get_role(str(dev_id))
            topo_fix = self._topology.fixtures.get(str(dev_id))

            try:
                writes = agent.step(role, self._director.palette, ctx, topo_fix, caps)
            except Exception as exc:
                log.debug("agent step failed for %s: %s", dev_id, exc)
                continue

            # Compositional override: if a move is active, let it rewrite
            # the agent's per-fixture output. ``apply`` mutates ``writes``
            # in place — for ``override`` style moves it clears + repaints
            # entirely; for ``modulate`` it post-processes.
            if active_move is not None and move_ctx is not None:
                try:
                    active_move.move.apply(
                        str(dev_id), caps, topo_fix, role.role, move_ctx, writes,
                    )
                except Exception as exc:
                    log.debug("move apply failed for %s: %s", dev_id, exc)

            if mode == "live":
                uni_num = int(getattr(dev, "universe", 0) or 0)
                uni = universes.get(uni_num)
                if uni is None:
                    continue
                owned = self._owned_channels.setdefault(uni_num, set())
                for ch, val in writes.items():
                    if 0 <= ch < len(uni):
                        uni[ch] = val
                        owned.add(ch)
                        wrote += 1

        # Zero owned channels we used last frame but didn't touch this frame.
        if mode == "live" and previously_owned:
            for uni_num, prev in previously_owned.items():
                uni = universes.get(uni_num)
                if uni is None:
                    continue
                cur = self._owned_channels.get(uni_num, set())
                for ch in prev - cur:
                    if 0 <= ch < len(uni):
                        uni[ch] = 0

        self._diag_last_frame_wrote = wrote
        self._diag_last_frame_mode = mode
        self._diag_last_frame_ts = int(time.time() * 1000)
        return wrote

    # ------------------------------------------------------------------

    def _release(self, universes: Dict[int, List[int]]) -> None:
        if not self._owned_channels:
            return
        for uni_num, channels in self._owned_channels.items():
            uni = universes.get(uni_num)
            if uni is None:
                continue
            for ch in channels:
                if 0 <= ch < len(uni):
                    uni[ch] = 0
        self._owned_channels = {}

    def _pick_personality(self, dev_id: str, dev) -> str:
        """Deterministic personality assignment based on device id + capabilities.

        Movers lean twitchy/steady (they're the kinetic accents). Pars and
        non-movers spread across lazy/steady with the occasional rebel.
        """
        caps = getattr(dev, "capabilities", None) or {}
        h = abs(hash(dev_id))
        if caps.get("has_movement"):
            options = ["twitchy", "steady", "twitchy", "rebel"]
        else:
            options = ["steady", "lazy", "steady", "rebel", "lazy"]
        return options[h % len(options)]

    # ------------------------------------------------------------------

    def last_snapshot(self) -> Dict[str, Any]:
        diag = self._director.diagnostics()
        diag["last_frame_wrote"] = self._diag_last_frame_wrote
        diag["last_frame_mode"] = self._diag_last_frame_mode
        diag["last_frame_ts"] = self._diag_last_frame_ts
        diag["agents"] = [
            {"device_id": dev_id, "personality": agent.personality,
             "brightness": round(agent.current_brightness, 2),
             "hue": round(agent.current_hue, 3)}
            for dev_id, agent in self._agents.items()
        ]
        return diag

    def force_save_memory(self) -> None:
        if self._director._memory_enabled and self._director._memory_dirty:
            save_memory(self._director._memory)
            self._director._memory_dirty = False
        # Move scheduler scores live on a separate file and persist
        # regardless of memory_persistence — flush them here too on
        # shutdown so we don't lose adaptation between runs.
        try:
            self._director._move_scheduler.force_save()
        except Exception:
            pass
