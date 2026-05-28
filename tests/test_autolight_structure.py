"""Tests for the structural-prior layer of AutoLight.

Covers: template phase resolution, replay-prior synthesis, auto-detection,
soft prior blending in the director, and the seek/pause/DJ-mix edge cases.
"""

from __future__ import annotations

import os
import sys

# Make the repo root importable when running pytest from inside ``tests/``.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from autolight_structure import (
    EDM_TEMPLATE,
    POP_TEMPLATE,
    ROCK_TEMPLATE,
    AMBIENT_TEMPLATE,
    StructureTracker,
    build_replay_prior,
)


# -----------------------------------------------------------------------------
# Template phase resolution
# -----------------------------------------------------------------------------


def test_edm_phase_resolution():
    assert EDM_TEMPLATE.phase_at(0.05).name == "intro"
    assert EDM_TEMPLATE.phase_at(0.30).name == "drop1"
    assert EDM_TEMPLATE.phase_at(0.55).name == "breakdown"
    assert EDM_TEMPLATE.phase_at(0.80).name == "drop2"
    assert EDM_TEMPLATE.phase_at(0.95).name == "outro"


def test_pop_phase_resolution():
    assert POP_TEMPLATE.phase_at(0.10).name == "verse1"
    assert POP_TEMPLATE.phase_at(0.30).name == "chorus1"
    assert POP_TEMPLATE.phase_at(0.72).name == "bridge"


def test_rock_phase_resolution():
    assert ROCK_TEMPLATE.phase_at(0.30).name == "chorus1"
    assert ROCK_TEMPLATE.phase_at(0.72).name == "solo"


def test_ambient_is_no_prior_sentinel():
    assert AMBIENT_TEMPLATE.no_prior is True
    assert AMBIENT_TEMPLATE.phases == ()


def test_drop2_is_more_intense_than_drop1():
    drop1 = EDM_TEMPLATE.phase_at(0.30)
    drop2 = EDM_TEMPLATE.phase_at(0.80)
    assert drop1.name == "drop1"
    assert drop2.name == "drop2"
    assert drop2.intensity > drop1.intensity


# -----------------------------------------------------------------------------
# Replay prior synthesis
# -----------------------------------------------------------------------------


def test_replay_prior_basic_lookup():
    # 180 s track, 3 transitions: PEAK at 20 s, RELEASE at 80 s, DRIFT at 150 s.
    rp = build_replay_prior([[20, 3], [80, 4], [150, 1]], 180_000)
    assert rp is not None
    assert rp.intent_at(0.05) == "PEAK"   # before first edge → first intent
    assert rp.intent_at(0.20) == "PEAK"   # 36 s
    assert rp.intent_at(0.50) == "RELEASE"  # 90 s
    assert rp.intent_at(0.95) == "DRIFT"  # past last edge


def test_replay_prior_rejects_low_coverage():
    # Only one transition near the very start — coverage too low.
    rp = build_replay_prior([[5, 3], [10, 4]], 180_000)
    assert rp is None


def test_replay_prior_rejects_short_track():
    rp = build_replay_prior([[5, 3], [60, 4]], 20_000)
    assert rp is None


def test_replay_prior_rejects_invalid_entries():
    # Mixed valid + garbage; should still produce a usable prior.
    rp = build_replay_prior(
        [[20, 3], [80, 99], ["nope", "nope"], [150, 1]],  # 99 = unknown intent
        180_000,
    )
    assert rp is not None
    # Intent index 99 is dropped → only PEAK + DRIFT survive.
    assert {intent for _, intent in rp.edges} == {"PEAK", "DRIFT"}


# -----------------------------------------------------------------------------
# StructureTracker — template selection
# -----------------------------------------------------------------------------


def _track_meta(position_ms=60_000, duration_ms=240_000, is_playing=True,
                title="Test Song", artist="Test Artist"):
    return {
        "title": title,
        "artist": artist,
        "position_ms": position_ms,
        "duration_ms": duration_ms,
        "is_playing": is_playing,
    }


def test_genre_preset_picks_template():
    t = StructureTracker()
    t.update(_track_meta(), 128.0, now=1.0, track_memory=None,
             genre_preset="edm", prior_mode="auto")
    assert t.template_source == "genre"
    assert t.template is EDM_TEMPLATE
    assert t.position_valid


def test_genre_preset_ambient_disables_prior():
    t = StructureTracker()
    t.update(_track_meta(), 90.0, now=1.0, track_memory=None,
             genre_preset="ambient", prior_mode="auto")
    assert t.template_source == "genre"
    assert t.template is AMBIENT_TEMPLATE
    # Even though template is set, no_prior should suppress current_prior.
    assert t.current_prior() is None


