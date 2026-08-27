"""Time model v2: a cue lasts `fade` then `duration`.

  v1: "wait `sleep`, then crossfade over `duration`" -- so how long a look
      stayed on stage lived in the NEXT step's sleep, which no timeline block
      can describe. The timeline bridge papered over it by writing
      "fade_in <op> fade_out" into `duration`, which the engine reads as
      (fade, per-device spread): a fade-out became a spread.
  v2: "crossfade over `fade`, then hold `duration`" -- one block of
      fade + duration whose fade-in is the fade. Both views describe the same
      thing, so neither has to guess.

Old files are converted on the way in, exactly, loop groups included.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_NODE = shutil.which("node")
requires_node = pytest.mark.skipif(_NODE is None, reason="node not available on PATH")


def _read(rel: str) -> str:
    with open(os.path.join(_REPO_ROOT, rel), "r", encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture()
def engine():
    from dmx_engine import DMXRenderEngine

    eng = DMXRenderEngine(artnet_ip="127.0.0.1")
    yield eng
    try:
        eng.stop()
    except Exception:
        pass


def _v1(name, sleep, duration, **extra):
    step = {"name": name, "sleep": sleep, "duration": duration, "devices": {"1": {"channels": {"Universe": 0, "0": 10}}}}
    step.update(extra)
    return step


def _v2(name, fade, duration, **extra):
    step = {"name": name, "fade": fade, "duration": duration, "devices": {"1": {"channels": {"Universe": 0, "0": 10}}}}
    step.update(extra)
    return step


# -----------------------------------------------------------------------------
# Reading a step
# -----------------------------------------------------------------------------

def test_a_v2_step_states_its_fade_and_its_hold():
    from dmx_engine import DMXRenderEngine as E

    step = _v2("a", "500 > 2000", 3000)
    assert E.step_fade_field(step) == "500 > 2000"
    assert E.step_hold_ms(step) == 3000


def test_a_v1_step_is_recognised_and_holds_nothing_of_its_own():
    from dmx_engine import DMXRenderEngine as E

    step = _v1("a", 1000, "500")
    assert E.step_fade_field(step) == "500", "the old `duration` was the fade"
    assert E.step_hold_ms(step) == 0, "its hold lived in the next step's sleep"
    assert E.is_time_model_v2([step]) is False
    assert E.is_time_model_v2([_v2("a", "500", 0)]) is True
    assert E.is_time_model_v2([]) is True


def test_a_bad_duration_does_not_raise():
    from dmx_engine import DMXRenderEngine as E

    assert E.step_hold_ms({"fade": "0", "duration": "nope"}) == 0
    assert E.step_hold_ms({"fade": "0", "duration": -5}) == 0
    assert E.step_hold_ms(None) == 0
    assert E.step_fade_field(None) == "0"


# -----------------------------------------------------------------------------
# Converting a v1 sequence
# -----------------------------------------------------------------------------

def test_the_hold_of_a_step_is_the_wait_that_preceded_the_next():
    from dmx_engine import DMXRenderEngine as E

    sequence = [_v1("a", 0, "500"), _v1("b", 2000, "300"), _v1("c", 1500, "200")]
    out, lead_in = E.migrate_sequence_time_model(sequence)

    assert lead_in == 0
    assert [s["fade"] for s in out] == ["500", "300", "200"]
    assert [s["duration"] for s in out] == [2000, 1500, 0]
    assert all("sleep" not in s for s in out)
    # The source list is untouched.
    assert sequence[1]["sleep"] == 2000


def test_a_leading_wait_becomes_the_sequence_lead_in():
    from dmx_engine import DMXRenderEngine as E

    out, lead_in = E.migrate_sequence_time_model([_v1("a", 500, "100"), _v1("b", 800, "100")])
    assert lead_in == 500
    assert [s["duration"] for s in out] == [800, 0]


def test_an_already_converted_sequence_is_left_alone():
    from dmx_engine import DMXRenderEngine as E

    sequence = [_v2("a", "500", 1000)]
    out, lead_in = E.migrate_sequence_time_model(sequence)
    assert out is sequence
    assert lead_in == 0


def test_a_loop_group_holds_back_to_its_own_first_step():
    """Inside a group, what plays after its last step is its FIRST step."""
    from dmx_engine import DMXRenderEngine as E

    sequence = [
        _v1("intro", 0, "100"),
        _v1("a", 1050, "600", loopGroup="g", loopCount=3),
        _v1("b", 1050, "600", loopGroup="g", loopCount=3),
        _v1("after", 1500, "0"),
    ]
    out, _ = E.migrate_sequence_time_model(sequence)

    assert out[1]["duration"] == 1050            # a -> b
    assert out[2]["duration"] == 1050            # b -> back to a (NOT 1500)
    assert out[2]["exit_duration"] == 1500       # ...except on the way out
    assert "exit_duration" not in out[1]


def test_no_exit_duration_when_it_matches_the_loop_hold():
    from dmx_engine import DMXRenderEngine as E

    sequence = [
        _v1("a", 1000, "100", loopGroup="g", loopCount=2),
        _v1("b", 1000, "100", loopGroup="g", loopCount=2),
        _v1("after", 1000, "100"),
    ]
    out, _ = E.migrate_sequence_time_model(sequence)
    assert out[1]["duration"] == 1000
    assert "exit_duration" not in out[1], "nothing worth writing when both agree"


# -----------------------------------------------------------------------------
# The playback plan
# -----------------------------------------------------------------------------

def _plan(engine, sequence, lead_in_ms=0, speed=1.0):
    return engine._expand_playback_sequence(
        sequence, virtual_groups={}, speed=speed, lead_in_ms=lead_in_ms,
    )


def test_a_cue_fades_then_holds(engine):
    plan = _plan(engine, [_v2("a", "500", 2000), _v2("b", "300", 0)])

    assert [(e.fade_start_at_ms, e.fade_end_at_ms) for e in plan if not e.hold_only] == [
        (0, 500),        # a fades
        (2500, 2800),    # ...holds 2000, then b fades
    ]
    assert plan[1].sleep_ms == 2000, "the wait before b is a's hold"
    assert plan[1].hold_cue_index == 0, "and the operator must see a holding"


def test_the_lead_in_delays_the_first_cue(engine):
    plan = _plan(engine, [_v2("a", "100", 0)], lead_in_ms=750)
    assert plan[0].sleep_ms == 750
    assert plan[0].hold_cue_index == -1, "a lead-in belongs to no cue"
    assert (plan[0].fade_start_at_ms, plan[0].fade_end_at_ms) == (750, 850)


def test_the_last_cue_holds_too(engine):
    plan = _plan(engine, [_v2("a", "100", 0), _v2("b", "200", 1500)])
    tail = [e for e in plan if e.hold_only]

    assert len(tail) == 1, "the last hold needs an entry of its own"
    assert tail[0].sleep_ms == 1500
    assert tail[0].cue_index == 1
    assert tail[0].fade_ms == 0
    # Without it a loop pass would cut the last look short.
    assert plan[-1].fade_end_at_ms == 100 + 200 + 1500


def test_no_trailing_entry_when_the_last_cue_holds_nothing(engine):
    plan = _plan(engine, [_v2("a", "100", 0)])
    assert not [e for e in plan if e.hold_only]


def test_the_fade_keeps_its_spread_operator(engine):
    step = _v2("a", "100 > 500", 0)
    # A spread needs somebody to spread across.
    step["devices"] = {
        "1": {"channels": {"Universe": 0, "0": 10}},
        "2": {"channels": {"Universe": 0, "10": 10}},
    }
    plan = _plan(engine, [step])
    # base + spread: the last device finishes at 600.
    assert plan[0].fade_ms == 600

    alone = _plan(engine, [_v2("a", "100 > 500", 0)])
    assert alone[0].fade_ms == 100, "one device, nothing to stagger"


def test_the_exit_hold_applies_only_to_the_last_pass(engine):
    sequence = [
        _v2("a", "0", 1000, loopGroup="g", loopCount=3),
        _v2("b", "0", 1000, exit_duration=250, loopGroup="g", loopCount=3),
    ]
    plan = [e for e in _plan(engine, sequence) if not e.hold_only]
    holds = [e.hold_ms for e in plan]

    assert len(plan) == 6, "3 passes of 2 cues"
    assert holds == [1000, 1000, 1000, 1000, 1000, 250]


def test_speed_scales_fade_and_hold_alike(engine):
    plan = _plan(engine, [_v2("a", "400", 2000), _v2("b", "200", 0)], speed=2.0)
    assert plan[0].fade_ms == 200
    assert plan[0].hold_ms == 1000
    assert plan[1].sleep_ms == 1000


def test_converted_sequences_play_at_the_same_times(engine):
    """The conversion may not move a single fade boundary."""
    v1 = [
        _v1("intro", 0, "100"),
        _v1("a", 1050, "600", loopGroup="g", loopCount=3),
        _v1("b", 1050, "600", loopGroup="g", loopCount=3),
        _v1("after", 1500, "250"),
    ]
    # What v1 played: wait sleep, then fade, for every expanded occurrence.
    expected, cursor = [], 0
    for cue in [v1[0], v1[1], v1[2], v1[1], v1[2], v1[1], v1[2], v1[3]]:
        cursor += int(cue["sleep"])
        start = cursor
        cursor += int(cue["duration"])
        expected.append((start, cursor))

    from dmx_engine import DMXRenderEngine as E
    migrated, lead_in = E.migrate_sequence_time_model(v1)
    got = [(e.fade_start_at_ms, e.fade_end_at_ms)
           for e in _plan(engine, migrated, lead_in_ms=lead_in) if not e.hold_only]

    assert got == expected


# -----------------------------------------------------------------------------
# Wire format
# -----------------------------------------------------------------------------

def test_the_cue_payload_names_the_fade(engine):
    payload, order = engine._build_cue_payload_from_step(_v2("a", "250 > 100", 500), {})
    assert payload["fade"] == "250 > 100"
    assert "duration" not in payload
    assert order == ["1"]


def test_applying_a_cue_accepts_both_names(engine):
    engine.register_rig_devices([{
        "device_id": "1", "universe": 0, "address": 0, "fixture": "t.json",
        "attr_map": {"dimmer": 0},
    }], replace=True)

    engine.go_cue({"devices": {"1": {"channels": {"Universe": 0, "0": 200}}}, "fade": "400"}, ["1"])
    assert engine._fade is not None and engine._fade.duration_ms == 400

    # An older client still says "duration".
    engine.go_cue({"devices": {"1": {"channels": {"Universe": 0, "0": 0}}}, "duration": "700"}, ["1"])
    assert engine._fade is not None and engine._fade.duration_ms == 700


def test_the_run_endpoint_takes_a_lead_in():
    import app

    client = app.app.test_client()
    res = client.post("/api/playback/run", json={
        "sequence": [_v2("a", "10", 0)],
        "lead_in_ms": 250,
        "mode": "classic",
    })
    assert res.status_code in (200, 503)
    if res.status_code == 200:
        assert res.get_json().get("ok") is True


# -----------------------------------------------------------------------------
# The front speaks the same model
# -----------------------------------------------------------------------------

def _front_source() -> str:
    src = _read("static/cues.js")
    start = src.index("const TIME_MODEL = 2;")
    end = src.index("window.analyzeCueListTiming")
    block = src[start:end]
    assert "function migrateSequenceTimeModel" in block
    assert "function analyzeCueListTiming" in block
    return block


_HARNESS = """
var window = {};
function t(key, fallback) { return fallback; }
%(source)s
const out = [];
function check(name, cond, detail) {
  out.push({ name, ok: !!cond, detail: detail === undefined ? null : detail });
}
%(body)s
console.log(JSON.stringify(out));
"""


def _run_node(body: str):
    script = _HARNESS % {"source": _front_source(), "body": body}
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "check.js")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(script)
        proc = subprocess.run([_NODE, path], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    results = json.loads(proc.stdout.strip().splitlines()[-1])
    failed = [r for r in results if not r["ok"]]
    assert not failed, "\n".join(f"{r['name']}: {r['detail']}" for r in failed)
    return results


@requires_node
def test_the_front_converts_exactly_like_python():
    """Two implementations of one rule drift apart; this pins them together."""
    from dmx_engine import DMXRenderEngine as E

    fixtures = [
        [_v1("a", 0, "500"), _v1("b", 2000, "300"), _v1("c", 1500, "200")],
        [_v1("a", 500, "100"), _v1("b", 800, "100")],
        [
            _v1("intro", 0, "100"),
            _v1("a", 1050, "600", loopGroup="g", loopCount=3),
            _v1("b", 1050, "600", loopGroup="g", loopCount=3),
            _v1("after", 1500, "0"),
        ],
        [_v2("a", "500", 1000)],
    ]
    expected = []
    for fixture in fixtures:
        out, lead_in = E.migrate_sequence_time_model(json.loads(json.dumps(fixture)))
        expected.append({
            "lead_in_ms": lead_in,
            "steps": [
                {"fade": str(s["fade"]), "duration": s.get("duration", 0),
                 "exit_duration": s.get("exit_duration")}
                for s in out
            ],
        })

    body = """
