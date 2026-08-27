"""autolight_beatgrid.py — the "DJ brain" tempo / phase / meter tracker.

This is the core piece the old AutoLight was missing: it does not merely detect
that a kick happened, it maintains a *beat grid* — knowing where each beat falls,
which beat is the downbeat ("the 1"), how beats group into 4/4 bars, and how bars
group into phrases (8/16/32). From that it derives an anticipation signal
(``bars_to_phrase_end`` / ``building``) so the show can prepare a drop instead of
only reacting after it.

Design goals (see AUTOLIGHT-REWRITE-DESIGN.md):
  * Pure Python (math only) → unit-testable with synthetic onset trains, no audio.
  * Tempo octave-folded toward the "felt" dancefloor range (≈90–180 BPM window).
  * PLL phase-lock onto onsets, high gain at first (lock < ~2 s) then steady.
  * Authority: tap > db reference for the *value*; audio always drives the *phase*.
  * Resets cleanly on track change / seek (no lifetime drift).

Two entry points:
  * Low-level (tests / fine control): ``add_onset()``, ``set_energy()``, ``update(now)``.
  * High-level (render overlay): ``observe(now, snapshot)`` consuming an
    ``AudioAnalyzer.snapshot()`` dict.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

# Felt-tempo window: one octave wide so folding always terminates. A tempo is
# repeatedly halved/doubled until it lands in [_FELT_LO, _FELT_HI).
_FELT_LO = 90.0
_FELT_HI = 180.0  # = 2 * _FELT_LO

_BEATS_PER_BAR = 4
_PHRASE_CANDIDATES = (8, 16, 32)
_DEFAULT_PHRASE = 16

# Phase-lock tolerance: an onset within this fraction of a beat period of a
# predicted beat is treated as supporting that beat.
_PHASE_TOL_FRAC = 0.28
# PLL correction gains.
_GAIN_FAST = 0.45   # while not yet locked → snap quickly (<~2 s)
_GAIN_SLOW = 0.12   # once locked → gentle tracking
# Period adaptation gain (only used when no hard reference BPM).
_PERIOD_GAIN = 0.04


def fold_bpm(bpm: float, lo: float = _FELT_LO, hi: float = _FELT_HI) -> float:
    """Octave-fold a BPM into the felt window [lo, hi)."""
    if not bpm or bpm <= 0:
        return 0.0
    b = float(bpm)
    # Guard against pathological inputs.
    for _ in range(8):
        if b < lo:
            b *= 2.0
        elif b >= hi:
            b /= 2.0
        else:
            break
    return b


class BeatGrid:
    """Maintains a phase-locked 4/4 beat grid with phrase tracking."""

    def __init__(self, beats_per_bar: int = _BEATS_PER_BAR) -> None:
        self.beats_per_bar = int(beats_per_bar) or 4
        # Onset buffers (time in seconds on the caller's clock).
        self._pending: Deque[Tuple[float, float, str]] = deque()       # not yet phase-processed
        self._recent: Deque[Tuple[float, float, str]] = deque()        # for IOI / downbeat windows
        # Reference BPM (db/tap). source: "db" | "tap" | None.
        self._ref_bpm: Optional[float] = None
        self._ref_source: Optional[str] = None
        # Energy (short-term) fed per frame, accumulated per bar.
        self._energy: float = 0.0
        self.reset(hard=True)

    # ------------------------------------------------------------------ reset
    def reset(self, hard: bool = True) -> None:
        """Clear phase/counters. ``hard`` also drops tempo + phrase learning."""
        self._period: float = 0.0           # seconds per beat (0 = unknown)
        self._next_beat_t: Optional[float] = None
        self._beat_index: int = -1          # last emitted beat (-1 = none yet)
        self._downbeat_offset: int = 0      # which raw beat-pos is "the 1"
        self._pos_strength: List[float] = [0.0] * self.beats_per_bar  # EMA per raw pos
        self._phase_err: Deque[float] = deque(maxlen=16)
        self._bar_energy: Deque[float] = deque(maxlen=72)
        self._bar_energy_accum: float = 0.0
        self._bar_energy_n: int = 0
        self._phrase_len: int = _DEFAULT_PHRASE
        self._beats_supported: int = 0      # beats that had a nearby onset
        self._pending.clear()
        self._recent.clear()
        if hard:
            self._period = 0.0
            self._ref_bpm = None
            self._ref_source = None

    # ------------------------------------------------------------- references
    def set_reference_bpm(self, bpm: Optional[float], source: str = "db") -> None:
        """Set an authoritative tempo. ``tap`` outranks ``db``; both fold to felt."""
        if bpm is None or bpm <= 0:
            if source == self._ref_source:
                self._ref_bpm = None
                self._ref_source = None
            return
        # tap always wins; db only sets if no tap currently in force.
        if self._ref_source == "tap" and source != "tap":
            return
        self._ref_bpm = fold_bpm(float(bpm))
        self._ref_source = source
        self._period = 60.0 / self._ref_bpm

    def clear_tap(self) -> None:
        if self._ref_source == "tap":
            self._ref_bpm = None
            self._ref_source = None

    # ------------------------------------------------------------------ input
    def add_onset(self, t: float, strength: float = 1.0, kind: str = "kick") -> None:
        """Register a percussive onset at time ``t`` (seconds)."""
        s = max(0.0, float(strength))
        self._pending.append((float(t), s, kind))
        self._recent.append((float(t), s, kind))

    def set_energy(self, energy: float) -> None:
        self._energy = max(0.0, float(energy))

    # ----------------------------------------------------------- core update
    def update(self, now: float) -> Dict[str, Any]:
        """Advance the grid to ``now`` and return the current state dict."""
        now = float(now)
        self._trim_recent(now)

        if self._period <= 0:
            self._seed_tempo()

        # Establish the grid origin once we have a period and at least one onset.
        if self._next_beat_t is None and self._period > 0 and self._recent:
            last_t = self._recent[-1][0]
            self._next_beat_t = last_t + self._period

        # Phase-lock against onsets that arrived since last update.
        self._process_phase(now)

        # Emit beats up to `now`.
        if self._next_beat_t is not None and self._period > 0:
            guard = 0
            while now >= self._next_beat_t and guard < 256:
                self._emit_beat(self._next_beat_t)
                self._next_beat_t += self._period
                guard += 1

        return self.state(now)

    # ----------------------------------------------------------- high level
    def observe(self, now: float, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """Feed an ``AudioAnalyzer.snapshot()`` dict and advance the grid.

        New kick/snare onsets are detected from the per-frame flags; because the
        analyzer and the render loop use different clocks, onsets are timestamped
        at the current frame ``now`` (≤ ~25 ms quantization, well within the PLL
        tolerance).
        """
        if not isinstance(snapshot, dict):
            return self.update(now)

        # Reference BPM authority: tap > db > audio.
        src = str(snapshot.get("bpm_source") or "auto").lower()
        if src == "tap":
            self.set_reference_bpm(snapshot.get("bpm"), "tap")
        else:
            self.clear_tap()
            db_bpm = snapshot.get("db_bpm")  # set by metadata layer when available
            if db_bpm:
                self.set_reference_bpm(db_bpm, "db")

        # Detect new onsets via the monotonic counts.
        kc = int(snapshot.get("kick_count", 0) or 0)
        sc = int(snapshot.get("snare_count", 0) or 0)
        if not hasattr(self, "_last_kc"):
            self._last_kc = kc
            self._last_sc = sc
        if kc > self._last_kc:
            self.add_onset(now, float(snapshot.get("beat_intensity", 1.0) or 1.0), "kick")
        if sc > self._last_sc:
            self.add_onset(now, float(snapshot.get("snare_intensity", 0.7) or 0.7), "snare")
        self._last_kc = kc
        self._last_sc = sc

        self.set_energy(float(snapshot.get("rms", 0.0) or 0.0))
        return self.update(now)

    # -------------------------------------------------------------- internals
    def _trim_recent(self, now: float, window_s: float = 8.0) -> None:
        while self._recent and (now - self._recent[0][0]) > window_s:
            self._recent.popleft()

    def _seed_tempo(self) -> None:
        """Estimate a period from recent inter-onset intervals (no reference)."""
        if self._ref_bpm:
            self._period = 60.0 / self._ref_bpm
            return
        times = [t for (t, _s, _k) in self._recent if _k == "kick"]
        if len(times) < 4:
            return
        iois = [b - a for a, b in zip(times, times[1:]) if 0.05 < (b - a) < 2.0]
        if len(iois) < 3:
            return
        # Fold each interval into a felt beat period, then take the median.
        periods = sorted(60.0 / fold_bpm(60.0 / x) for x in iois)
        self._period = periods[len(periods) // 2]

    def _process_phase(self, now: float) -> None:
        if self._next_beat_t is None or self._period <= 0:
            self._pending.clear()
            return
        gain = _GAIN_SLOW if self.locked else _GAIN_FAST
        tol = self._period * _PHASE_TOL_FRAC
        leftover: Deque[Tuple[float, float, str]] = deque()
        for (t, s, kind) in self._pending:
            if t > now:
                leftover.append((t, s, kind))
                continue
            # Nearest predicted beat to this onset.
            k = round((t - self._next_beat_t) / self._period)
            nearest = self._next_beat_t + k * self._period
            err = t - nearest
            if abs(err) <= tol:
                # PLL: shift the whole grid toward the onset.
                self._next_beat_t += gain * err
                self._phase_err.append(err / self._period)
                self._beats_supported += 1
                # Adapt period slightly when running on audio only.
                if self._ref_source is None and abs(err) < tol:
                    self._period += _PERIOD_GAIN * err * 0.25
        self._pending = leftover

    def _emit_beat(self, beat_t: float) -> None:
        self._beat_index += 1
        raw_pos = self._beat_index % self.beats_per_bar
        # Downbeat histogram: strongest kick near this beat informs "the 1".
        strength = self._onset_strength_near(beat_t)
        self._pos_strength[raw_pos] += 0.10 * (strength - self._pos_strength[raw_pos])
        # Accumulate per-bar energy; close the bar on the last beat of it.
        self._bar_energy_accum += self._energy + strength
        self._bar_energy_n += 1
        if raw_pos == self.beats_per_bar - 1:
            self._close_bar()
        self._update_downbeat()

    def _onset_strength_near(self, beat_t: float) -> float:
        tol = self._period * _PHASE_TOL_FRAC if self._period > 0 else 0.1
        best = 0.0
        for (t, s, kind) in self._recent:
            if kind != "kick":
                continue
            if abs(t - beat_t) <= tol and s > best:
                best = s
        return best

    def _update_downbeat(self) -> None:
        # Pick the bar position with the strongest kick as the downbeat, with
        # hysteresis: only switch if it clearly beats the current downbeat.
        if max(self._pos_strength) <= 0:
            return
        best_pos = max(range(self.beats_per_bar), key=lambda i: self._pos_strength[i])
        if best_pos != self._downbeat_offset:
            if self._pos_strength[best_pos] > self._pos_strength[self._downbeat_offset] * 1.25:
                self._downbeat_offset = best_pos

    def _close_bar(self) -> None:
        if self._bar_energy_n > 0:
            self._bar_energy.append(self._bar_energy_accum / self._bar_energy_n)
        self._bar_energy_accum = 0.0
        self._bar_energy_n = 0
        self._detect_phrase_len()

    def _detect_phrase_len(self) -> None:
        n = len(self._bar_energy)
        if n < 2 * _PHRASE_CANDIDATES[0]:
            return
        energies = list(self._bar_energy)
        mean = sum(energies) / n
        dev = [e - mean for e in energies]
        denom = sum(d * d for d in dev) or 1e-9
        best_lag, best_score = _DEFAULT_PHRASE, -1.0
        for lag in _PHRASE_CANDIDATES:
            if n < 2 * lag:
                continue
            acc = sum(dev[i] * dev[i - lag] for i in range(lag, n))
            score = acc / denom
            if score > best_score:
                best_score, best_lag = score, lag
        # Only trust a clear periodicity; otherwise keep the default.
        if best_score > 0.15:
            self._phrase_len = best_lag

    # --------------------------------------------------------------- queries
    @property
    def locked(self) -> bool:
        if self._beats_supported < 4 or len(self._phase_err) < 3:
            return False
        return self.confidence >= 0.5

    @property
    def confidence(self) -> float:
        if not self._phase_err:
            return 0.0
        rms = math.sqrt(sum(e * e for e in self._phase_err) / len(self._phase_err))
        # rms is in beat fractions; 0 → 1.0, _PHASE_TOL_FRAC → ~0.
        c = 1.0 - (rms / _PHASE_TOL_FRAC)
        return max(0.0, min(1.0, c))

    def state(self, now: Optional[float] = None) -> Dict[str, Any]:
        bpm = (60.0 / self._period) if self._period > 0 else 0.0
        beat_in_bar = -1
        bar_index = 0
        if self._beat_index >= 0:
            adj = self._beat_index - self._downbeat_offset
            beat_in_bar = adj % self.beats_per_bar
            bar_index = adj // self.beats_per_bar
        phrase_len = self._phrase_len or _DEFAULT_PHRASE
        bar_in_phrase = bar_index % phrase_len if self._beat_index >= 0 else 0
        bars_to_phrase_end = (phrase_len - 1) - bar_in_phrase
        phrase_index = bar_index // phrase_len if self._beat_index >= 0 else 0

        beat_phase = 0.0
        since_beat = to_next = 0.0
        if now is not None and self._period > 0 and self._next_beat_t is not None:
            to_next = max(0.0, self._next_beat_t - now)
            since_beat = max(0.0, self._period - to_next)
            beat_phase = (since_beat / self._period) if self._period > 0 else 0.0

        return {
            "bpm": round(bpm, 2),
            "bpm_source": self._ref_source or "audio",
            "period_s": round(self._period, 5),
            "locked": self.locked,
            "confidence": round(self.confidence, 3),
            "beat_index": self._beat_index,
            "beat_in_bar": beat_in_bar,
            "is_downbeat": (beat_in_bar == 0),
            "bar_index": bar_index,
            "phrase_len": phrase_len,
            "phrase_index": phrase_index,
            "bar_in_phrase": bar_in_phrase,
            "bars_to_phrase_end": bars_to_phrase_end,
            "beat_phase": round(beat_phase, 3),
            "since_beat_s": round(since_beat, 4),
            "to_next_beat_s": round(to_next, 4),
            "building": self._is_building(bars_to_phrase_end),
        }

    def _is_building(self, bars_to_phrase_end: int) -> bool:
        # "Building" = approaching a phrase boundary with rising bar energy.
        if bars_to_phrase_end > 3 or len(self._bar_energy) < 4:
            return False
        recent = list(self._bar_energy)[-4:]
        return recent[-1] > recent[0] * 1.08
