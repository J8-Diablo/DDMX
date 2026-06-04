"""Unit tests for the timeline fade-in/out mix (dmx_engine)."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dmx_engine import DMXRenderEngine, TimelineBlock  # noqa: E402


def _eng():
    return DMXRenderEngine(artnet_ip="127.0.0.1")


def _blk(**kw):
    base = dict(plan_index=0, cue_index=0, cue_name="c", lane=0,
                start_ms=0, length_ms=1000, end_ms=1000)
    base.update(kw)
    return TimelineBlock(**base)


def test_edge_fades_in_and_out():
    e = _eng()
    b = _blk(fade_in_ms=200, fade_out_ms=300)
    f = lambda t: round(e._timeline_block_fade_mix(b, t), 3)
    assert f(0) == 0.0
    assert f(100) == 0.5            # mid fade-in
    assert f(200) == 1.0           # fade-in complete
    assert f(500) == 1.0           # sustain
    assert f(850) == 0.5           # mid fade-out (remaining 150 / 300)
    assert f(1000) == 0.0          # past end
    assert f(999) < 0.05           # almost gone


def test_fade_in_only():
    e = _eng()
    b = _blk(fade_in_ms=400, fade_out_ms=0)
    assert e._timeline_block_fade_mix(b, 200) == 0.5
    assert e._timeline_block_fade_mix(b, 400) == 1.0
    assert e._timeline_block_fade_mix(b, 900) == 1.0


def test_legacy_model_still_works_without_edge_fades():
    e = _eng()
    b = _blk(fade_start_ms=100, fade_end_ms=300)  # no fade_in/out
    assert e._timeline_block_fade_mix(b, 50) == 0.0
    assert round(e._timeline_block_fade_mix(b, 200), 3) == 0.5
    assert e._timeline_block_fade_mix(b, 400) == 1.0


def test_normalize_parses_edge_fades():
    e = _eng()
    blocks = e._normalize_timeline_blocks([{
        "start_ms": 0, "length_ms": 1000, "fade_in_ms": 250, "fade_out_ms": 400,
        "lane": 0, "cue_name": "X",
    }])
    assert len(blocks) == 1
    assert blocks[0].fade_in_ms == 250
    assert blocks[0].fade_out_ms == 400


def test_edge_fades_clamped_to_length():
    e = _eng()
    blocks = e._normalize_timeline_blocks([{
        "start_ms": 0, "length_ms": 500, "fade_in_ms": 9999, "fade_out_ms": 9999,
    }])
    assert blocks[0].fade_in_ms == 500
    assert blocks[0].fade_out_ms == 500