const FIXTURES = %s;
const EXPECTED = %s;
FIXTURES.forEach((fixture, i) => {
  const got = migrateSequenceTimeModel(fixture);
  const shaped = {
    lead_in_ms: got.lead_in_ms,
    steps: got.sequence.map((s) => ({
      fade: String(s.fade),
      duration: s.duration ?? 0,
      exit_duration: s.exit_duration === undefined ? null : s.exit_duration,
    })),
  };
  check(`fixture ${i} matches Python`,
        JSON.stringify(shaped) === JSON.stringify(EXPECTED[i]),
        JSON.stringify(shaped) + " != " + JSON.stringify(EXPECTED[i]));
});
""" % (json.dumps(fixtures), json.dumps(expected))
    _run_node(body)


@requires_node
def test_the_front_spots_what_a_cue_list_cannot_play():
    body = """
const block = (name, fade, duration, start, lane) => ({
  name, fade, duration, devices: {}, timeline: { start_ms: start, lane: lane || 0 },
});

// Back to back: a cue list plays this exactly.
let issues = analyzeCueListTiming({ sequence: [
  block("a", "100", 900, 0), block("b", "100", 900, 1000),
] });
check("contiguous list is compatible", issues.compatible, JSON.stringify(issues));

// A hole: the timeline goes dark, a cue list keeps the previous look.
issues = analyzeCueListTiming({ sequence: [
  block("a", "100", 900, 0), block("b", "100", 900, 3000),
] });
check("a hole is reported", issues.gaps.length === 1 && !issues.compatible, JSON.stringify(issues.gaps));
check("the hole is measured", issues.gaps[0].from_ms === 1000 && issues.gaps[0].to_ms === 3000,
      JSON.stringify(issues.gaps));

