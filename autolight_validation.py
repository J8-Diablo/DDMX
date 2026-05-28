"""autolight_validation.py — reproducible reference-track validation harness.

Generates synthetic per-frame audio snapshots that mimic real tracks (scripted
sections with known BPM, bar count and drop positions), runs them through the
real BeatGrid + MusicBrain, and measures how well the engine recovers the
ground truth: BPM accuracy, lock time, bar counting, drop detection and false
positives.

This is the "reference-track" half of the validation the rewrite calls for
(the "live" half is done by ear). It is pure Python — no audio hardware — so it
runs in CI and stays reproducible. Usable both as a pytest fixture (see
tests/test_reference_tracks.py) and as a CLI:  ``python autolight_validation.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from autolight_beatgrid import BeatGrid, fold_bpm
from autolight_brain import MusicBrain, DROP, BUILD, CALM, GROOVE, SILENCE


@dataclass
class Section:
    """A musical section of ``bars`` 4/4 bars at a given intensity level."""
    bars: int
    level: int                       # 0 silent · 1 verse · 2 chorus · 3 high · 4 drop
    kicks: Tuple[int, ...] = (0, 1, 2, 3)   # in-bar beat positions carrying a kick
    label: str = ""
    ramp: bool = False               # energy rises across the section (a build-up)


@dataclass
class TrackScript:
    name: str
    bpm: float
    sections: List[Section]
    db_bpm: Optional[float] = None   # official BPM fed to the grid, or None
    dt: float = 0.025                # frame period (40 Hz render tick)

    @property
    def total_bars(self) -> int:
        return sum(s.bars for s in self.sections)


def _level_rms(level: int) -> Tuple[float, float]:
    """(long_rms, short_rms) baseline for a structure level."""
    table = {0: 0.010, 1: 0.030, 2: 0.060, 3: 0.090, 4: 0.140}
    base = table.get(level, 0.03)
    return base, base


def simulate(track: TrackScript) -> List[Dict[str, Any]]:
    """Run the pipeline over a scripted track; return per-frame records."""
    grid = BeatGrid()
    brain = MusicBrain()
    if track.db_bpm:
        grid.set_reference_bpm(track.db_bpm, "db")

    period = 60.0 / track.bpm
    # Expand sections into a per-beat plan.
    beat_plan: List[Dict[str, Any]] = []
    for s in track.sections:
        for bar in range(s.bars):
            for pos in range(4):
                beat_plan.append({"pos": pos, "level": s.level, "kicks": s.kicks,
                                  "ramp": s.ramp, "label": s.label, "bar_in_sec": bar,
                                  "sec_bars": s.bars})
    total_beats = len(beat_plan)
    end_t = total_beats * period + period

    records: List[Dict[str, Any]] = []
    kick_count = 0
    snare_count = 0
    t = 0.0
    bi = 0  # next beat index to fire
    while t <= end_t:
        # Fire any beats whose time has arrived.
        while bi < total_beats and (bi * period) <= t:
            plan = beat_plan[bi]
            pos = plan["pos"]
            if pos in plan["kicks"]:
                kick_count += 1
            if pos in (1, 3):  # snare on the backbeat
                snare_count += 1
            bi += 1

        # Current section context (by elapsed beats).
        cur = beat_plan[min(bi, total_beats - 1)] if total_beats else {"level": 0, "ramp": False, "bar_in_sec": 0, "sec_bars": 1, "label": ""}
        level = cur["level"]
        long_rms, short_rms = _level_rms(level)
        # Build-up ramp: energy climbs across the section; short>long near the end.
        if cur["ramp"] and cur["sec_bars"]:
            frac = cur["bar_in_sec"] / max(1, cur["sec_bars"])
            short_rms = long_rms * (1.0 + 0.4 * frac)
        # Drop entry: a short-term spike vs the long average.
        drop_score = 0.0
        if level >= 4:
            drop_score = 2.2
            short_rms = long_rms * 1.9
        # downbeat carries the strongest kick (helps downbeat estimation).
        beat_intensity = 1.0 if (bi > 0 and beat_plan[bi - 1]["pos"] == 0) else 0.6

        snap = {
            "available": True, "active": level > 0, "rms": long_rms,
            "kick_count": kick_count, "snare_count": snare_count,
            "beat_intensity": beat_intensity, "snare_intensity": 0.7,
            "bpm": track.bpm, "bpm_source": "auto",
            "structure": {"level": level, "drop_score": drop_score,
                          "long_rms": long_rms, "short_rms": short_rms},
        }
        if track.db_bpm:
            snap["db_bpm"] = track.db_bpm

        grid_state = grid.observe(t, snap)
        directive = brain.decide(t, grid_state, snap, None)
        records.append({"t": t, "grid": grid_state, "intent": directive.intent,
                        "true_level": level, "label": cur["label"]})
        t += track.dt
    return records


def score(track: TrackScript, records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute a scorecard from a simulated run."""
    felt_truth = fold_bpm(track.db_bpm or track.bpm)
    final = records[-1]["grid"] if records else {}
    detected_bpm = float(final.get("bpm", 0.0))
    bpm_error = abs(detected_bpm - felt_truth) if detected_bpm > 0 else 999.0

    # Time to first lock.
    locked_at = None
    for r in records:
        if r["grid"].get("locked"):
            locked_at = r["t"]
            break

    bars_detected = final.get("bar_index", 0)
    # The grid counts at the FELT tempo, so a half-time track (e.g. 87→174)
    # legitimately reports ~2× the script's bars. Scale expectation to felt.
    felt_ratio = (felt_truth / track.bpm) if track.bpm else 1.0
    bars_expected_felt = round(track.total_bars * felt_ratio)

    # Drop detection: did the brain reach DROP during each level-4 section, and
    # how many DROP frames fired during non-drop (calm/verse) sections?
    drop_frames = sum(1 for r in records if r["intent"] == DROP)
    drop_frames_in_truth = sum(1 for r in records if r["intent"] == DROP and r["true_level"] >= 4)
    false_drop_frames = sum(1 for r in records if r["intent"] == DROP and r["true_level"] <= 1)
    had_drop_section = any(r["true_level"] >= 4 for r in records)
    drop_detected = drop_frames_in_truth > 0 if had_drop_section else None

    # Did a BUILD ever appear (anticipation) before a drop?
    build_seen = any(r["intent"] == BUILD for r in records)

    return {
        "name": track.name,
        "bpm_truth_felt": round(felt_truth, 2),
        "bpm_detected": round(detected_bpm, 2),
        "bpm_error": round(bpm_error, 2),
        "locked_at_s": None if locked_at is None else round(locked_at, 2),
        "bars_detected": bars_detected,
        "bars_expected": bars_expected_felt,
        "drop_detected": drop_detected,
        "drop_frames": drop_frames,
        "false_drop_frames": false_drop_frames,
        "build_seen": build_seen,
    }


