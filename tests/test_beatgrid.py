"""Unit tests for autolight_beatgrid.BeatGrid.

These drive the grid with synthetic onset trains (no audio), which is exactly
the kind of reproducible reference-track validation the rewrite calls for.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from autolight_beatgrid import BeatGrid, fold_bpm  # noqa: E402


def _run_train(grid, bpm, *, beats, dt=0.025, start=0.0, phase=0.0,
               kick_positions=(0, 2), strength=1.0):
    """Simulate a 4/4 train: kicks on the given in-bar positions.

    Steps a virtual clock at `dt`, dropping onsets at the right times and
    calling update() each frame. Returns the final state.
    """
    period = 60.0 / bpm
    onset_times = []
    for b in range(beats):
        pos = b % 4
        if pos in kick_positions:
            onset_times.append(start + phase + b * period)
    onset_times.sort()
    state = None
    t = start
    end = start + phase + beats * period + period
    oi = 0
    while t <= end:
        while oi < len(onset_times) and onset_times[oi] <= t:
            grid.add_onset(onset_times[oi], strength, "kick")
            oi += 1
        state = grid.update(t)
        t += dt
    return state


def test_fold_bpm_into_felt_window():
    assert 90.0 <= fold_bpm(64.0) < 180.0   # 64 → 128
    assert abs(fold_bpm(64.0) - 128.0) < 1e-6
    assert abs(fold_bpm(140.0) - 140.0) < 1e-6
    assert abs(fold_bpm(174.0) - 174.0) < 1e-6   # DnB stays felt-fast
    assert abs(fold_bpm(220.0) - 110.0) < 1e-6
    assert fold_bpm(0) == 0.0


def test_locks_to_steady_128_four_on_floor():
    grid = BeatGrid()
    grid.set_reference_bpm(128.0, "db")
    st = _run_train(grid, 128.0, beats=64, kick_positions=(0, 1, 2, 3))
    assert st["locked"] is True
    assert st["confidence"] > 0.7
    assert abs(st["bpm"] - 128.0) < 1.0
    # 64 beats → 16 bars elapsed (index of last completed bar ~15).
    assert st["bar_index"] >= 14


def test_counts_bars_in_four_four():
    grid = BeatGrid()
    grid.set_reference_bpm(120.0, "db")
    st = _run_train(grid, 120.0, beats=32, kick_positions=(0, 1, 2, 3))
    # 32 beats at 4/4 → 8 bars.
    assert 7 <= st["bar_index"] <= 8
    assert st["beat_in_bar"] in (0, 1, 2, 3)


def test_downbeat_aligns_to_strong_kick():
    # Kick only on positions 0 and 2, with the "1" much stronger than the "3".
    grid = BeatGrid()
    grid.set_reference_bpm(128.0, "db")
    period = 60.0 / 128.0
    beats = 64
    t = 0.0
    dt = 0.02
    onsets = []
    for b in range(beats):
        pos = b % 4
        if pos == 0:
            onsets.append((b * period, 1.0))
        elif pos == 2:
            onsets.append((b * period, 0.4))
    onsets.sort()
    oi = 0
    st = None
    end = beats * period + period
    while t <= end:
        while oi < len(onsets) and onsets[oi][0] <= t:
            grid.add_onset(onsets[oi][0], onsets[oi][1], "kick")
            oi += 1
        st = grid.update(t)
        t += dt
    # The downbeat must track the strongest kick position: whichever raw
    # bar-position accumulated the most kick energy is the one tagged as "the 1"
    # (its grid-relative offset may differ since the grid origin is arbitrary).
    peak_pos = max(range(4), key=lambda i: grid._pos_strength[i])
    assert grid._downbeat_offset == peak_pos
    assert grid._pos_strength[peak_pos] > sorted(grid._pos_strength)[-2] * 1.5
    assert st["phrase_len"] in (8, 16, 32)


def test_phrase_length_detected_16():
    # Build 32 bars where every 16th bar is a loud accent (phrase boundary).
    grid = BeatGrid()
    grid.set_reference_bpm(130.0, "db")
    period = 60.0 / 130.0
    dt = 0.02
    t = 0.0
    beats = 16 * 4 * 2  # 32 bars
    onsets = []
    for b in range(beats):
        bar = b // 4
        pos = b % 4
        base = 0.6
        # Accent the downbeat of every 16th bar.
        if pos == 0 and bar % 16 == 0:
            base = 1.0
        if pos in (0, 1, 2, 3):
            onsets.append((b * period, base))
    oi = 0
    st = None
    end = beats * period + period
    while t <= end:
        while oi < len(onsets) and onsets[oi][0] <= t:
            grid.add_onset(onsets[oi][0], onsets[oi][1], "kick")
            oi += 1
        st = grid.update(t)
        t += dt
    assert st["phrase_len"] == 16


def test_handles_phase_offset_start():
    # Start the train at an arbitrary phase; the grid must still lock.
    grid = BeatGrid()
    grid.set_reference_bpm(140.0, "db")
    st = _run_train(grid, 140.0, beats=48, phase=0.137, kick_positions=(0, 1, 2, 3))
    assert st["locked"] is True
    assert st["confidence"] > 0.6


def test_audio_only_tempo_estimate_no_reference():
    # No reference BPM: the grid must estimate tempo from onset intervals.
    grid = BeatGrid()
    st = _run_train(grid, 124.0, beats=64, kick_positions=(0, 1, 2, 3))
    assert st["bpm"] > 0
    # Estimated tempo should be within ~6 BPM of truth (felt-folded).
    assert abs(st["bpm"] - 124.0) < 6.0


def test_reset_clears_counters():
    grid = BeatGrid()
    grid.set_reference_bpm(128.0, "db")
    _run_train(grid, 128.0, beats=32, kick_positions=(0, 1, 2, 3))
    grid.reset(hard=False)
    st = grid.update(100.0)
    assert st["beat_index"] == -1
    assert st["bar_index"] == 0


def test_tap_outranks_db():
    grid = BeatGrid()
    grid.set_reference_bpm(128.0, "db")
    grid.set_reference_bpm(140.0, "tap")
    assert grid.state()["bpm_source"] == "tap"
    assert abs(grid.state()["bpm"] - 140.0) < 1e-6
    # A later db update must NOT override the tap.
    grid.set_reference_bpm(150.0, "db")
    assert abs(grid.state()["bpm"] - 140.0) < 1e-6


def test_observe_from_snapshot_stream():
    # Drive via observe() with snapshot-like dicts (counts increment on a beat).
    grid = BeatGrid()
    bpm = 128.0
    period = 60.0 / bpm
    dt = 0.025
    t = 0.0
    kc = 0
    next_beat = 0.0
    st = None
    while t <= 16 * period:
        snap = {"bpm": bpm, "bpm_source": "auto", "db_bpm": bpm, "rms": 0.2,
                "kick_count": kc, "snare_count": 0, "beat_intensity": 1.0}
        if t >= next_beat:
            kc += 1
            snap["kick_count"] = kc
            next_beat += period
        st = grid.observe(t, snap)
        t += dt
    assert st["bpm_source"] == "db"
    assert st["beat_index"] >= 12