// Two cues at once: a cue list would serialise them.
issues = analyzeCueListTiming({ sequence: [
  block("a", "100", 1900, 0, 0), block("b", "100", 900, 500, 1),
] });
check("an overlap is reported", issues.overlaps.length === 1 && !issues.compatible,
      JSON.stringify(issues.overlaps));

// Dead time before the first cue is NOT a hole: lead_in_ms says it.
issues = analyzeCueListTiming({ sequence: [block("a", "100", 900, 2000)] });
check("a lead-in is not a hole", issues.compatible, JSON.stringify(issues));

// Blocks placed against the list order.
issues = analyzeCueListTiming({ sequence: [
  block("a", "100", 900, 2000), block("b", "100", 900, 0),
] });
check("an inverted order is reported", issues.order_mismatch, JSON.stringify(issues));

// A converted v1 list has no positions of its own: contiguous, so compatible.
issues = analyzeCueListTiming({ sequence: [
  { name: "a", sleep: 0, duration: "500", devices: {} },
  { name: "b", sleep: 2000, duration: "300", devices: {} },
] });
check("a converted v1 list is compatible", issues.compatible, JSON.stringify(issues));

// Loop groups produce several occurrences, and they must not read as overlaps.
issues = analyzeCueListTiming({ sequence: [
  { name: "a", fade: "0", duration: 500, devices: {}, loopGroup: "g", loopCount: 3,
    timeline: { start_ms: 0, lane: 0 } },
  { name: "b", fade: "0", duration: 500, devices: {}, loopGroup: "g", loopCount: 3,
    timeline: { start_ms: 500, lane: 0 } },
] });
check("a looped group stays compatible", issues.compatible && issues.occurrences === 6,
      JSON.stringify(issues));
