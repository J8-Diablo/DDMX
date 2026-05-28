"""autolight_brain.py — the decision layer ("what should the show do now?").

Sits above the BeatGrid (timing/meter/anticipation) and the audio snapshot
(energy/level) and below the show layer (which turns decisions into DMX). It is
the part that behaves like a DJ: it builds tension before a drop, contrasts
calm sections hard against peaks, recovers instantly from a false trigger, and
adapts to soft genres.

Encodes the interview decisions (see RELEASE-0.4.0-SPEC.md §2.3):
  * Very high energy contrast (calm very low, drop explodes).
  * Breakdowns reduced strongly to build tension.
  * Anticipation: once the phrase is locked, prepare the drop in the last bars.
  * Energetic bias BUT immediate graceful recovery from false drops.
  * Transitions aligned to phrase boundaries + immediate on a detected drop.
  * Soft genres (jazz/ambient/…) auto-switch to a calmer "soft" mode.
  * Guardrails: global intensity ceiling + small-venue preset.

Pure Python and deterministic: feed synthetic grid + audio dicts and assert the
emitted Directive. No audio / no rig needed for tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# Intent vocabulary.
SILENCE = "silence"   # no audio (or DJ gap between tracks)
CALM = "calm"         # intro / breakdown / ambient — strong reduction
GROOVE = "groove"     # verse/chorus sustained energy
BUILD = "build"       # rising toward a drop (anticipation)
DROP = "drop"         # the peak — impact then groove on the kick
RELEASE = "release"   # just after a drop / a collapsed false trigger

# Genres that flip the brain into a calmer "soft" mode.
_SOFT_GENRES = (
    "jazz", "ambient", "classical", "acoustic", "blues", "soul", "lounge",
    "chill", "downtempo", "folk", "piano", "orchestral", "soundtrack",
)

# Per-intent baseline target energy (before contrast + ceiling).
_BASE_ENERGY = {
    SILENCE: 0.04,
    CALM: 0.14,
    GROOVE: 0.52,
    BUILD: 0.62,   # ramps up via build_progress
    DROP: 1.0,
    RELEASE: 0.30,
}


@dataclass
class Directive:
    """The brain's per-frame instruction to the show layer."""
    intent: str = CALM
    energy: float = 0.0           # 0..1 overall target intensity (post-guardrails)
    mode: str = "club"            # "club" | "soft"
    allow_strobe: bool = False
    want_impact: bool = False     # punch on the downbeat of a drop
    groove_on_kick: bool = False  # pulse effects on each kick
    build_progress: float = 0.0   # 0..1 across an anticipated build
    bars_to_drop: int = -1        # bars until the anticipated phrase boundary (-1 = n/a)
    contrast: float = 1.0         # 0..1 how hard calm↔peak is spread
    palette: Dict[str, Any] = field(default_factory=dict)
    # Passthrough timing for the show layer.
    bpm: float = 0.0
    beat_in_bar: int = -1
    is_downbeat: bool = False
    beat_phase: float = 0.0
    locked: bool = False
    confidence: float = 0.0
    guardrails: Dict[str, Any] = field(default_factory=dict)


