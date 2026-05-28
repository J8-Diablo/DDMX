#!/usr/bin/env python3
"""Song-structure prior for the AutoLight ``MusicDirector``.

The director's intent state machine is purely reactive — it only sees the
audio that just played. Most commercial tracks (EDM in particular) follow
predictable percentage-based structures: drop1 around 25-45 %, breakdown
45-62 %, drop2 72-92 %, etc. This module turns that structure into a *soft
prior* the director can use to bias intent decisions when the live audio is
ambiguous, without ever hard-overriding a real audio drop.

Two prior sources, in priority order:

1. **Replay prior** — when the current track has been heard before (with
   ``memory_persistence`` enabled) and ``TrackMemory.section_log`` covers
   enough of the track, we synthesise a percentage-keyed step function from
   the previous listen. Track-specific knowledge beats genre averages.

2. **Genre template** — the user's ``genre_preset`` (or auto-detection by
   BPM + duration) selects one of the four shipped templates (EDM, Pop,
   Rock, Ambient). ``Ambient`` is a sentinel that exists only so a user can
   explicitly opt out of structural priors via the genre preset.

The tracker exposes ``current_prior() -> Optional[(intent, weight,
intensity)]``. ``weight`` ramps in at phase entry / out at phase exit so
boundary crossings don't fight the live audio. ``intensity`` is a
per-phase multiplier on the director's energy ceiling — drop2 sits at 1.10
so the rig reads it as hotter than drop1.
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


log = logging.getLogger(__name__)


# Mirror of MusicDirector's intent ↔ index map. Re-declared here so this
# module has no import dependency on ``autolight_director`` (avoids a cycle).
_INTENT_NAMES = {0: "REST", 1: "DRIFT", 2: "BUILD", 3: "PEAK", 4: "RELEASE", 5: "BREATH"}


# =============================================================================
# Template definitions
# =============================================================================


@dataclass(frozen=True)
class PhaseSpec:
    """A named structural phase covering ``[pct_lo, pct_hi)`` of a track."""
    name: str
    pct_lo: float
    pct_hi: float
    default_intent: str
    intensity: float = 1.0  # multiplies director's ENERGY_CEILINGS for this phase


@dataclass(frozen=True)
class StructureTemplate:
    name: str
    phases: Tuple[PhaseSpec, ...]
    bpm_range: Tuple[float, float]
    duration_range_s: Tuple[float, float]
    # Sentinel templates (currently only Ambient) match a genre but never
    # produce a prior. Useful as an explicit "no structural bias here".
    no_prior: bool = False

    def phase_at(self, pct: float) -> Optional[PhaseSpec]:
        if not self.phases:
            return None
        for phase in self.phases:
            if phase.pct_lo <= pct < phase.pct_hi:
                return phase
        # Past the last hi: stick with the last phase (covers pct == 1.0).
        return self.phases[-1] if pct >= self.phases[-1].pct_hi else None

    def next_phase_after(self, pct: float) -> Optional[PhaseSpec]:
        for phase in self.phases:
            if phase.pct_lo > pct:
                return phase
        return None


# Standard 7-part EDM (Beatport-style commercial drop). drop2 sits hotter
# than drop1 so the rig genuinely reads the track's emotional climax.
EDM_TEMPLATE = StructureTemplate(
    name="edm",
    phases=(
        PhaseSpec("intro",     0.00, 0.08, "DRIFT",   0.65),
        PhaseSpec("buildup1",  0.08, 0.23, "BUILD",   0.85),
        PhaseSpec("drop1",     0.23, 0.45, "PEAK",    1.00),
        PhaseSpec("breakdown", 0.45, 0.62, "BREATH",  0.45),
        PhaseSpec("buildup2",  0.62, 0.72, "BUILD",   0.95),
        PhaseSpec("drop2",     0.72, 0.92, "PEAK",    1.10),
        PhaseSpec("outro",     0.92, 1.00, "RELEASE", 0.55),
    ),
    bpm_range=(120.0, 140.0),
    duration_range_s=(180.0, 480.0),
)

# Pop verse/chorus alternation with a bridge. Less violent ceilings than EDM.
POP_TEMPLATE = StructureTemplate(
    name="pop",
    phases=(
        PhaseSpec("intro",   0.00, 0.06, "DRIFT",   0.60),
        PhaseSpec("verse1",  0.06, 0.22, "DRIFT",   0.75),
        PhaseSpec("chorus1", 0.22, 0.38, "BUILD",   0.95),
        PhaseSpec("verse2",  0.38, 0.52, "DRIFT",   0.75),
        PhaseSpec("chorus2", 0.52, 0.68, "BUILD",   0.95),
        PhaseSpec("bridge",  0.68, 0.78, "BREATH",  0.55),
        PhaseSpec("chorus3", 0.78, 0.94, "PEAK",    1.05),
        PhaseSpec("outro",   0.94, 1.00, "RELEASE", 0.55),
    ),
    bpm_range=(90.0, 120.0),
    duration_range_s=(150.0, 300.0),
)

# Rock — similar shape to pop but with a solo where pop has a bridge, and a
# bigger BPM window covering both ballads and harder rock.
ROCK_TEMPLATE = StructureTemplate(
    name="rock",
    phases=(
        PhaseSpec("intro",   0.00, 0.08, "DRIFT",   0.60),
        PhaseSpec("verse1",  0.08, 0.24, "DRIFT",   0.75),
        PhaseSpec("chorus1", 0.24, 0.40, "BUILD",   0.95),
        PhaseSpec("verse2",  0.40, 0.54, "DRIFT",   0.75),
        PhaseSpec("chorus2", 0.54, 0.68, "BUILD",   0.95),
        PhaseSpec("solo",    0.68, 0.80, "PEAK",    1.05),
        PhaseSpec("chorus3", 0.80, 0.94, "PEAK",    1.10),
        PhaseSpec("outro",   0.94, 1.00, "RELEASE", 0.55),
    ),
    bpm_range=(95.0, 160.0),
    duration_range_s=(180.0, 360.0),
)

# Ambient is a sentinel: it's "selectable" via genre_preset but produces no
# prior. Lets a user say "this is calm music, leave the rig purely audio-led".
AMBIENT_TEMPLATE = StructureTemplate(
    name="ambient",
    phases=(),
    bpm_range=(50.0, 110.0),
    duration_range_s=(120.0, 1200.0),
    no_prior=True,
)


TEMPLATES_BY_GENRE: Dict[str, StructureTemplate] = {
    "edm":     EDM_TEMPLATE,
    "pop":     POP_TEMPLATE,
    "rock":    ROCK_TEMPLATE,
    "ambient": AMBIENT_TEMPLATE,
}

# Auto-detect order: try EDM first, then POP, then ROCK. First match by
# bpm_range + duration_range_s wins. AMBIENT is intentionally excluded — we
# only auto-pick prior-producing templates.
_TEMPLATES_AUTO_DETECT: Tuple[StructureTemplate, ...] = (
    EDM_TEMPLATE, POP_TEMPLATE, ROCK_TEMPLATE,
)


# =============================================================================
# Replay prior — synthesised from a previous listen's section_log
# =============================================================================


@dataclass
class ReplayPrior:
    """Step function over song percentage → intent name.

    Built once when a known track starts (cheap), queried per frame via
    ``intent_at(pct)``. Edges are (pct, intent_name) tuples sorted by pct.
    """
    edges: List[Tuple[float, str]]
    pct_first: float
    pct_last: float

    def intent_at(self, pct: float) -> str:
        # Locate the latest edge whose pct is ≤ ``pct``.
        idx = bisect.bisect_right([e[0] for e in self.edges], pct) - 1
        if idx < 0:
            return self.edges[0][1]
        return self.edges[idx][1]

    def segment_extent(self, pct: float) -> Tuple[float, float]:
        """Return ``(seg_lo, seg_hi)`` enclosing ``pct``. Used for weight ramping."""
        pcts = [e[0] for e in self.edges]
        idx = bisect.bisect_right(pcts, pct) - 1
        if idx < 0:
            return (0.0, pcts[0] if pcts else 1.0)
        seg_lo = pcts[idx]
        seg_hi = pcts[idx + 1] if idx + 1 < len(pcts) else 1.0
        return (seg_lo, seg_hi)


# Minimum coverage of [0, 1] required before we'll trust a replay prior.
# A track only partially logged (e.g. user skipped halfway) shouldn't drive
# intent decisions for sections we never observed.
_REPLAY_COVERAGE_MIN = 0.60


def build_replay_prior(
    section_log: List[List[float]],
    duration_ms: int,
) -> Optional[ReplayPrior]:
    """Compress a stored ``section_log`` into a percentage-keyed step function.

    ``section_log`` is the format ``MusicDirector`` writes:
    ``[[elapsed_seconds, intent_index], ...]``. We renormalise to track
    percentage and discard duplicate/out-of-order entries.
    """
    if duration_ms <= 30_000:
        return None
    duration_s = duration_ms / 1000.0
    if not section_log:
        return None

    raw: List[Tuple[float, str]] = []
    for entry in section_log:
        try:
            elapsed = float(entry[0])
            intent_idx = int(entry[1])
        except (ValueError, TypeError, IndexError):
            continue
        if elapsed < 0.0 or elapsed > duration_s * 1.05:
            continue
        intent_name = _INTENT_NAMES.get(intent_idx)
        if intent_name is None:
            continue
        pct = max(0.0, min(1.0, elapsed / duration_s))
        raw.append((pct, intent_name))
    raw.sort(key=lambda t: t[0])

    # Collapse adjacent duplicates (same intent in a row).
    edges: List[Tuple[float, str]] = []
    for pct, intent in raw:
        if edges and edges[-1][1] == intent:
            continue
        edges.append((pct, intent))
    if len(edges) < 2:
        return None

    coverage = edges[-1][0] - edges[0][0]
    if coverage < _REPLAY_COVERAGE_MIN:
        return None

    return ReplayPrior(edges=edges, pct_first=edges[0][0], pct_last=edges[-1][0])


# =============================================================================
# StructureTracker — per-frame state + prior synthesis
# =============================================================================


# How long (s) of stable BPM we wait before locking in an auto-detected
# template. Stable = within ±5 BPM of the first observed BPM.
_AUTO_DETECT_HOLD_S = 30.0
_AUTO_DETECT_BPM_TOL = 5.0

# Seek tolerance: how big a position discrepancy (ms) counts as a seek
# rather than normal jitter. 8 s is loose enough to forgive timing noise
# but tight enough to catch a deliberate skip.
_SEEK_THRESHOLD_MS = 8000.0
_SEEK_FREEZE_S = 2.0

# DJ-mix detection: if position barely advances while audio is active, we're
# probably in a loop / mix and structural priors will be misleading.
_DJ_MIX_MIN_RATE = 0.30
_DJ_MIX_HOLD_S = 10.0

# Phase weight ramps — soft entry / exit so boundary crossings don't whip
# the intent decision.
_PHASE_RAMP_IN = 0.15
_PHASE_RAMP_OUT = 0.10
_PHASE_PEAK_WEIGHT = 1.00
_PHASE_OUT_WEIGHT = 0.50

# Replay weight: high in segment middle, soft at edges so a stored intent
# transition doesn't fight the audio just before it actually flips.
_REPLAY_PEAK_WEIGHT = 0.85
_REPLAY_EDGE_WEIGHT = 0.50
_REPLAY_EDGE_FRAC = 0.10  # first/last 10 % of each segment


class StructureTracker:
    """Maintains structural awareness for one ``MusicDirector`` instance.

    Owns the template/replay-prior selection state, frame-by-frame phase
    resolution, and seek/DJ-mix detection. The director queries
    ``current_prior()`` once per intent decision.
    """

    def __init__(self) -> None:
        self.template: Optional[StructureTemplate] = None
        self.template_source: str = "none"  # "genre" | "auto" | "replay" | "none"
        self.replay_prior: Optional[ReplayPrior] = None

        self.song_pct: float = 0.0
        self.current_phase: Optional[PhaseSpec] = None
        self.phase_progress: float = 0.0
        self.next_phase: Optional[PhaseSpec] = None
        self.next_phase_eta_s: float = 0.0
        self.position_valid: bool = False

        # Per-track state — reset on track-id change.
        self._track_id: Optional[str] = None
        self._duration_ms: int = 0
        self._track_started_wall: float = 0.0

        # Seek + jitter detection.
        self._last_position_ms: int = -1
        self._last_position_wall: float = 0.0
        self._seek_freeze_until: float = 0.0

        # Auto-detect window: armed on track change, locked once a template
        # is picked or the window expires.
        self._auto_detect_armed_until: float = 0.0
        self._auto_first_bpm: float = 0.0
        self._auto_locked: bool = False

        # DJ-mix detection: if a track-id is here, we've decided its position
        # data is unreliable and won't apply any structural prior for it.
        self._dj_mix_disabled: Set[str] = set()
        self._dj_mix_suspect_since: Optional[float] = None

    # ------------------------------------------------------------------

    def update(
        self,
        track_meta: Optional[Dict[str, Any]],
        audio_bpm: float,
        now: float,
        track_memory: Any,  # autolight_director.TrackMemory or None
        genre_preset: str,
        prior_mode: str,
    ) -> None:
        if prior_mode != "auto":
            self._reset_state()
            return

        title = (track_meta or {}).get("title")
        artist = (track_meta or {}).get("artist")
        position_ms = (track_meta or {}).get("position_ms")
        duration_ms = (track_meta or {}).get("duration_ms")
        is_playing = (track_meta or {}).get("is_playing", True)

        track_id = self._make_track_id(title, artist) if title else None

        # New track → reset per-track state and arm auto-detect.
        if track_id != self._track_id:
            self._track_id = track_id
            self._duration_ms = int(duration_ms or 0)
            self._track_started_wall = now
            self._last_position_ms = -1
            self._last_position_wall = 0.0
            self._seek_freeze_until = 0.0
            self._auto_detect_armed_until = now + _AUTO_DETECT_HOLD_S
            self._auto_first_bpm = audio_bpm
            self._auto_locked = False
            self._dj_mix_suspect_since = None
            self.template = None
            self.template_source = "none"
            self.replay_prior = None

            # Try replay prior immediately (track_memory needs listen_count >= 2).
            if track_id and track_memory is not None:
                listen_count = int(getattr(track_memory, "listen_count", 0) or 0)
                tm_duration = int(getattr(track_memory, "duration_ms", 0) or 0) or self._duration_ms
                section_log = list(getattr(track_memory, "section_log", []) or [])
                if listen_count >= 2 and tm_duration > 0:
                    rp = build_replay_prior(section_log, tm_duration)
                    if rp is not None:
                        self.replay_prior = rp
                        self.template_source = "replay"

        # Validity gates: we need both position and a non-trivial duration,
        # and the track must actually be playing. Anything else and we let
        # the director fall back to pure audio.
        if (
            position_ms is None
            or duration_ms is None
            or int(duration_ms) <= 30_000
            or not is_playing
        ):
            self.position_valid = False
            self.current_phase = None
            self.phase_progress = 0.0
            self.next_phase = None
            self.next_phase_eta_s = 0.0
            return

        position_ms = int(position_ms)
        duration_ms = int(duration_ms)

        # Seek detection — large discrepancy between expected and observed
        # position deltas. Freeze prior briefly so audio re-establishes.
        if self._last_position_ms >= 0:
            wall_delta_ms = max(0.0, (now - self._last_position_wall) * 1000.0)
            pos_delta_ms = position_ms - self._last_position_ms
            if abs(pos_delta_ms - wall_delta_ms) > _SEEK_THRESHOLD_MS:
                self._seek_freeze_until = now + _SEEK_FREEZE_S
            # DJ-mix suspicion: position barely advances while audio plays.
            if (
                wall_delta_ms > 1000.0
                and pos_delta_ms >= 0
                and pos_delta_ms < wall_delta_ms * _DJ_MIX_MIN_RATE
            ):
                if self._dj_mix_suspect_since is None:
                    self._dj_mix_suspect_since = now
                elif (now - self._dj_mix_suspect_since) >= _DJ_MIX_HOLD_S and self._track_id:
                    self._dj_mix_disabled.add(self._track_id)
                    self._dj_mix_suspect_since = None
            else:
                self._dj_mix_suspect_since = None

        self._last_position_ms = position_ms
        self._last_position_wall = now

        # Pick a template if we don't already have a replay prior for this
        # track. Replay > genre > auto.
        if self.replay_prior is None:
            if not self._auto_locked:
                self._select_template(genre_preset, audio_bpm, duration_ms, now)

        # If this track was blacklisted as a DJ-mix, suppress the prior.
        if self._track_id and self._track_id in self._dj_mix_disabled:
            self.template_source = "none"
            self.template = None
            self.replay_prior = None

        # Compute song percentage + active phase.
        pct = max(0.0, min(1.0, position_ms / duration_ms))
        self.song_pct = pct
        self.position_valid = True

        if self.template is not None and not self.template.no_prior:
            phase = self.template.phase_at(pct)
            self.current_phase = phase
            if phase is not None and phase.pct_hi > phase.pct_lo:
                self.phase_progress = (pct - phase.pct_lo) / (phase.pct_hi - phase.pct_lo)
                self.phase_progress = max(0.0, min(1.0, self.phase_progress))
            else:
                self.phase_progress = 0.0
            nxt = self.template.next_phase_after(pct)
            self.next_phase = nxt
            if nxt is not None:
                remain_pct = max(0.0, nxt.pct_lo - pct)
                self.next_phase_eta_s = remain_pct * (duration_ms / 1000.0)
            else:
                self.next_phase_eta_s = 0.0
        else:
            self.current_phase = None
            self.phase_progress = 0.0
            self.next_phase = None
            self.next_phase_eta_s = 0.0

    # ------------------------------------------------------------------

    def current_prior(self) -> Optional[Tuple[str, float, float]]:
        """Return ``(intent_name, weight ∈ [0,1], intensity)`` or None.

        Intensity defaults to 1.0 when sourced from a replay prior — replay
        priors only carry intent transitions, not energy multipliers.
        """
        if not self.position_valid:
            return None
        if self._seek_freeze_until > self._last_position_wall:
            # Still inside the freeze window from the most recent update tick.
            return None

        if self.template_source == "replay" and self.replay_prior is not None:
            seg_lo, seg_hi = self.replay_prior.segment_extent(self.song_pct)
            seg_span = max(1e-6, seg_hi - seg_lo)
            seg_progress = (self.song_pct - seg_lo) / seg_span
            edge = _REPLAY_EDGE_FRAC
            if seg_progress < edge:
                weight = _REPLAY_EDGE_WEIGHT + (
                    _REPLAY_PEAK_WEIGHT - _REPLAY_EDGE_WEIGHT
                ) * (seg_progress / edge)
            elif seg_progress > 1.0 - edge:
                weight = _REPLAY_PEAK_WEIGHT - (
                    _REPLAY_PEAK_WEIGHT - _REPLAY_EDGE_WEIGHT
                ) * ((seg_progress - (1.0 - edge)) / edge)
            else:
                weight = _REPLAY_PEAK_WEIGHT
            return (self.replay_prior.intent_at(self.song_pct), weight, 1.0)

        if self.template is None or self.template.no_prior or self.current_phase is None:
            return None

        # Template prior: ramp in over first 15 % of phase, hold, decay out
        # over last 10 %. Keeps the prior from yanking the intent at the
        # exact moment of a phase boundary.
        progress = self.phase_progress
        if progress < _PHASE_RAMP_IN:
            weight = _PHASE_PEAK_WEIGHT * (progress / _PHASE_RAMP_IN)
        elif progress > 1.0 - _PHASE_RAMP_OUT:
            weight = _PHASE_PEAK_WEIGHT - (
                _PHASE_PEAK_WEIGHT - _PHASE_OUT_WEIGHT
            ) * ((progress - (1.0 - _PHASE_RAMP_OUT)) / _PHASE_RAMP_OUT)
        else:
            weight = _PHASE_PEAK_WEIGHT
        return (self.current_phase.default_intent, weight, self.current_phase.intensity)

    # ------------------------------------------------------------------

    def diagnostics(self) -> Dict[str, Any]:
        # ``input_*`` fields surface what the tracker actually received from
        # the media probe last frame. Use them to debug "why isn't the prior
        # firing?" — most often the answer is the player isn't reporting a
        # position (browser playback, some Windows apps), not that the
        # tracker is buggy.
        return {
            "phase": self.current_phase.name if self.current_phase else None,
            "phase_progress": round(self.phase_progress, 3),
            "song_progress": round(self.song_pct, 4),
            "source": self.template_source,
            "template_name": self.template.name if self.template else (
                "replay" if self.template_source == "replay" else None
            ),
            "next_phase": self.next_phase.name if self.next_phase else None,
            "next_phase_eta_s": round(self.next_phase_eta_s, 2),
            "position_valid": self.position_valid,
            "track_id": self._track_id,
            "input_position_ms": self._last_position_ms if self._last_position_ms >= 0 else None,
            "input_duration_ms": self._duration_ms or None,
            "auto_locked": self._auto_locked,
            "seek_frozen": self._seek_freeze_until > self._last_position_wall,
        }

    # ==================================================================
    # Internals
    # ==================================================================

    def _reset_state(self) -> None:
        self.template = None
        self.template_source = "none"
        self.replay_prior = None
        self.song_pct = 0.0
        self.current_phase = None
        self.phase_progress = 0.0
        self.next_phase = None
        self.next_phase_eta_s = 0.0
        self.position_valid = False
        self._auto_locked = False
        self._auto_detect_armed_until = 0.0

    def _select_template(
        self, genre_preset: str, audio_bpm: float, duration_ms: int, now: float,
    ) -> None:
        genre = (genre_preset or "auto").strip().lower()

        # Explicit genre wins.
        if genre in TEMPLATES_BY_GENRE:
            self.template = TEMPLATES_BY_GENRE[genre]
            self.template_source = "genre"
            self._auto_locked = True
            return

        # Auto-detect: requires the BPM to have stabilised. We watch the BPM
        # for AUTO_DETECT_HOLD_S; if it's stable we lock a template.
        if genre != "auto":
            self.template = None
            self.template_source = "none"
            self._auto_locked = True
            return

        if audio_bpm < 40.0:
            return  # no estimate yet — keep waiting

        if abs(audio_bpm - self._auto_first_bpm) > _AUTO_DETECT_BPM_TOL:
            # BPM drifted — restart the window with the new value.
            self._auto_first_bpm = audio_bpm
            self._auto_detect_armed_until = now + _AUTO_DETECT_HOLD_S
            return

        if now < self._auto_detect_armed_until:
            return  # still inside the warm-up window

        duration_s = duration_ms / 1000.0
        for tpl in _TEMPLATES_AUTO_DETECT:
            if tpl.bpm_range[0] <= audio_bpm <= tpl.bpm_range[1] and (
                tpl.duration_range_s[0] <= duration_s <= tpl.duration_range_s[1]
            ):
                self.template = tpl
                self.template_source = "auto"
                self._auto_locked = True
                return

        # No match — record that we tried so we don't keep retrying.
        self.template = None
        self.template_source = "none"
        self._auto_locked = True

    @staticmethod
    def _make_track_id(title: Optional[str], artist: Optional[str]) -> str:
        t = (title or "").strip().lower()
        a = (artist or "").strip().lower()
        return f"{a}::{t}" if a else t