"""
    _run_node(body)


# -----------------------------------------------------------------------------
# What must not come back
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("needle", [
    # Reading `sleep` is how the conversion works; writing it is the old model.
    "step.sleep =",
    "cue-prop-sleep",
    '${meta.fade_in_ms} ${op} ${meta.fade_out_ms}',
])
def test_the_old_model_is_gone_from_the_front(needle):
    for name in sorted(os.listdir(os.path.join(_REPO_ROOT, "static"))):
        if not name.endswith(".js"):
            continue
        assert needle not in _read(f"static/{name}"), f"{needle} still in static/{name}"


def test_rapid_fire_fires_an_incompatible_list_on_the_timeline():
    src = _read("static/rapidfire.js")
    assert "timelineOnly" in src
    assert "timeline: asTimeline" in src, "the pad must pick the mode, not force classic"
    assert "window.analyzeCueListTiming" in src


def test_the_dialog_offers_the_three_ways_out():
    src = _read("static/ui.js")
    start = src.index("async function cueTimeModelModal")
    body = src[start:src.index("async function operationStatusModal")]
    assert "cues.timeIssuesOpenClassic" in body
    assert "cues.timeIssuesOpenTimeline" in body
    assert "showCancel: true" in body
    assert 'state.choice = "timeline"' in body


def _timeline_source() -> str:
    """parseDurationMeta + the block <-> step mapping, lifted from timeline.js."""
    src = _read("static/timeline.js")
    head = src[src.index("  function parseDurationMeta("):src.index("  function ensureStepTimeline(")]
    body = src[src.index("  function stepFade(step)"):src.index("  function syncStepTimelineBounds(")]
    assert "function applyBlockLengthToStep" in body
    assert "function applyBlockFadeToStep" in body
    assert "function applyBlockOperatorToStep" in body
    return head + body


_TIMELINE_HARNESS = """
const MIN_BLOCK_MS = 100;
const OPERATOR_OPTIONS = ["", "|", "<", ">", "<>", "><", "||", "?"];
function clamp(value, min, max) { return Math.max(min, Math.min(max, value)); }
var window = {
  stepFadeField: (step) => (step && step.fade != null ? step.fade : (step && step.duration != null ? step.duration : "0")),
  stepHoldMs: (step) => {
    if (!step || step.fade == null) return 0;
    const n = parseInt(step.duration, 10);
    return Number.isFinite(n) ? Math.max(0, n) : 0;
  },
};
%(source)s
const out = [];
function check(name, cond, detail) {
  out.push({ name, ok: !!cond, detail: detail === undefined ? null : detail });
}
%(body)s
console.log(JSON.stringify(out));
"""


def _run_timeline_node(body: str):
    script = _TIMELINE_HARNESS % {"source": _timeline_source(), "body": body}
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "check.js")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(script)
        proc = subprocess.run([_NODE, path], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    results = json.loads(proc.stdout.strip().splitlines()[-1])
    failed = [r for r in results if not r["ok"]]
    assert not failed, "\n".join(f"{r['name']}: {r['detail']}" for r in failed)


@requires_node
def test_a_block_is_the_cue_and_editing_it_writes_the_cue():
    """The block is derived from fade + duration, and edits go back into them.

    This is what stops the two views from drifting: there is one place where
    time is stored, and the timeline reads it rather than keeping its own copy.
    """
    body = """