# --- Reference track library ------------------------------------------------

def reference_tracks() -> List[TrackScript]:
    return [
        TrackScript("EDM 128 (drop @16)", 128.0, [
            Section(8, 1, label="intro"),
            Section(8, 2, label="verse"),
            Section(8, 3, kicks=(0, 2), ramp=True, label="build"),
            Section(16, 4, label="drop"),
            Section(8, 1, label="breakdown"),
            Section(8, 3, ramp=True, label="build2"),
            Section(16, 4, label="drop2"),
        ], db_bpm=128.0),
        TrackScript("House 124 (no db)", 124.0, [
            Section(8, 2, label="intro"),
            Section(16, 3, label="groove"),
            Section(8, 3, ramp=True, label="build"),
            Section(16, 4, label="peak"),
        ]),
        TrackScript("DnB 174", 174.0, [
            Section(8, 2, label="intro"),
            Section(8, 3, ramp=True, label="build"),
            Section(16, 4, label="drop"),
        ], db_bpm=174.0),
        TrackScript("Half-time 87 felt-174", 87.0, [
            Section(8, 2, kicks=(0, 2), label="intro"),
            Section(16, 3, kicks=(0, 2), label="groove"),
        ]),
    ]


def run_all() -> List[Dict[str, Any]]:
    return [score(t, simulate(t)) for t in reference_tracks()]


if __name__ == "__main__":
    print(f"{'track':24} {'truth':>6} {'detec':>6} {'err':>5} "
          f"{'lock':>5} {'bars':>9} {'drop':>5} {'false':>6} {'build':>6}")
    for s in run_all():
        print(f"{s['name']:24} {s['bpm_truth_felt']:6.1f} {s['bpm_detected']:6.1f} "
              f"{s['bpm_error']:5.1f} {str(s['locked_at_s']):>5} "
              f"{str(s['bars_detected'])+'/'+str(s['bars_expected']):>9} "
              f"{str(s['drop_detected']):>5} {s['false_drop_frames']:6d} "
              f"{str(s['build_seen']):>6}")
