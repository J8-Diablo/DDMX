"""What the engine still renders after the UI has stopped an effect.

A playback empties the live layer at its start and puts a snapshot back at its
end, so the cue owns the rig meanwhile. That snapshot used to be frozen: a Stop
FX or an effect deletion during the cue was applied to the emptied layer and
then overwritten by the restore, so the effect came back to life and kept being
emitted with nothing in the UI to explain it.

The cue pool had the mirror-image problem: it only ever grew, and a stopped
playback left it behind.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _group(gid, device_ids, **extra):
    """An effect group shaped like the ones the UI posts."""
    g = {
        "id": gid,
        "mode": "intelligent",
        "type": "breathing",
        "targets": ["dimmer"],
        "deviceIds": [str(d) for d in device_ids],
        "speed": 1.0,
        "min": 0,
        "max": 255,
    }
    g.update(extra)
    return g


@pytest.fixture()
def engine():
    from dmx_engine import DMXRenderEngine

    eng = DMXRenderEngine(artnet_ip="127.0.0.1")
    eng.register_rig_devices([
        {
            "device_id": str(i + 1),
            "universe": 0,
            "address": i * 10,
            "fixture": "test.fixture.json",
            "attr_map": {"dimmer.main.level": i * 10, "dimmer": i * 10},
        }
        for i in range(4)
    ], replace=True)
    yield eng
    try:
        eng.stop()
    except Exception:
        pass


def _start_playback(engine):
    """What a cue start does to the live layer."""
    with engine._lock:
        engine._prepare_playback_render_locked()


def _end_playback(engine):
    with engine._lock:
        engine._restore_playback_render_locked()


# -----------------------------------------------------------------------------
# The live layer across a playback
# -----------------------------------------------------------------------------

def test_stop_fx_during_a_cue_is_not_undone_when_the_cue_ends(engine):
    engine.set_live_effect_groups([_group("g1", ["1", "2"])], action="set")
    assert list(engine._live_effect_groups) == ["g1"]

    _start_playback(engine)
    assert engine._live_effect_groups == {}, "the cue owns the rig while it plays"

    engine.set_live_effect_groups([], action="set")      # Stop FX
    _end_playback(engine)

    assert engine._live_effect_groups == {}, "the effect came back from the snapshot"
    assert engine._live_groups_by_device == {}


def test_deleting_an_effect_during_a_cue_stays_deleted(engine):
    """/api/live/effects/groups/purge exists precisely for this."""
    engine.set_live_effect_groups([_group("g1", ["1"]), _group("g2", ["2"])], action="set")
    _start_playback(engine)

    assert engine.remove_effect_group_everywhere(["g1"]) >= 0
    _end_playback(engine)

    assert "g1" not in engine._live_effect_groups
    assert "g2" in engine._live_effect_groups, "only the deleted group must go"


def test_an_effect_added_during_a_cue_survives_the_cue(engine):
    """The engine follows the UI, in both directions."""
    _start_playback(engine)
    engine.set_live_effect_groups([_group("g9", ["3"])], action="set")
    _end_playback(engine)

    assert list(engine._live_effect_groups) == ["g9"]
    assert engine._live_groups_by_device.get("3") == {"g9"}


def test_a_hold_from_before_the_cue_still_comes_back(engine):
    """The snapshot is not simply dropped: untouched live state returns."""
    engine.set_live_effect_groups([_group("g1", ["1"])], action="set")
    engine.set_manual_attrs([{"device_id": "1", "attr": "dimmer", "value": 200}])

    _start_playback(engine)
    assert engine.get_manual_attrs() == {}
    _end_playback(engine)

    assert list(engine._live_effect_groups) == ["g1"]
    assert engine.get_manual_attrs() == {"1": {"dimmer": 200}}


def test_stop_fx_reaches_the_output_through_a_whole_playback(engine):
    """End to end: run a real sequence, Stop FX mid-flight, look at the frame."""
    engine.set_live_effect_groups([_group("g1", ["1", "2"])], action="set")
    engine.start()

    sequence = [
        {"name": "s1", "devices": {}, "device_groups": {}, "duration": "0", "sleep": "150"},
        {"name": "s2", "devices": {}, "device_groups": {}, "duration": "0", "sleep": "150"},
    ]
    engine.run_sequence(sequence, 0)
    time.sleep(0.1)
    engine.set_live_effect_groups([], action="set")      # Stop FX, cue still running
    deadline = time.time() + 3.0
    while engine._playback_state.get("active") and time.time() < deadline:
        time.sleep(0.05)

    assert not engine._playback_state.get("active"), "the sequence never finished"
    assert engine._live_effect_groups == {}
    # And two frames apart, the dimmer channel no longer moves.
    engine._render_frame()
    first = engine._universes[0][0]
    time.sleep(0.15)
    engine._render_frame()
    assert engine._universes[0][0] == first, "something is still animating the channel"


# -----------------------------------------------------------------------------
# The cue pool
# -----------------------------------------------------------------------------

def test_the_cue_pool_drops_a_group_nothing_belongs_to(engine):
    engine.go_cue({
        "devices": {"1": {"channels": {"Universe": 0, "0": 10}}},
        "duration": "0",
        "effect_groups": [_group("g1", ["1"])],
    }, device_order=["1"])
    assert list(engine._cue_effect_groups) == ["g1"]

    # The same cue without the effect: the group is gone, not merely unused.
    engine.go_cue({
        "devices": {"1": {"channels": {"Universe": 0, "0": 10}}},
        "duration": "0",
        "effect_groups": [],
    }, device_order=["1"])
    assert engine._cue_effect_groups == {}
    assert engine._cue_groups_by_device.get("1", set()) == set()


def test_a_crossfade_keeps_the_outgoing_group_renderable(engine):
    """The group being faded out must stay in the pool for the crossfade."""
    engine.go_cue({
        "devices": {"1": {"channels": {"Universe": 0, "0": 10}}},
        "duration": "0",
        "effect_groups": [_group("g1", ["1"])],
    }, device_order=["1"])

    engine.go_cue({
        "devices": {"1": {"channels": {"Universe": 0, "0": 200}}},
        "duration": "500",
        "effect_groups": [_group("g2", ["1"])],
    }, device_order=["1"])

    assert engine._fade is not None
    assert "g2" in engine._cue_effect_groups
    assert "g1" in engine._cue_effect_groups, "the crossfade still renders it"
    union = engine._fade_effect_groups["union"].get("1", set())
    assert {"g1", "g2"} <= union


def test_stopping_the_playback_clears_the_cue_pool(engine):
    engine.go_cue({
        "devices": {"1": {"channels": {"Universe": 0, "0": 10}}},
        "duration": "0",
        "effect_groups": [_group("g1", ["1"])],
    }, device_order=["1"])
    assert engine._cue_effect_groups

    engine.stop_playback()

    assert engine._cue_effect_groups == {}
    assert engine._cue_groups_by_device == {}


def test_adding_during_a_cue_keeps_what_existed_before_it(engine):
    """The trap: while a cue plays the live pool holds only what arrived since
    the cue started, so mirroring it wholesale would drop the rest."""
    engine.set_live_effect_groups([_group("before", ["1"])], action="set")
    _start_playback(engine)

    engine.set_live_effect_groups([_group("during", ["2"])], action="add")
    _end_playback(engine)

    assert set(engine._live_effect_groups) == {"before", "during"}


def test_removing_one_group_during_a_cue_spares_the_others(engine):
    engine.set_live_effect_groups([_group("a", ["1"]), _group("b", ["2"])], action="set")
    _start_playback(engine)

    engine.set_live_effect_groups([], action="remove", group_ids=["a"])
    _end_playback(engine)

    assert set(engine._live_effect_groups) == {"b"}
    assert engine._live_groups_by_device.get("2") == {"b"}