const step = { name: "a", fade: "200", duration: 800 };
let meta = ensureStepTimeline(step);
check("length is fade + duration", meta.length_ms === 1000, JSON.stringify(meta));
check("fade-in is the fade", meta.fade_in_ms === 200, JSON.stringify(meta));

// Resizing the clip changes how long the cue holds, not its fade.
applyBlockLengthToStep(step, 3000);
meta = ensureStepTimeline(step);
check("resize writes duration", step.duration === 2800 && step.fade === "200", JSON.stringify(step));
check("resize is reflected", meta.length_ms === 3000, JSON.stringify(meta));

// Dragging the fade handle changes the fade.
applyBlockFadeToStep(step, 500);
meta = ensureStepTimeline(step);
check("fade handle writes fade", step.fade === "500", JSON.stringify(step));
check("fade-in follows", meta.fade_in_ms === 500, JSON.stringify(meta));

// A spread survives being dragged: only the fade time moves.
const spread = { name: "b", fade: "100 > 500", duration: 1000 };
meta = ensureStepTimeline(spread);
check("spread counts toward the length", meta.length_ms === 1600, JSON.stringify(meta));
check("spread counts toward the fade-in", meta.fade_in_ms === 600, JSON.stringify(meta));
applyBlockFadeToStep(spread, 900);
check("dragging keeps the spread", spread.fade === "400 > 500", spread.fade);

