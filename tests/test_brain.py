"""Unit tests for autolight_brain.MusicBrain — deterministic, no audio/rig."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from autolight_brain import (  # noqa: E402
    MusicBrain, SILENCE, CALM, GROOVE, BUILD, DROP, RELEASE,
)


def _grid(**kw):
    base = dict(bpm=128.0, beat_in_bar=0, is_downbeat=True, beat_phase=0.0,
                locked=True, confidence=0.9, building=False, bars_to_phrase_end=8,
                bar_in_phrase=0, phrase_len=16)
    base.update(kw)
    return base


def _audio(level=2, drop_score=0.0, long_rms=0.1, short_rms=0.1, active=True):
    return {"active": active,
            "structure": {"level": level, "drop_score": drop_score,
                          "long_rms": long_rms, "short_rms": short_rms}}


def test_silence_when_inactive():
    b = MusicBrain()
    d = b.decide(0.0, _grid(), _audio(active=False))
    assert d.intent == SILENCE
    assert d.energy < 0.1


def test_calm_on_low_level():
    b = MusicBrain()
    d = b.decide(1.0, _grid(), _audio(level=0, long_rms=0.02, short_rms=0.018))
    assert d.intent == CALM


def test_groove_on_chorus():
    b = MusicBrain()
    d = b.decide(1.0, _grid(), _audio(level=2, long_rms=0.1, short_rms=0.1))
    assert d.intent == GROOVE


def test_hard_drop_on_level_4():
    b = MusicBrain()
    d = b.decide(1.0, _grid(), _audio(level=4, drop_score=2.0,
                                      long_rms=0.2, short_rms=0.4))
    assert d.intent == DROP
    assert d.energy > 0.8
    assert d.groove_on_kick is True


def test_impact_on_downbeat_at_drop_entry():
    b = MusicBrain()
    d = b.decide(1.0, _grid(is_downbeat=True), _audio(level=4, drop_score=2.0))
    assert d.want_impact is True
    # Not a downbeat → no impact punch.
    d2 = b.decide(1.05, _grid(is_downbeat=False), _audio(level=4, drop_score=2.0))
    assert d2.want_impact is False


def test_anticipation_enters_build_near_phrase_end():
    b = MusicBrain()
    g = _grid(building=True, bars_to_phrase_end=1, bar_in_phrase=15)
    d = b.decide(1.0, g, _audio(level=3, long_rms=0.1, short_rms=0.12))
    assert d.intent == BUILD
    assert 0.0 <= d.build_progress <= 1.0
    assert d.allow_strobe is True   # strobe allowed during build


def test_immediate_recovery_from_collapsed_drop():
    b = MusicBrain()
    # Enter a drop.
    b.decide(1.0, _grid(), _audio(level=4, drop_score=2.0, long_rms=0.3, short_rms=0.5))
    # Energy collapses next frame → must drop to RELEASE immediately, no hold.
    d = b.decide(1.1, _grid(), _audio(level=1, drop_score=0.2,
                                      long_rms=0.3, short_rms=0.1))
    assert d.intent == RELEASE


def test_soft_genre_never_drops_and_no_strobe():
    b = MusicBrain()
    b.set_genre("Smooth Jazz")
    d = b.decide(1.0, _grid(), _audio(level=4, drop_score=3.0,
                                      long_rms=0.2, short_rms=0.6))
    assert d.intent != DROP
    assert d.mode == "soft"
    assert d.allow_strobe is False
    assert d.energy <= 0.6


def test_intensity_ceiling_caps_energy():
    b = MusicBrain()
    b.configure(intensity_ceiling=0.5)
    d = b.decide(1.0, _grid(), _audio(level=4, drop_score=2.0,
                                      long_rms=0.2, short_rms=0.4))
    assert d.energy <= 0.5 + 1e-6


def test_small_venue_reduces_energy():
    b_full = MusicBrain()
    b_small = MusicBrain()
    b_small.configure(small_venue=True)
    a = _audio(level=2, long_rms=0.1, short_rms=0.1)
    e_full = b_full.decide(1.0, _grid(), a).energy
    e_small = b_small.decide(1.0, _grid(), a).energy
    assert e_small < e_full


def test_contrast_spreads_calm_lower():
    high = MusicBrain(); high.configure(contrast=1.0)
    low = MusicBrain();  low.configure(contrast=0.0)
    a = _audio(level=0, long_rms=0.02, short_rms=0.018)
    e_high = high.decide(1.0, _grid(), a).energy
    e_low = low.decide(1.0, _grid(), a).energy
    # Higher contrast pushes calm energy further below the midpoint.
    assert e_high < e_low


def test_palette_scheme_tracks_intent():
    b = MusicBrain()
    calm = b.decide(1.0, _grid(), _audio(level=0, long_rms=0.02, short_rms=0.018))
    assert calm.palette["scheme"] in ("analogous", "warm_analogous")
    assert calm.palette["change_rate"] == "slow"
    drop = b.decide(2.0, _grid(), _audio(level=4, drop_score=2.0,
                                         long_rms=0.2, short_rms=0.4))
    assert drop.palette["scheme"] == "complementary"
    assert drop.palette["change_rate"] == "sharp"
    assert drop.palette["saturation"] > calm.palette["saturation"]


def test_metadata_genre_switches_soft_mode():
    b = MusicBrain()
    d = b.decide(1.0, _grid(), _audio(level=2),
                 metadata={"genre": "Ambient", "musical_key": "Am"})
    assert d.mode == "soft"
    assert d.palette["musical_key"] == "Am"