def test_auto_detect_needs_stable_bpm_window():
    t = StructureTracker()
    # First tick at BPM 128 — auto-detect window opens.
    t.update(_track_meta(position_ms=1_000), 128.0, now=0.0, track_memory=None,
             genre_preset="auto", prior_mode="auto")
    assert t.template is None  # not enough time passed
    # 5 s later, still 128 — still warming up.
    t.update(_track_meta(position_ms=6_000), 128.0, now=5.0, track_memory=None,
             genre_preset="auto", prior_mode="auto")
    assert t.template is None
    # 35 s later — past the 30 s hold window. Should lock EDM.
    t.update(_track_meta(position_ms=36_000), 128.0, now=35.0, track_memory=None,
             genre_preset="auto", prior_mode="auto")
    assert t.template is EDM_TEMPLATE
    assert t.template_source == "auto"


def test_auto_detect_no_match_for_slow_long_track():
    t = StructureTracker()
    # Slow BPM (75) + long duration (10 min) → no template matches.
    meta = _track_meta(duration_ms=600_000, position_ms=10_000)
    t.update(meta, 75.0, now=0.0, track_memory=None, genre_preset="auto", prior_mode="auto")
    t.update(meta, 75.0, now=35.0, track_memory=None, genre_preset="auto", prior_mode="auto")
    assert t.template is None
    assert t.template_source == "none"


def test_off_mode_disables_everything():
    t = StructureTracker()
    t.update(_track_meta(), 128.0, now=1.0, track_memory=None,
             genre_preset="edm", prior_mode="off")
    assert t.template is None
    assert t.position_valid is False
    assert t.current_prior() is None


# -----------------------------------------------------------------------------
# Edge cases
# -----------------------------------------------------------------------------


def test_unknown_duration_invalidates_position():
    t = StructureTracker()
    meta = _track_meta(duration_ms=0)
    t.update(meta, 128.0, now=1.0, track_memory=None,
             genre_preset="edm", prior_mode="auto")
    assert t.position_valid is False
    assert t.current_prior() is None


def test_pause_invalidates_position():
    t = StructureTracker()
    meta = _track_meta(is_playing=False)
    t.update(meta, 128.0, now=1.0, track_memory=None,
             genre_preset="edm", prior_mode="auto")
    assert t.position_valid is False


def test_seek_freezes_prior():
    t = StructureTracker()
    # Establish a track + a prior.
    t.update(_track_meta(position_ms=30_000), 128.0, now=1.0,
             track_memory=None, genre_preset="edm", prior_mode="auto")
    assert t.current_prior() is not None  # template is engaged
    # Seek forward 60 s while wall-clock only advanced 1 s.
    t.update(_track_meta(position_ms=90_000), 128.0, now=2.0,
             track_memory=None, genre_preset="edm", prior_mode="auto")
    # Now in the seek-freeze window — prior suppressed.
    assert t.current_prior() is None


def test_track_change_resets_template():
    t = StructureTracker()
    t.update(_track_meta(title="A"), 128.0, now=1.0,
             track_memory=None, genre_preset="edm", prior_mode="auto")
    first_track_id = t._track_id
    t.update(_track_meta(title="B"), 128.0, now=2.0,
             track_memory=None, genre_preset="edm", prior_mode="auto")
    assert t._track_id != first_track_id
    # Auto-locked again on the new track because genre is explicit.
    assert t.template is EDM_TEMPLATE


# -----------------------------------------------------------------------------
# Replay prior supersedes template
# -----------------------------------------------------------------------------


class _FakeTrackMemory:
    def __init__(self, listen_count, section_log, duration_ms):
        self.listen_count = listen_count
        self.section_log = section_log
        self.duration_ms = duration_ms


def test_replay_prior_supersedes_genre_template():
    # Track has been heard twice; previous listen logged PEAK → RELEASE → DRIFT.
    tm = _FakeTrackMemory(
        listen_count=3,
        section_log=[[20, 3], [80, 4], [150, 1]],
        duration_ms=180_000,
    )
    t = StructureTracker()
    t.update(
        _track_meta(position_ms=36_000, duration_ms=180_000),
        128.0, now=1.0, track_memory=tm, genre_preset="edm", prior_mode="auto",
    )
    assert t.template_source == "replay"
    prior = t.current_prior()
    assert prior is not None
    intent, _, _ = prior
    # At pct=0.20, replay prior says PEAK (genre template would say buildup1=BUILD).
    assert intent == "PEAK"


def test_replay_prior_skipped_when_only_one_listen():
    tm = _FakeTrackMemory(
        listen_count=1,
        section_log=[[20, 3], [80, 4], [150, 1]],
        duration_ms=180_000,
    )
    t = StructureTracker()
    t.update(
        _track_meta(position_ms=36_000, duration_ms=180_000),
        128.0, now=1.0, track_memory=tm, genre_preset="edm", prior_mode="auto",
    )
    # Falls back to genre template, not replay.
    assert t.template_source == "genre"


# -----------------------------------------------------------------------------
# Integration with MusicDirector intent decision
# -----------------------------------------------------------------------------