// Choosing "Cut" merges the spread back into one fade for everybody.
applyBlockOperatorToStep(spread, "");
check("cut merges the spread", spread.fade === "900", spread.fade);
applyBlockOperatorToStep(spread, "><");
check("an operator comes back", spread.fade === "900 >< 0", spread.fade);

// A clip can never be shorter than its own fade.
const tight = { name: "c", fade: "800", duration: 200 };
applyBlockLengthToStep(tight, 300);
check("hold cannot go negative", tight.duration === 0, JSON.stringify(tight));
check("the block keeps the fade", ensureStepTimeline(tight).length_ms === 800,
      JSON.stringify(ensureStepTimeline(tight)));

// A step still in the old model reads as a fade with no hold.
const legacy = { name: "d", sleep: 1000, duration: "400" };
meta = ensureStepTimeline(legacy);
check("legacy step: length is its fade", meta.length_ms === 400, JSON.stringify(meta));
"""
    _run_timeline_node(body)


def test_the_timeline_never_writes_a_derived_field_by_hand():
    """Every edit must go through the step, or it is undone on the next derive."""
    src = _read("static/timeline.js")
    for forbidden in ("meta.length_ms =", "meta.fade_in_ms =", "meta.fade_operator ="):
        occurrences = src.count(forbidden)
        # ensureStepTimeline is the one place allowed to set them: it derives.
        derive = src[src.index("  function ensureStepTimeline("):src.index("  // Editing a clip writes back")]
        assert occurrences == derive.count(forbidden), (
            f"{forbidden} is assigned outside ensureStepTimeline "
            f"({occurrences} total, {derive.count(forbidden)} in the deriver)"
        )