class MusicBrain:
    """Stateful decision maker. One instance per AutoLight session."""

    def __init__(self) -> None:
        # Tunables (settable via configure()).
        self.intensity_ceiling: float = 1.0   # global cap on energy
        self.small_venue: bool = False         # sober preset
        self.contrast: float = 1.0             # 0..1; user picked "very contrasted"
        self.energetic_bias: bool = True       # react quick, recover quick
        self.allow_strobe_global: bool = True
        self.build_lead_bars: int = 2          # how early to start anticipating
        self.drop_score_threshold: float = 1.5
        self._genre: str = ""
        self._soft: bool = False
        self.reset()

    # ------------------------------------------------------------- config
    def configure(self, **kw: Any) -> None:
        for k in ("intensity_ceiling", "contrast", "build_lead_bars",
                  "drop_score_threshold"):
            if k in kw and kw[k] is not None:
                setattr(self, k, type(getattr(self, k))(kw[k]))
        for k in ("small_venue", "energetic_bias", "allow_strobe_global"):
            if k in kw and kw[k] is not None:
                setattr(self, k, bool(kw[k]))
        self.intensity_ceiling = max(0.05, min(1.0, self.intensity_ceiling))
        self.contrast = max(0.0, min(1.0, self.contrast))

    def set_genre(self, genre: Optional[str]) -> None:
        self._genre = (genre or "").strip().lower()
        self._soft = any(g in self._genre for g in _SOFT_GENRES)

    def reset(self) -> None:
        self._intent: str = CALM
        self._intent_since: float = 0.0
        self._silence_since: Optional[float] = None
        self._last_drop_t: float = -1e9
        self._energy_smooth: float = 0.0

    # ------------------------------------------------------------- decide
    def decide(self, now: float, grid: Dict[str, Any], audio: Dict[str, Any],
               metadata: Optional[Dict[str, Any]] = None) -> Directive:
        now = float(now)
        if metadata and metadata.get("genre"):
            self.set_genre(metadata.get("genre"))

        active = bool(audio.get("active"))
        structure = audio.get("structure") or {}
        level = int(structure.get("level", 0) or 0)
        drop_score = float(structure.get("drop_score", 0.0) or 0.0)
        long_rms = float(structure.get("long_rms", 0.0) or 0.0)
        short_rms = float(structure.get("short_rms", 0.0) or 0.0)
        rms_ratio = (short_rms / long_rms) if long_rms > 1e-6 else 1.0

        building = bool(grid.get("building"))
        bars_to_end = int(grid.get("bars_to_phrase_end", -1))
        bar_in_phrase = int(grid.get("bar_in_phrase", 0))
        phrase_len = int(grid.get("phrase_len", 16) or 16)

        intent = self._choose_intent(
            now, active, level, drop_score, rms_ratio, building, bars_to_end,
        )
        if intent != self._intent:
            self._intent = intent
            self._intent_since = now
            if intent == DROP:
                self._last_drop_t = now

        # Build progress (0..1) when anticipating.
        build_progress = 0.0
        bars_to_drop = -1
        if intent == BUILD and phrase_len > 0:
            lead = max(1, self.build_lead_bars)
            bars_to_drop = max(0, bars_to_end)
            build_progress = max(0.0, min(1.0, 1.0 - (bars_to_end / float(lead))))
            # Fine-grained ramp within the bar via beat phase.
            bp = float(grid.get("beat_in_bar", 0)) / 4.0 + float(grid.get("beat_phase", 0.0)) / 4.0
            build_progress = min(1.0, build_progress + bp * (1.0 / max(1, lead)) * 0.5)

        energy = self._energy_for(intent, build_progress, rms_ratio)
        mode = "soft" if self._soft else "club"

        allow_strobe = (
            self.allow_strobe_global and not self._soft and intent in (BUILD, DROP)
        )
        want_impact = (
            intent == DROP and bool(grid.get("is_downbeat"))
            and (now - self._last_drop_t) < 0.35
        )
        groove_on_kick = intent == DROP and not self._soft

        return Directive(
            intent=intent,
            energy=energy,
            mode=mode,
            allow_strobe=allow_strobe,
            want_impact=want_impact,
            groove_on_kick=groove_on_kick,
            build_progress=round(build_progress, 3),
            bars_to_drop=bars_to_drop,
            contrast=self.contrast,
            palette=self._palette(intent, energy, metadata),
            bpm=float(grid.get("bpm", 0.0) or 0.0),
            beat_in_bar=int(grid.get("beat_in_bar", -1)),
            is_downbeat=bool(grid.get("is_downbeat")),
            beat_phase=float(grid.get("beat_phase", 0.0) or 0.0),
            locked=bool(grid.get("locked")),
            confidence=float(grid.get("confidence", 0.0) or 0.0),
            guardrails={
                "intensity_ceiling": self.intensity_ceiling,
                "small_venue": self.small_venue,
                "soft": self._soft,
            },
        )

    # ----------------------------------------------------------- internals
    def _choose_intent(self, now, active, level, drop_score, rms_ratio,
                       building, bars_to_end) -> str:
        if not active:
            if self._silence_since is None:
                self._silence_since = now
            return SILENCE
        self._silence_since = None

        prev = self._intent

        # Hard drop rule (energetic bias: react immediately).
        is_drop = (level >= 4) or (drop_score >= self.drop_score_threshold and level >= 3)
        if self._soft:
            is_drop = False  # soft mode never "explodes"
        if is_drop:
            return DROP

        # Immediate recovery from a collapsed drop / false trigger.
        if prev == DROP:
            if rms_ratio < 0.8 or level <= 1:
                return RELEASE
            return DROP  # still peaking

        # Anticipation: rising energy approaching a phrase boundary.
        if (not self._soft) and building and bars_to_end <= self.build_lead_bars \
                and level >= 2 and rms_ratio >= 1.02:
            return BUILD

        # Stay in RELEASE briefly to let the peak breathe out.
        if prev == RELEASE and (now - self._intent_since) < 1.0:
            return RELEASE

        if level <= 0:
            return CALM
        if level == 1:
            return CALM if rms_ratio < 0.95 else GROOVE
        return GROOVE

    def _energy_for(self, intent: str, build_progress: float, rms_ratio: float) -> float:
        base = _BASE_ENERGY.get(intent, 0.3)
        if intent == BUILD:
            base = _BASE_ENERGY[CALM] + (_BASE_ENERGY[DROP] - _BASE_ENERGY[CALM]) * (
                0.4 + 0.6 * build_progress)
        # Apply contrast: spread values away from the midpoint (0.5).
        e = 0.5 + (base - 0.5) * (0.5 + 0.5 * self.contrast)
        # Soft mode is gentler and never maxes out.
        if self._soft:
            e = min(e, 0.6) * 0.8
        # Guardrails.
        e *= self.intensity_ceiling
        if self.small_venue:
            e *= 0.7
        return max(0.0, min(1.0, e))

    def _palette(self, intent: str, energy: float,
                 metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        # Harmony auto by intensity (interview): mono/analogous calm →
        # complementary/contrasted peaks. Saturation tracks energy.
        if intent in (SILENCE, CALM):
            scheme = "analogous"
        elif intent in (GROOVE,):
            scheme = "analogous"
        elif intent == BUILD:
            scheme = "warm_analogous"
        else:  # DROP / RELEASE
            scheme = "complementary"
        saturation = round(min(1.0, 0.35 + 0.65 * energy), 3)
        if self._soft:
            scheme = "analogous"
            saturation = round(min(saturation, 0.5), 3)
        return {
            "scheme": scheme,
            "saturation": saturation,
            "genre": self._genre,
            "musical_key": (metadata or {}).get("musical_key", ""),
            # change cadence: slow when calm, sharp on drops (interview).
            "change_rate": "slow" if intent in (SILENCE, CALM, GROOVE) else "sharp",
        }