def test_director_audio_drop_overrides_prior():
    """A real audio drop (drop_score > 1.5) must always win."""
    from autolight_director import MusicDirector

    md = MusicDirector()
    md.set_genre_preset("edm")
    md.set_structural_prior_mode("auto")

    # Force the tracker to claim we're in breakdown (BREATH prior).
    md._structure.update(
        _track_meta(position_ms=110_000, duration_ms=200_000),
        128.0, now=1.0, track_memory=None, genre_preset="edm", prior_mode="auto",
    )
    assert md._structure.current_phase.name == "breakdown"

    audio = {
        "active": True, "rms": 0.06, "bass": 0.04, "mid": 0.03, "treble": 0.02,
        "bpm": 128.0, "beat_count": 5, "last_beat_ms": 1000.0,
        "structure": {"level": 4, "drop_score": 1.8, "long_rms": 0.06,
                       "short_rms": 0.10, "build_up_slope": 0.0001},
    }
    intent = md._decide_intent(audio, now=2.0)
    assert intent == "PEAK"  # audio wins


def test_director_prior_wins_when_audio_ambiguous():
    """When audio is mid-level + flat, the structural prior should pull us
    toward the phase's expected intent."""
    from autolight_director import MusicDirector

    md = MusicDirector()
    md.set_genre_preset("edm")
    md.set_structural_prior_mode("auto")

    # Position the tracker mid-drop1 (PEAK prior).
    md._structure.update(
        _track_meta(position_ms=60_000, duration_ms=200_000),
        128.0, now=1.0, track_memory=None, genre_preset="edm", prior_mode="auto",
    )
    assert md._structure.current_phase.name == "drop1"

    # Audio is ambiguous: level=2, no clear drop, no clear build/release.
    audio = {
        "active": True, "rms": 0.04, "bass": 0.02, "mid": 0.02, "treble": 0.01,
        "bpm": 128.0, "beat_count": 5, "last_beat_ms": 1000.0,
        "structure": {"level": 2, "drop_score": 0.4, "long_rms": 0.04,
                       "short_rms": 0.04, "build_up_slope": 0.0},
    }
    intent = md._decide_intent(audio, now=2.0)
    # Audio alone would yield DRIFT; the prior pulls us to PEAK.
    assert intent == "PEAK"


def test_director_off_mode_falls_back_to_audio():
    from autolight_director import MusicDirector

    md = MusicDirector()
    md.set_genre_preset("edm")
    md.set_structural_prior_mode("off")

    md._structure.update(
        _track_meta(position_ms=60_000, duration_ms=200_000),
        128.0, now=1.0, track_memory=None, genre_preset="edm", prior_mode="off",
    )
    assert md._structure.current_prior() is None

    audio = {
        "active": True, "rms": 0.04, "bass": 0.02, "mid": 0.02, "treble": 0.01,
        "bpm": 128.0, "beat_count": 5, "last_beat_ms": 1000.0,
        "structure": {"level": 2, "drop_score": 0.4, "long_rms": 0.04,
                       "short_rms": 0.04, "build_up_slope": 0.0},
    }
    intent = md._decide_intent(audio, now=2.0)
    # Pure audio: level=2, ratio=1, slope=0 → DRIFT (the legacy path).
    assert intent == "DRIFT"


def test_drop2_has_higher_energy_ceiling_than_drop1():
    """The intensity multiplier on drop2 (1.10) must produce a ceiling that's
    actually higher than drop1's (1.00) — would clamp to the same value
    without the lowered PEAK base. Uses two trackers so the position jump
    between drop1 and drop2 doesn't get classified as a seek."""
    from autolight_director import MusicDirector

    # Drop1: 60 s into a 200 s track.
    md1 = MusicDirector()
    md1.set_genre_preset("edm")
    md1.set_structural_prior_mode("auto")
    md1._structure.update(
        _track_meta(position_ms=60_000, duration_ms=200_000),
        128.0, now=1.0, track_memory=None, genre_preset="edm", prior_mode="auto",
    )
    assert md1._structure.current_phase.name == "drop1"
    md1._enter_intent("PEAK", now=1.0)
    drop1_ceiling = md1.intent.energy_ceiling

    # Drop2: fresh tracker so the position is the first sample seen.
    md2 = MusicDirector()
    md2.set_genre_preset("edm")
    md2.set_structural_prior_mode("auto")
    md2._structure.update(
        _track_meta(position_ms=160_000, duration_ms=200_000),
        128.0, now=1.0, track_memory=None, genre_preset="edm", prior_mode="auto",
    )
    assert md2._structure.current_phase.name == "drop2"
    md2._enter_intent("PEAK", now=1.0)
    drop2_ceiling = md2.intent.energy_ceiling

    assert drop2_ceiling > drop1_ceiling, (
        f"drop2 ({drop2_ceiling}) should exceed drop1 ({drop1_ceiling})"
    )
