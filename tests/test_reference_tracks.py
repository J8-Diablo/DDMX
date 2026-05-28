"""Reference-track validation: quantifies detection quality on scripted tracks.

Runs the full BeatGrid + MusicBrain pipeline over synthetic tracks with known
BPM / bar count / drop positions and asserts accuracy thresholds. Reproducible
and CI-safe (no audio). The "live by ear" validation is done separately.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest  # noqa: E402

from autolight_validation import reference_tracks, simulate, score  # noqa: E402


# Per-track expectations keyed by track name.
EXPECT = {
    "EDM 128 (drop @16)":    {"has_drop": True,  "lock_max": 2.5, "needs_db": True},
    "House 124 (no db)":     {"has_drop": True,  "lock_max": 8.0, "needs_db": False},
    "DnB 174":               {"has_drop": True,  "lock_max": 2.5, "needs_db": True},
    "Half-time 87 felt-174": {"has_drop": False, "lock_max": 9.0, "needs_db": False},
}


@pytest.fixture(scope="module")
def scores():
    return {t.name: score(t, simulate(t)) for t in reference_tracks()}


@pytest.mark.parametrize("name", list(EXPECT.keys()))
def test_bpm_accuracy(scores, name):
    s = scores[name]
    assert s["bpm_detected"] > 0, "tempo never locked"
    assert s["bpm_error"] <= 2.0, f"{name}: bpm error {s['bpm_error']}"


@pytest.mark.parametrize("name", list(EXPECT.keys()))
def test_grid_locks_in_time(scores, name):
    s = scores[name]
    assert s["locked_at_s"] is not None, f"{name}: never locked"
    assert s["locked_at_s"] <= EXPECT[name]["lock_max"], \
        f"{name}: locked at {s['locked_at_s']}s"


@pytest.mark.parametrize("name", list(EXPECT.keys()))
def test_bar_counting_accurate(scores, name):
    s = scores[name]
    tol = max(2, round(0.12 * s["bars_expected"]))
    assert abs(s["bars_detected"] - s["bars_expected"]) <= tol, \
        f"{name}: bars {s['bars_detected']} vs {s['bars_expected']} (tol {tol})"


@pytest.mark.parametrize("name", list(EXPECT.keys()))
def test_no_false_drops_in_calm(scores, name):
    s = scores[name]
    assert s["false_drop_frames"] == 0, \
        f"{name}: {s['false_drop_frames']} false-drop frames in calm sections"


@pytest.mark.parametrize("name", [n for n, e in EXPECT.items() if e["has_drop"]])
def test_drop_detected(scores, name):
    s = scores[name]
    assert s["drop_detected"] is True, f"{name}: drop section not detected"


@pytest.mark.parametrize("name", [n for n, e in EXPECT.items() if e["has_drop"]])
def test_build_anticipated_before_drop(scores, name):
    s = scores[name]
    assert s["build_seen"] is True, f"{name}: no BUILD anticipation before drop"


def test_dnb_folds_to_felt_tempo(scores):
    # 174 BPM DnB should be tracked at the felt 174, not halved to 87.
    assert abs(scores["DnB 174"]["bpm_detected"] - 174.0) <= 2.0


def test_half_time_folds_up_to_felt(scores):
    # 87 BPM half-time track should fold UP to the felt 174.
    assert abs(scores["Half-time 87 felt-174"]["bpm_detected"] - 174.0) <= 2.0
