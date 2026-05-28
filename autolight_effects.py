#!/usr/bin/env python3
"""Pre-made effects library for AutoLight.

Each effect is a short-duration transformation layered on top of the scene
engine's per-fixture values. Effects are *time-bounded* and scheduled on the
beat/bar grid. The :class:`EffectScheduler` picks which effect fires when,
using the current scene, BPM, section tempo (if available), and a cooldown.

Pipeline per frame:
  1. Scene engine computes base ``writes`` per fixture.
  2. Scheduler picks/advances the currently active effect.
  3. Active effect's ``transform(dev_id, caps, topology_fixture, writes, ctx)``
     mutates the writes dict in place.

Effects operate on channel semantics (dimmer, R, G, B, pan, tilt), not raw
channel indexes — so they work across every fixture template. They get the
per-device capability dict so they can target (e.g.) only strobe-friendly
units or only movers.
"""

from __future__ import annotations

import colorsys
import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# -----------------------------------------------------------------------------
# Channel-semantics helpers
# -----------------------------------------------------------------------------
#
# Every effect speaks in terms of brightness and color, not raw DMX channels.
# These helpers translate into ``writes`` for both master-dimmer fixtures
# (separate dimmer channel + RGB) and par-style fixtures (RGB only, brightness
# has to be baked into the color). This is what makes the effects work in
# ``effects_only`` mode where no scene baseline keeps the rig lit.


def _write_brightness(writes: Dict[int, int], caps: Dict[str, Any], level: int) -> None:
    """Set fixture brightness 0–255.

    * Master-dimmer fixture: writes to the dimmer channel, leaves color alone.
    * Par-style fixture (no master dimmer): scales RGB by the level, seeding
      with white if no color has been written yet.
    """
    level = max(0, min(255, int(level)))
    dim_ch = caps.get("dimmer_channel")
    if dim_ch is not None:
        writes[int(dim_ch)] = level
        return
    factor = level / 255.0
    for role in ("red_channel", "green_channel", "blue_channel"):
        ch = caps.get(role)
        if ch is None:
            continue
        current = writes.get(int(ch), 255)
        writes[int(ch)] = max(0, min(255, int(current * factor)))


def _write_color(writes: Dict[int, int], caps: Dict[str, Any], r: int, g: int, b: int, brightness: int = 255) -> None:
    """Set fixture to an explicit color + brightness.

    On master-dimmer fixtures the dimmer channel carries brightness and RGB
    stays at full saturation. On pars the RGB values are pre-multiplied by
    the brightness factor so the fixture actually reflects both.
    """
    brightness = max(0, min(255, int(brightness)))
    r_ch = caps.get("red_channel"); g_ch = caps.get("green_channel"); b_ch = caps.get("blue_channel")
    dim_ch = caps.get("dimmer_channel")
    if dim_ch is not None:
        writes[int(dim_ch)] = brightness
        if r_ch is not None: writes[int(r_ch)] = max(0, min(255, int(r)))
        if g_ch is not None: writes[int(g_ch)] = max(0, min(255, int(g)))
        if b_ch is not None: writes[int(b_ch)] = max(0, min(255, int(b)))
    else:
        factor = brightness / 255.0
        if r_ch is not None: writes[int(r_ch)] = max(0, min(255, int(r * factor)))
        if g_ch is not None: writes[int(g_ch)] = max(0, min(255, int(g * factor)))
        if b_ch is not None: writes[int(b_ch)] = max(0, min(255, int(b * factor)))


def _ensure_visible(writes: Dict[int, int], caps: Dict[str, Any], color: Tuple[int, int, int] = (255, 255, 255)) -> None:
    """Guarantee the fixture would be visually lit if its dimmer is non-zero.

    Fills missing dimmer/RGB channels with a full-brightness white baseline.
    Channels already present in ``writes`` are preserved, so dimmer-only
    effects keep their dim value while pars get the white seed needed to
    display anything.
    """
    dim_ch = caps.get("dimmer_channel")
    if dim_ch is not None and int(dim_ch) not in writes:
        writes[int(dim_ch)] = 255
    r_ch = caps.get("red_channel"); g_ch = caps.get("green_channel"); b_ch = caps.get("blue_channel")
    if r_ch is not None and int(r_ch) not in writes:
        writes[int(r_ch)] = int(color[0])
    if g_ch is not None and int(g_ch) not in writes:
        writes[int(g_ch)] = int(color[1])
    if b_ch is not None and int(b_ch) not in writes:
        writes[int(b_ch)] = int(color[2])


# -----------------------------------------------------------------------------
# Context passed to every effect tick
# -----------------------------------------------------------------------------

@dataclass
class EffectContext:
    now: float            # monotonic seconds
    t0: float             # effect start monotonic seconds
    elapsed: float        # now - t0
    duration: float       # total scheduled duration
    progress: float       # elapsed / duration in [0, 1]
    beat_period_s: float  # 60 / BPM (fallback 0.5 when BPM unknown)
    beats_elapsed: float  # elapsed / beat_period
    bar_position: float   # 0..1 inside the current bar (4 beats, 4/4 assumed)
    scene: str
    bass_norm: float
    mid_norm: float
    treble_norm: float
    global_hue: float
    # Beat envelope: 1.0 right on the kick, decays exponentially (~180 ms half-life).
    # Effects multiply this into brightness for music-locked accents instead of
    # writing a constant level.
    kick_env: float = 0.0
    # max(bass, mid, treble) normalized — quick "is the music loud" gauge so
    # ambient effects can ride a global energy wave without needing band logic.
    energy: float = 0.0


# -----------------------------------------------------------------------------
# Effect primitives
# -----------------------------------------------------------------------------

class Effect:
    """Base class. Subclasses override :meth:`transform` and optionally
    :meth:`default_duration_beats`."""
    name: str = "effect"
    # "ambient": long-running backdrop, the visual fabric (16-32 beats).
    # "accent":  shorter musical punctuation that sits on top (4-8 beats).
    # "impact":  hard short hits saved for drops / phrase boundaries (1-4 beats).
    # The scheduler weights kinds per scene so DROP is impact-heavy while VERSE
    # leans almost-pure-ambient.
    kind: str = "ambient"
    default_duration_beats: float = 16.0
    cooldown_bars: float = 1.0
    # Which scenes this effect is eligible for (matches scene name string).
    eligible_scenes: Tuple[str, ...] = ("VERSE", "CHORUS", "HIGH", "DROP")
    # BPM gating: effect is only picked when the detected BPM falls inside
    # this window. 0 disables the lower bound, 999 the upper. Fast effects
    # (strobes, stabs) set min_bpm > 100 so they don't appear on slow tracks.
    min_bpm: float = 0.0
    max_bpm: float = 999.0
    # Mood tags used by the UI mood filter. Order doesn't matter.
    mood_tags: Tuple[str, ...] = ()
    # Intrinsic preference among siblings of the same kind. The user's UI
    # weight (effect_config) multiplies on top.
    weight: float = 1.0

    def transform(
        self,
        dev_id: str,
        caps: Dict[str, Any],
        topo_fixture: Any,
        writes: Dict[int, int],
        ctx: EffectContext,
    ) -> None:
        raise NotImplementedError


class StrobeBurst(Effect):
    """12 Hz square-wave strobe on strobe-friendly fixtures; others hold steady."""
    name = "strobe_burst"
    kind = "impact"
    default_duration_beats = 4.0
    cooldown_bars = 4.0
    eligible_scenes = ("HIGH", "DROP")
    min_bpm = 110.0
    mood_tags = ("aggressive", "energetic")
    weight = 1.0

    def transform(self, dev_id, caps, topo, writes, ctx):
        # Strobe at 4 subdivisions per beat (16th notes) — lovely sync with
        # the music. Falls back to 12 Hz if BPM is unknown. Clamped so fast
        # tempos don't exceed fixture shutter speed.
        if ctx.beat_period_s > 0.01:
            hz = max(6.0, min(20.0, 4.0 / ctx.beat_period_s))
        else:
            hz = 12.0
        phase = int(ctx.now * hz * 2) % 2 == 0
        if caps.get("strobe_friendly"):
            _write_color(writes, caps, 255, 255, 255, 255 if phase else 0)
            return
        _write_brightness(writes, caps, 180)
        _ensure_visible(writes, caps)


class StrobeWhiteOut(Effect):
    """Full-white hard hit for 2 beats — the 'boom' moment."""
    name = "white_out"
    kind = "impact"
    default_duration_beats = 2.0
    cooldown_bars = 6.0
    eligible_scenes = ("DROP",)
    min_bpm = 90.0
    mood_tags = ("aggressive", "dramatic")
    weight = 0.7

    def transform(self, dev_id, caps, topo, writes, ctx):
        _write_color(writes, caps, 255, 255, 255, 255)


class BlackoutStab(Effect):
    """Everything off for 1 beat — the dramatic pause before the drop."""
    name = "blackout_stab"
    kind = "impact"
    default_duration_beats = 1.0
    cooldown_bars = 6.0
    eligible_scenes = ("CHORUS", "HIGH")
    min_bpm = 70.0
    mood_tags = ("dramatic",)
    weight = 0.5

    def transform(self, dev_id, caps, topo, writes, ctx):
        _write_color(writes, caps, 0, 0, 0, 0)


class BuildUpSweep(Effect):
    """Chaser that accelerates from slow→fast, brightening over 4 bars."""
    name = "build_up_sweep"
    kind = "accent"
    default_duration_beats = 16.0
    cooldown_bars = 6.0
    eligible_scenes = ("VERSE", "CHORUS")
    min_bpm = 70.0
    max_bpm = 180.0
    mood_tags = ("energetic", "cinematic")
    weight = 0.8

    def transform(self, dev_id, caps, topo, writes, ctx):
        rate = 0.8 + 3.2 * ctx.progress
        order = getattr(topo, "order_index", 0) if topo else 0
        width = max(0.08, 0.25 - 0.15 * ctx.progress)
        head = (ctx.elapsed * rate) % 1.0
        n = max(1, getattr(topo, "_chaser_len", 16) if topo else 16)
        pos = (order % n) / n
        d = abs(pos - head)
        d = min(d, 1.0 - d)
        envelope = max(0.0, 1.0 - (d / width))
        level = int(envelope * 255 * (0.4 + 0.6 * ctx.progress))
        _write_brightness(writes, caps, level)
        if level > 0:
            _ensure_visible(writes, caps)


class ColorFlashRed(Effect):
    """Saturate every color fixture to red for 2 beats."""
    name = "color_flash_red"
    kind = "impact"
    default_duration_beats = 2.0
    cooldown_bars = 5.0
    eligible_scenes = ("HIGH", "DROP")
    min_bpm = 90.0
    mood_tags = ("aggressive",)
    weight = 0.6

    def transform(self, dev_id, caps, topo, writes, ctx):
        _write_color(writes, caps, 255, 0, 0, 255)


class RainbowSweep(Effect):
    """Smooth hue rotation across the rig — one full revolution every 8 bars."""
    name = "rainbow_sweep"
    kind = "ambient"
    default_duration_beats = 24.0
    cooldown_bars = 6.0
    eligible_scenes = ("VERSE", "CHORUS", "HIGH")
    max_bpm = 180.0
    mood_tags = ("calm", "energetic")
    weight = 1.2

    def transform(self, dev_id, caps, topo, writes, ctx):
        n = max(1, getattr(topo, "_chaser_len", 16) if topo else 16)
        order = getattr(topo, "order_index", 0) if topo else 0
        # Hue cycles once every 8 bars (32 beats) — slow enough to read as a
        # gradient rather than a chase, fast enough to feel alive.
        hue = ((order / n) + (ctx.beats_elapsed / 32.0)) % 1.0
        rf, gf, bf = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
        # Brightness rides the kick: 70% baseline + 30% beat pulse.
        brightness = int(180 + 75 * ctx.kick_env)
        _write_color(writes, caps, int(rf * 255), int(gf * 255), int(bf * 255), brightness)


class MirrorPingPong(Effect):
    """Left/right mirror pairs alternate flashes every half-beat for 2 bars."""
    name = "mirror_pingpong"
    kind = "accent"
    default_duration_beats = 8.0
    cooldown_bars = 4.0
    eligible_scenes = ("CHORUS", "HIGH")
    min_bpm = 80.0
    max_bpm = 180.0
    mood_tags = ("energetic",)
    weight = 1.0

    def transform(self, dev_id, caps, topo, writes, ctx):
        side = getattr(topo, "mirror_side", None) if topo else None
        phase = int(ctx.beats_elapsed * 2) % 2 == 0
        on = (side == "left" and phase) or (side == "right" and not phase)
        # Unpaired fixtures land at half brightness so they still visibly pulse.
        if side is None:
            level = 150 if phase else 40
        else:
            level = 255 if on else 25
        _write_brightness(writes, caps, level)
        _ensure_visible(writes, caps)


class LaserSwirl(Effect):
    """Movers trace a circle while lit; static fixtures stay bright, 6 bars."""
    name = "laser_swirl"
    kind = "ambient"
    default_duration_beats = 24.0
    cooldown_bars = 8.0
    eligible_scenes = ("CHORUS", "HIGH", "DROP")
    min_bpm = 70.0
    max_bpm = 200.0
    mood_tags = ("cinematic", "energetic")
    weight = 1.0

    def transform(self, dev_id, caps, topo, writes, ctx):
        if caps.get("has_movement"):
            pan_ch = caps.get("pan_channel")
            tilt_ch = caps.get("tilt_channel")
            if pan_ch is not None and tilt_ch is not None:
                n = max(1, getattr(topo, "_chaser_len", 16) if topo else 16)
                order = getattr(topo, "order_index", 0) if topo else 0
                phi = ctx.bar_position * 2.0 * math.pi + (order / n) * 2.0 * math.pi
                amp = 80
                writes[int(pan_ch)] = max(0, min(255, 128 + int(amp * math.cos(phi))))
                writes[int(tilt_ch)] = max(0, min(255, 128 + int(amp * math.sin(phi))))
        # Light up every fixture so the swirl is visible; movers get a pulsing
        # sine brightness, static ones hold steady bright.
        if caps.get("has_movement"):
            level = 180 + int(60 * math.sin(ctx.bar_position * 2.0 * math.pi))
        else:
            level = 200
        _write_brightness(writes, caps, level)
        _ensure_visible(writes, caps)


class PumpUp(Effect):
    """Dimmer ramps up to full across 2 bars — pre-drop tension."""
    name = "pump_up"
    kind = "accent"
    default_duration_beats = 8.0
    cooldown_bars = 5.0
    eligible_scenes = ("VERSE", "CHORUS")
    min_bpm = 70.0
    max_bpm = 200.0
    mood_tags = ("energetic",)
    weight = 0.8

    def transform(self, dev_id, caps, topo, writes, ctx):
        target = int(60 + 195 * ctx.progress)
        _write_brightness(writes, caps, target)
        _ensure_visible(writes, caps)


class FadeDown(Effect):
    """Gentle fade from full to near-dark over 2 bars — the breath moment."""
    name = "fade_down"
    kind = "accent"
    default_duration_beats = 8.0
    cooldown_bars = 5.0
    eligible_scenes = ("VERSE", "CHORUS")
    max_bpm = 130.0
    mood_tags = ("calm", "cinematic")
    weight = 0.7

    def transform(self, dev_id, caps, topo, writes, ctx):
        # Always start fully lit and fade down. Works whether writes is empty
        # (effects_only) or already populated by the scene engine.
        level = int(max(20, 255 * (1.0 - ctx.progress * 0.92)))
        _write_brightness(writes, caps, level)
        _ensure_visible(writes, caps)


class SnapBack(Effect):
    """Hard all-on for one beat: the punctuation."""
    name = "snap_back"
    kind = "impact"
    default_duration_beats = 1.0
    cooldown_bars = 3.0
    eligible_scenes = ("HIGH", "DROP")
    min_bpm = 100.0
    mood_tags = ("aggressive",)
    weight = 0.6

    def transform(self, dev_id, caps, topo, writes, ctx):
        _write_brightness(writes, caps, 255)
        _ensure_visible(writes, caps)


class WaveRoll(Effect):
    """Dimmer wave rolling left→right — one wave per bar, repeats."""
    name = "wave_roll"
    kind = "ambient"
    default_duration_beats = 16.0
    cooldown_bars = 5.0
    eligible_scenes = ("VERSE", "CHORUS", "HIGH")
    min_bpm = 60.0
    max_bpm = 180.0
    mood_tags = ("calm", "energetic")
    weight = 1.1

    def transform(self, dev_id, caps, topo, writes, ctx):
        n = max(1, getattr(topo, "_chaser_len", 16) if topo else 16)
        order = getattr(topo, "order_index", 0) if topo else 0
        pos = (order % n) / n
        # One wave-front per 2 bars (8 beats) → readable motion locked to phrase.
        head = (ctx.beats_elapsed / 8.0) % 1.0
        d = pos - head
        if d < 0:
            d += 1.0
        envelope = math.exp(-(d * 8.0) ** 2)
        # Bass kick adds a global brightness pop on top of the wave.
        level = int(40 + 195 * envelope + 20 * ctx.kick_env)
        level = max(0, min(255, level))
        _write_brightness(writes, caps, level)
        _ensure_visible(writes, caps)


class CircleChase(Effect):
    """Chaser cycling around the rig, reverses every bar."""
    name = "circle_chase"
    kind = "ambient"
    default_duration_beats = 16.0
    cooldown_bars = 5.0
    eligible_scenes = ("CHORUS", "HIGH")
    min_bpm = 90.0
    max_bpm = 200.0
    mood_tags = ("energetic",)
    weight = 0.9

    def transform(self, dev_id, caps, topo, writes, ctx):
        n = max(1, getattr(topo, "_chaser_len", 16) if topo else 16)
        order = getattr(topo, "order_index", 0) if topo else 0
        direction = 1.0 if (int(ctx.beats_elapsed) // 4) % 2 == 0 else -1.0
        # Two cycles per bar regardless of effect duration.
        head = (ctx.beats_elapsed * 0.5 * direction) % 1.0
        pos = (order % n) / n
        d = abs(pos - head)
        d = min(d, 1.0 - d)
        envelope = max(0.0, 1.0 - d * 6.0)
        level = int(50 + 205 * envelope + 20 * ctx.kick_env)
        level = max(0, min(255, level))
        _write_brightness(writes, caps, level)
        _ensure_visible(writes, caps)


class PoliceLights(Effect):
    """Red/blue alternating at 2 Hz on mirror pairs — party alert, 1 bar."""
    name = "police_lights"
    kind = "impact"
    default_duration_beats = 4.0
    cooldown_bars = 8.0
    eligible_scenes = ("HIGH", "DROP")
    min_bpm = 110.0
    max_bpm = 200.0
    mood_tags = ("aggressive", "energetic")
    weight = 0.5

    def transform(self, dev_id, caps, topo, writes, ctx):
        phase = int(ctx.now * 4) % 2 == 0
        side = getattr(topo, "mirror_side", None) if topo else None
        if side == "right":
            red_on = not phase
        else:
            red_on = phase
        if red_on:
            _write_color(writes, caps, 255, 0, 0, 255)
        else:
            _write_color(writes, caps, 0, 0, 255, 255)


class HueBounce(Effect):
    """Global hue sweeps over 4 bars; saturated colors everywhere with beat pulse."""
    name = "hue_bounce"
    kind = "ambient"
    default_duration_beats = 16.0
    cooldown_bars = 5.0
    eligible_scenes = ("CHORUS", "HIGH")
    min_bpm = 80.0
    max_bpm = 200.0
    mood_tags = ("energetic",)
    weight = 0.9

    def transform(self, dev_id, caps, topo, writes, ctx):
        # Hue describes a sine arc, so it sweeps out and back, peaking mid-effect.
        phase = math.sin(ctx.progress * math.pi)
        hue = (ctx.global_hue + phase * 0.5) % 1.0
        rf, gf, bf = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
        brightness = int(180 + 75 * ctx.kick_env)
        _write_color(writes, caps, int(rf * 255), int(gf * 255), int(bf * 255), brightness)


class TwinkleStars(Effect):
    """Random individual flashes — stable hash per fixture+beat, 6 bars."""
    name = "twinkle_stars"
    kind = "ambient"
    default_duration_beats = 24.0
    cooldown_bars = 6.0
    eligible_scenes = ("VERSE", "CHORUS")
    max_bpm = 130.0
    mood_tags = ("calm",)
    weight = 0.8

    def transform(self, dev_id, caps, topo, writes, ctx):
        beat_bucket = int(ctx.beats_elapsed * 2)
        h = hash((dev_id, beat_bucket)) & 0xff
        on = h < 80
        # On-fixtures peak with the beat envelope, off-fixtures stay at a soft
        # ember so the rig never goes fully dark between twinkles.
        if on:
            level = int(180 + 75 * ctx.kick_env)
            _write_brightness(writes, caps, level)
            _ensure_visible(writes, caps, (255, 240, 200))  # warm white twinkle
        else:
            _write_brightness(writes, caps, 30)
            _ensure_visible(writes, caps, (255, 240, 200))


class ColorSplit(Effect):
    """Left side = warm, right side = cool — 4 bars; brightness pulses on beat."""
    name = "color_split"
    kind = "ambient"
    default_duration_beats = 16.0
    cooldown_bars = 6.0
    eligible_scenes = ("VERSE", "CHORUS", "HIGH")
    max_bpm = 160.0
    mood_tags = ("calm", "cinematic")
    weight = 0.9

    def transform(self, dev_id, caps, topo, writes, ctx):
        side = getattr(topo, "mirror_side", None) if topo else None
        brightness = int(170 + 85 * ctx.kick_env)
        if side == "left":
            _write_color(writes, caps, 255, 90, 0, brightness)
        elif side == "right":
            _write_color(writes, caps, 0, 110, 255, brightness)
        else:
            _write_color(writes, caps, 140, 100, 140, brightness)


class FanSpread(Effect):
    """Movers tilt outward from center, forming a fan — 3 bars."""
    name = "fan_spread"
    kind = "accent"
    default_duration_beats = 12.0
    cooldown_bars = 6.0
    eligible_scenes = ("CHORUS", "HIGH")
    min_bpm = 80.0
    max_bpm = 180.0
    mood_tags = ("cinematic", "energetic")
    weight = 0.8

    def transform(self, dev_id, caps, topo, writes, ctx):
        if caps.get("has_movement"):
            pan_ch = caps.get("pan_channel")
            tilt_ch = caps.get("tilt_channel")
            if pan_ch is not None and tilt_ch is not None:
                side = getattr(topo, "mirror_side", None) if topo else None
                order = getattr(topo, "order_index", 0) if topo else 0
                amp_pan = int(80 * ctx.progress)
                sign = -1 if side == "left" else (1 if side == "right" else 0)
                writes[int(pan_ch)] = max(0, min(255, 128 + sign * amp_pan))
                tilt_off = int(30 * math.sin(ctx.bar_position * math.pi * 2 + order * 0.5))
                writes[int(tilt_ch)] = max(0, min(255, 128 + tilt_off))
        # Light up everything — movers trace the fan, pars fill the stage.
        _write_brightness(writes, caps, 220)
        _ensure_visible(writes, caps)


class CenterOut(Effect):
    """Dimmer blooms from center outward, reaches full by end — 3 bars."""
    name = "center_out"
    kind = "accent"
    default_duration_beats = 12.0
    cooldown_bars = 6.0
    eligible_scenes = ("VERSE", "CHORUS", "HIGH")
    min_bpm = 60.0
    max_bpm = 160.0
    mood_tags = ("cinematic",)
    weight = 0.8

    def transform(self, dev_id, caps, topo, writes, ctx):
        cluster_x = getattr(topo, "cluster_x", 1) if topo else 1
        if cluster_x == 1:
            amp = min(1.0, ctx.progress * 2.5)
        else:
            amp = max(0.0, (ctx.progress - 0.35) * 1.8)
        level = int(255 * amp)
        _write_brightness(writes, caps, level)
        if level > 0:
            _ensure_visible(writes, caps)


class HeartBeat(Effect):
    """Bump-bump-pause pattern locked to detected beats — 2 bars."""
    name = "heart_beat"
    kind = "accent"
    default_duration_beats = 8.0
    cooldown_bars = 5.0
    eligible_scenes = ("VERSE", "CHORUS")
    max_bpm = 120.0
    mood_tags = ("cinematic",)
    weight = 0.6

    def transform(self, dev_id, caps, topo, writes, ctx):
        b = ctx.beats_elapsed
        pos = b - math.floor(b / 1.5) * 1.5
        hit = (pos < 0.12) or (0.4 <= pos < 0.52)
        # Use kick_env on hits so the heartbeat naturally aligns to detected
        # beats rather than the BPM-estimated phase, which can drift.
        if hit:
            level = int(200 + 55 * ctx.kick_env)
        else:
            level = 30
        _write_brightness(writes, caps, level)
        _ensure_visible(writes, caps, (255, 60, 60))  # subtle red tint — heartbeat


class AccelerandoChase(Effect):
    """Chaser that starts slow and accelerates over 2 bars — pre-drop tension."""
    name = "accelerando_chase"
    kind = "accent"
    default_duration_beats = 8.0
    cooldown_bars = 5.0
    eligible_scenes = ("CHORUS", "HIGH")
    min_bpm = 100.0
    max_bpm = 200.0
    mood_tags = ("energetic", "aggressive")
    weight = 0.7

    def transform(self, dev_id, caps, topo, writes, ctx):
        n = max(1, getattr(topo, "_chaser_len", 16) if topo else 16)
        order = getattr(topo, "order_index", 0) if topo else 0
        rate = 0.5 + 6.0 * ctx.progress * ctx.progress
        head = (ctx.elapsed * rate) % 1.0
        pos = (order % n) / n
        d = abs(pos - head)
        d = min(d, 1.0 - d)
        env = max(0.0, 1.0 - d * 7.0)
        level = int(35 + 220 * env)
        _write_brightness(writes, caps, level)
        _ensure_visible(writes, caps)


class HalfBlackout(Effect):
    """One mirror side blacks out while the other stays lit — 1 bar."""
    name = "half_blackout"
    kind = "accent"
    default_duration_beats = 4.0
    cooldown_bars = 5.0
    eligible_scenes = ("CHORUS", "HIGH", "DROP")
    min_bpm = 80.0
    max_bpm = 180.0
    mood_tags = ("dramatic",)
    weight = 0.6

    def transform(self, dev_id, caps, topo, writes, ctx):
        side = getattr(topo, "mirror_side", None) if topo else None
        kill_left = int(ctx.beats_elapsed) % 2 == 0
        killed = (side == "left" and kill_left) or (side == "right" and not kill_left)
        if killed:
            _write_color(writes, caps, 0, 0, 0, 0)
        else:
            _write_brightness(writes, caps, 255)
            _ensure_visible(writes, caps)


class BeatPunch(Effect):
    """Hard dimmer spike locked to the live kick — 2 bars."""
    name = "beat_punch"
    kind = "accent"
    default_duration_beats = 8.0
    cooldown_bars = 4.0
    eligible_scenes = ("CHORUS", "HIGH", "DROP")
    min_bpm = 100.0
    max_bpm = 220.0
    mood_tags = ("aggressive", "energetic")
    weight = 1.0

    def transform(self, dev_id, caps, topo, writes, ctx):
        # Use the audio-detected kick envelope directly — the punches always
        # hit the actual drum, never drift with BPM-estimation error.
        level = int(40 + 215 * ctx.kick_env)
        _write_brightness(writes, caps, level)
        _ensure_visible(writes, caps)


class LaserCircle(Effect):
    """Movers trace tight circles; pars stay lit — 6 bars."""
    name = "laser_circle"
    kind = "ambient"
    default_duration_beats = 24.0
    cooldown_bars = 8.0
    eligible_scenes = ("CHORUS", "HIGH", "DROP")
    min_bpm = 80.0
    max_bpm = 200.0
    mood_tags = ("cinematic",)
    weight = 0.9

    def transform(self, dev_id, caps, topo, writes, ctx):
        if caps.get("has_movement"):
            pan_ch = caps.get("pan_channel")
            tilt_ch = caps.get("tilt_channel")
            if pan_ch is not None and tilt_ch is not None:
                n = max(1, getattr(topo, "_chaser_len", 16) if topo else 16)
                order = getattr(topo, "order_index", 0) if topo else 0
                phi = ctx.beats_elapsed * math.pi + (order / n) * math.pi * 2
                amp = 40
                writes[int(pan_ch)] = max(0, min(255, 128 + int(amp * math.cos(phi))))
                writes[int(tilt_ch)] = max(0, min(255, 128 + int(amp * math.sin(phi))))
        level = int(180 + 60 * ctx.kick_env)
        _write_brightness(writes, caps, level)
        _ensure_visible(writes, caps)


class ColorFlashCycle(Effect):
    """Cycle through red→green→blue→white, one per beat, 2 bars."""
    name = "color_flash_cycle"
    kind = "accent"
    default_duration_beats = 8.0
    cooldown_bars = 5.0
    eligible_scenes = ("HIGH", "DROP")
    min_bpm = 90.0
    mood_tags = ("energetic",)
    weight = 0.7

    def transform(self, dev_id, caps, topo, writes, ctx):
        palette = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)]
        rv, gv, bv = palette[int(ctx.beats_elapsed) % 4]
        brightness = int(200 + 55 * ctx.kick_env)
        _write_color(writes, caps, rv, gv, bv, brightness)


class SweepFlash(Effect):
    """Ultra-fast sweep with comet trail — 1 bar."""
    name = "sweep_flash"
    kind = "impact"
    default_duration_beats = 4.0
    cooldown_bars = 4.0
    eligible_scenes = ("HIGH", "DROP")
    min_bpm = 110.0
    max_bpm = 220.0
    mood_tags = ("energetic", "aggressive")
    weight = 0.7

    def transform(self, dev_id, caps, topo, writes, ctx):
        n = max(1, getattr(topo, "_chaser_len", 16) if topo else 16)
        order = getattr(topo, "order_index", 0) if topo else 0
        head = ctx.progress
        pos = (order % n) / n
        d = pos - head
        envelope = 0.0 if d < 0 else max(0.0, 1.0 - d * 10.0)
        level = int(255 * envelope)
        _write_brightness(writes, caps, level)
        if level > 0:
            _ensure_visible(writes, caps)


class SlowBreathe(Effect):
    """Slow sine breathing — one breath per 2 bars, 6 bars total."""
    name = "slow_breathe"
    kind = "ambient"
    default_duration_beats = 24.0
    cooldown_bars = 6.0
    eligible_scenes = ("VERSE",)
    max_bpm = 110.0
    mood_tags = ("calm",)
    weight = 1.0

    def transform(self, dev_id, caps, topo, writes, ctx):
        # One breath cycle per 2 bars (8 beats), independent of effect duration.
        env = 0.5 + 0.5 * math.sin((ctx.beats_elapsed / 8.0) * math.pi * 2)
        # Explicit color write so par fixtures (no master dimmer) actually light
        # up — _write_brightness alone gets zeroed by an empty scene baseline.
        level = int(60 + 130 * env + 25 * ctx.kick_env)
        level = max(0, min(255, level))
        _write_color(writes, caps, 180, 200, 255, level)


class RandomStabs(Effect):
    """Single-beat stabs on random fixtures — 1 bar."""
    name = "random_stabs"
    kind = "impact"
    default_duration_beats = 4.0
    cooldown_bars = 5.0
    eligible_scenes = ("HIGH", "DROP")
    min_bpm = 110.0
    max_bpm = 220.0
    mood_tags = ("aggressive", "energetic")
    weight = 0.7

    def transform(self, dev_id, caps, topo, writes, ctx):
        beat_bucket = int(ctx.beats_elapsed)
        h = hash((dev_id, beat_bucket, "stab")) & 0xff
        chosen = h < 60
        pos_in_beat = ctx.beats_elapsed - math.floor(ctx.beats_elapsed)
        if chosen and pos_in_beat < 0.3:
            _write_brightness(writes, caps, 255)
            _ensure_visible(writes, caps)
        else:
            _write_brightness(writes, caps, 25)


class AmbientGlow(Effect):
    """Slow hue drift at low brightness — the default fabric for silent / verse.

    Always available regardless of BPM. Fills the 'between songs' moments
    instead of going to a black rig. Rides the kick gently when audio is
    present.
    """
    name = "ambient_glow"
    kind = "ambient"
    default_duration_beats = 32.0
    cooldown_bars = 4.0
    eligible_scenes = ("SILENT", "VERSE", "CHORUS")
    min_bpm = 0.0
    max_bpm = 999.0
    mood_tags = ("calm", "cinematic")
    weight = 1.4

    def transform(self, dev_id, caps, topo, writes, ctx):
        n = max(1, getattr(topo, "_chaser_len", 16) if topo else 16)
        order = getattr(topo, "order_index", 0) if topo else 0
        # Hue drifts very slowly (one full cycle per 16 bars). Adjacent
        # fixtures sit slightly apart on the wheel so the rig reads as a
        # gradient, not a single colour.
        hue = ((order / n) * 0.20 + ctx.beats_elapsed / 64.0 + ctx.global_hue * 0.15) % 1.0
        rf, gf, bf = colorsys.hsv_to_rgb(hue, 0.85, 1.0)
        # Brightness floor so it's always visibly on; rises with energy + beat.
        level = int(60 + 60 * ctx.energy + 60 * ctx.kick_env)
        level = max(0, min(190, level))  # cap so it's never blinding
        _write_color(writes, caps, int(rf * 255), int(gf * 255), int(bf * 255), level)


class EmberDrift(Effect):
    """Warm amber/red drift with slow tilt sway — late-night calm, 8 bars."""
    name = "ember_drift"
    kind = "ambient"
    default_duration_beats = 32.0
    cooldown_bars = 6.0
    eligible_scenes = ("SILENT", "VERSE")
    min_bpm = 0.0
    max_bpm = 110.0
    mood_tags = ("calm", "cinematic")
    weight = 0.9

    def transform(self, dev_id, caps, topo, writes, ctx):
        order = getattr(topo, "order_index", 0) if topo else 0
        # Phased breathing per fixture so the rig looks like a flickering
        # ember field rather than one synchronized pulse.
        phase = order * 0.45 + ctx.beats_elapsed / 6.0
        env = 0.5 + 0.5 * math.sin(phase * math.pi * 2)
        level = int(50 + 90 * env + 30 * ctx.kick_env)
        level = max(0, min(180, level))
        _write_color(writes, caps, 255, 110, 30, level)
        if caps.get("has_movement"):
            tilt_ch = caps.get("tilt_channel")
            if tilt_ch is not None:
                tilt = 110 + int(30 * math.sin(ctx.beats_elapsed * 0.4 + order))
                writes[int(tilt_ch)] = max(0, min(255, tilt))


class DeepPulse(Effect):
    """All-rig low-frequency throb: dim → bright once per bar, 6 bars.

    Designed for verse/chorus when you want the rig present but not busy.
    Locks the throb to detected beats via kick_env so it can't drift off
    the music.
    """
    name = "deep_pulse"
    kind = "ambient"
    default_duration_beats = 24.0
    cooldown_bars = 6.0
    eligible_scenes = ("VERSE", "CHORUS", "HIGH")
    min_bpm = 60.0
    max_bpm = 180.0
    mood_tags = ("cinematic", "energetic")
    weight = 1.0

    def transform(self, dev_id, caps, topo, writes, ctx):
        # Slow phrase wave (one per bar) layered with the live kick — the eye
        # reads the slow wave, the body reads the kick.
        phrase_env = 0.5 + 0.5 * math.sin((ctx.beats_elapsed / 4.0) * math.pi * 2)
        level = int(50 + 130 * phrase_env + 75 * ctx.kick_env)
        level = max(0, min(255, level))
        # Saturated hue sourced from the global wheel so it harmonises with
        # other ambient picks during cross-fades.
        rf, gf, bf = colorsys.hsv_to_rgb(ctx.global_hue, 0.9, 1.0)
        _write_color(writes, caps, int(rf * 255), int(gf * 255), int(bf * 255), level)


# Registry used by the scheduler — insertion order is cosmetic.
_EFFECT_REGISTRY: List[Effect] = [
    AmbientGlow(),
    EmberDrift(),
    DeepPulse(),
    StrobeBurst(),
    StrobeWhiteOut(),
    BlackoutStab(),
    BuildUpSweep(),
    ColorFlashRed(),
    RainbowSweep(),
    MirrorPingPong(),
    LaserSwirl(),
    PumpUp(),
    FadeDown(),
    SnapBack(),
    WaveRoll(),
    CircleChase(),
    PoliceLights(),
    HueBounce(),
    TwinkleStars(),
    ColorSplit(),
    FanSpread(),
    CenterOut(),
    HeartBeat(),
    AccelerandoChase(),
    HalfBlackout(),
    BeatPunch(),
    LaserCircle(),
    ColorFlashCycle(),
    SweepFlash(),
    SlowBreathe(),
    RandomStabs(),
]


def list_effects_meta() -> List[Dict[str, Any]]:
    """Public metadata for UI + API callers. Stable across reloads."""
    return [
        {
            "name": eff.name,
            "kind": eff.kind,
            "default_duration_beats": float(eff.default_duration_beats),
            "cooldown_bars": float(eff.cooldown_bars),
            "eligible_scenes": list(eff.eligible_scenes),
            "min_bpm": float(eff.min_bpm),
            "max_bpm": float(eff.max_bpm),
            "mood_tags": list(eff.mood_tags),
            "weight": float(eff.weight),
        }
        for eff in _EFFECT_REGISTRY
    ]


def all_mood_tags() -> List[str]:
    """Sorted union of every mood used by the registry — for the UI filter."""
    moods: set = set()
    for eff in _EFFECT_REGISTRY:
        moods.update(eff.mood_tags)
    return sorted(moods)


# -----------------------------------------------------------------------------
# Scheduler
# -----------------------------------------------------------------------------

@dataclass
class _ActiveEffect:
    effect: Effect
    start_ts: float
    duration_s: float


class EffectScheduler:
    """Picks and tracks one active effect at a time, biased by scene-kind weighting.

    The renderer calls :meth:`tick` every frame. Effects are evaluated:

    1. On scene transitions (DROP-up, CHORUS→HIGH...) → fire a fresh pick
       immediately; this is what makes the rig "react" to musical structure.
    2. When the current effect expires → pick the next one immediately so the
       rig is never visually idle while audio plays.
    3. On bar boundaries → low-prob re-pick to introduce variation mid-effect
       (currently disabled in favour of letting effects play out).

    Cooldown per-effect type prevents the same effect repeating, and per-scene
    kind weights bias the pool: VERSE leans ambient, DROP leans impact.
    """

    # Per-scene preference for each effect kind. The actual weight used at
    # pick time is ``effect.weight * scene_kind_weights[kind]``. Zero means
    # "kind not eligible in this scene".
    _SCENE_KIND_WEIGHTS: Dict[str, Dict[str, float]] = {
        "SILENT": {"ambient": 1.0, "accent": 0.0,  "impact": 0.0},
        "VERSE":  {"ambient": 1.0, "accent": 0.25, "impact": 0.0},
        "CHORUS": {"ambient": 0.7, "accent": 0.7,  "impact": 0.1},
        "HIGH":   {"ambient": 0.5, "accent": 0.8,  "impact": 0.4},
        "DROP":   {"ambient": 0.3, "accent": 0.7,  "impact": 1.0},
    }

    # Energy ranking for scene transitions. A move to a higher rank counts as
    # a "level up" and triggers an instant re-pick, often biased toward impact.
    _SCENE_ENERGY = {"SILENT": 0, "VERSE": 1, "CHORUS": 2, "HIGH": 3, "DROP": 4}

    def __init__(self, registry: Optional[List[Effect]] = None) -> None:
        self._registry: List[Effect] = list(registry or _EFFECT_REGISTRY)
        self._active: Optional[_ActiveEffect] = None
        self._last_trigger_ts: Dict[str, float] = {}
        self._last_bar_seen: int = -1
        self._last_seen_scene: str = "SILENT"
        self._rng = random.Random()
        self.last_chosen: Optional[str] = None
        self.trigger_count: int = 0
        self.history: List[Tuple[float, str]] = []
        # Per-effect config: {name: {"enabled": bool, "weight": float,
        # "duration_beats": float, "cooldown_bars": float}}. Missing keys
        # fall back to the effect's built-in defaults. Weight 0 = skip.
        self._config: Dict[str, Dict[str, Any]] = {}
        # Mood filter: when non-empty, only effects with at least one matching
        # tag are eligible. Empty = no filter (all moods OK).
        self._mood_filter: Tuple[str, ...] = ()
        # If True, require minimum BPM-confidence before firing "fast" effects.
        self._bpm_confidence_gate: float = 0.0  # disabled by default

    def current_effect_name(self) -> Optional[str]:
        return self._active.effect.name if self._active else None

    def set_seed(self, seed: Any) -> None:
        self._rng.seed(seed)

    def set_config(self, config: Dict[str, Dict[str, Any]]) -> None:
        """Replace the per-effect config with a fresh mapping."""
        self._config = {str(k): dict(v) for k, v in (config or {}).items() if isinstance(v, dict)}

    def set_mood_filter(self, moods: Any) -> None:
        """Limit picks to effects tagged with at least one of ``moods``.

        Pass ``None`` / ``[]`` to disable.
        """
        if not moods:
            self._mood_filter = ()
            return
        if isinstance(moods, str):
            moods = [moods]
        self._mood_filter = tuple(str(m).strip().lower() for m in moods if str(m).strip())

    def set_bpm_confidence_gate(self, threshold: float) -> None:
        """Require ``threshold`` BPM-confidence before picking BPM-constrained effects.

        0 = disabled (pick regardless). 0.5 = moderate (skip if intervals too jittery).
        """
        self._bpm_confidence_gate = max(0.0, min(1.0, float(threshold)))

    def get_config(self) -> Dict[str, Dict[str, Any]]:
        """Return the currently active per-effect config."""
        return {k: dict(v) for k, v in self._config.items()}

    def effective_effect_params(self, effect: Effect) -> Dict[str, Any]:
        """Resolve (enabled, weight, duration_beats, cooldown_bars) for an effect."""
        cfg = self._config.get(effect.name, {})
        enabled = bool(cfg.get("enabled", True))
        weight = float(cfg.get("weight", 1.0))
        if weight < 0.0:
            weight = 0.0
        duration_beats = float(cfg.get("duration_beats", effect.default_duration_beats))
        if duration_beats <= 0:
            duration_beats = effect.default_duration_beats
        cooldown_bars = float(cfg.get("cooldown_bars", effect.cooldown_bars))
        if cooldown_bars < 0:
            cooldown_bars = effect.cooldown_bars
        return {
            "enabled": enabled,
            "weight": weight,
            "duration_beats": duration_beats,
            "cooldown_bars": cooldown_bars,
        }

    def tick(
        self,
        now: float,
        scene: str,
        bpm: float,
        bar_count: int,
        last_beat_ms: float,
        audio_active: bool,
        continuous: bool = True,
        bpm_confidence: float = 1.0,
    ) -> Optional[EffectContext]:
        """Return the active EffectContext for this frame, or None.

        Default behaviour is **always-on**: as long as audio is playing, an
        effect is running. When the current effect expires we pick the next
        one immediately so there's no visual gap. Pass ``continuous=False``
        to fall back to the legacy bar-trigger behaviour (effect runs, then
        the rig waits for a bar-roll dice roll before the next pick).

        On scene transitions we force a re-pick — that's what makes the rig
        actually react to a chorus kicking in or a drop hitting, instead of
        finishing whatever long ambient was running and only then noticing.
        """
        beat_period = 60.0 / bpm if bpm >= 50.0 else 0.5

        # Detect scene transitions — used to force an immediate re-pick when
        # the music levels up or down. Doing this *before* expiry means a
        # drop hitting mid-ambient can interrupt with an impact.
        scene_changed = scene != self._last_seen_scene
        old_scene = self._last_seen_scene
        self._last_seen_scene = scene

        # Expire the active effect if past its duration.
        if self._active is not None:
            elapsed = now - self._active.start_ts
            if elapsed >= self._active.duration_s:
                self._last_trigger_ts[self._active.effect.name] = now
                self._active = None

        # Scene transition — force a fresh pick when energy goes up. Going
        # down (CHORUS→VERSE) we let the active effect play out; the next
        # natural expiry will pick something calmer.
        if scene_changed and audio_active:
            up = self._SCENE_ENERGY.get(scene, 0) > self._SCENE_ENERGY.get(old_scene, 0)
            if up:
                # Bias toward impact when entering DROP, otherwise just pick
                # whatever fits the new scene best.
                kind_bias = "impact" if scene == "DROP" else None
                candidate = self._pick_for_scene(
                    scene, now, beat_period, bpm, bpm_confidence,
                    kind_bias=kind_bias, allow_repeat=False,
                )
                if candidate is not None:
                    self._activate(candidate, now, beat_period)

        # Bar-boundary re-pick — only fires when no effect is active. Effects
        # always play through their full duration; mid-flight interruptions
        # are reserved for scene transitions above.
        if bar_count > self._last_bar_seen:
            self._last_bar_seen = bar_count

        # Always-on chain: whenever nothing is active, pick immediately. This
        # is the difference between "rig responds to music" and "rig
        # occasionally flashes". For SILENT we still pick — AmbientGlow gives
        # us a calm baseline instead of a black rig.
        if continuous and self._active is None and audio_active:
            candidate = self._pick_for_scene(scene, now, beat_period, bpm, bpm_confidence)
            if candidate is not None:
                self._activate(candidate, now, beat_period)

        if self._active is None:
            return None

        elapsed = now - self._active.start_ts
        progress = elapsed / max(0.001, self._active.duration_s)
        bar_period = beat_period * 4.0
        if last_beat_ms > 0:
            seconds_since_beat = (now * 1000.0 - last_beat_ms) / 1000.0
            beats_elapsed = seconds_since_beat / max(0.001, beat_period)
            bar_position = (beats_elapsed % 4.0) / 4.0
        else:
            beats_elapsed = elapsed / beat_period
            bar_position = (elapsed % bar_period) / bar_period

        # Beat-locked envelope: peaks at 1.0 right on the kick, decays
        # exponentially with ~180 ms half-life. Effects multiply this into
        # their brightness for music-locked accents.
        if last_beat_ms > 0:
            ms_since_beat = max(0.0, now * 1000.0 - last_beat_ms)
            kick_env = math.exp(-math.log(2.0) * (ms_since_beat / 1000.0) / 0.18)
            kick_env = max(0.0, min(1.0, kick_env))
        else:
            kick_env = 0.0

        return EffectContext(
            now=now,
            t0=self._active.start_ts,
            elapsed=elapsed,
            duration=self._active.duration_s,
            progress=min(1.0, max(0.0, progress)),
            beat_period_s=beat_period,
            beats_elapsed=beats_elapsed,
            bar_position=bar_position,
            scene=scene,
            bass_norm=0.0,
            mid_norm=0.0,
            treble_norm=0.0,
            global_hue=0.0,
            kick_env=kick_env,
            energy=0.0,
        )

    def apply(
        self,
        dev_id: str,
        caps: Dict[str, Any],
        topo_fixture: Any,
        writes: Dict[int, int],
        ctx: Optional[EffectContext],
    ) -> None:
        if self._active is None or ctx is None:
            return
        try:
            self._active.effect.transform(dev_id, caps, topo_fixture, writes, ctx)
        except Exception:
            # Never let a buggy effect take down the render loop.
            return

    def force_trigger(self, effect_name: str, now: float, beat_period: float = 0.5) -> bool:
        for effect in self._registry:
            if effect.name == effect_name:
                self._active = _ActiveEffect(effect, now, effect.default_duration_beats * beat_period)
                self.last_chosen = effect.name
                self.trigger_count += 1
                self.history.append((now, effect.name))
                return True
        return False

    def _activate(self, candidate: Effect, now: float, beat_period: float) -> None:
        params = self.effective_effect_params(candidate)
        duration = params["duration_beats"] * beat_period
        self._active = _ActiveEffect(candidate, now, duration)
        self.last_chosen = candidate.name
        self.trigger_count += 1
        self.history.append((now, candidate.name))
        if len(self.history) > 20:
            self.history.pop(0)

    def _eligible(self, effect: Effect, scene: str, bpm: float, bpm_confidence: float) -> bool:
        """Gate an effect by scene, mood filter, BPM window, and BPM stability."""
        if scene not in effect.eligible_scenes:
            return False
        # Mood filter is bypassed in SILENT — the filter is a "what should
        # play during music" preference, not "I'd rather have a black rig
        # between songs". In SILENT we always need a fallback ambient.
        if self._mood_filter and scene != "SILENT":
            if not any(tag in self._mood_filter for tag in effect.mood_tags):
                return False
        # BPM window: skip only once we actually have an estimate. A 0 BPM
        # means "unknown yet" and shouldn't lock everyone out at startup.
        if bpm >= 40.0:
            if bpm < effect.min_bpm or bpm > effect.max_bpm:
                return False
            narrow = (effect.max_bpm - effect.min_bpm) < 200.0
            if narrow and self._bpm_confidence_gate > 0 and bpm_confidence < self._bpm_confidence_gate:
                return False
        return True

    def _pick_for_scene(
        self,
        scene: str,
        now: float,
        beat_period: float,
        bpm: float = 0.0,
        bpm_confidence: float = 1.0,
        kind_bias: Optional[str] = None,
        allow_repeat: bool = False,
    ) -> Optional[Effect]:
        """Pick an effect for ``scene`` using kind-weighted probability.

        Each effect's final weight is::

            user_weight   *  effect.weight  *  scene_kind_weights[effect.kind]

        On top of that we tier candidates so cooled-down + non-repeating
        picks are preferred — but if the pool is exhausted (e.g. user only
        enabled one effect) we fall back to a softer tier so the rig stays
        animated rather than going dark.

        ``kind_bias`` (optional) multiplies the matching kind's weight by 4×
        — used on scene-up transitions to favour impact.
        """
        bar_period = beat_period * 4.0
        kind_weights = self._SCENE_KIND_WEIGHTS.get(scene, self._SCENE_KIND_WEIGHTS["VERSE"])

        all_eligible: List[Tuple[Effect, float, bool, bool]] = []
        for effect in self._registry:
            if not self._eligible(effect, scene, bpm, bpm_confidence):
                continue
            params = self.effective_effect_params(effect)
            if not params["enabled"] or params["weight"] <= 0.0:
                continue
            kind_w = kind_weights.get(effect.kind, 0.0)
            if kind_w <= 0.0:
                continue
            final_w = params["weight"] * effect.weight * kind_w
            if kind_bias is not None and effect.kind == kind_bias:
                final_w *= 4.0
            if final_w <= 0.0:
                continue
            last = self._last_trigger_ts.get(effect.name, -1e9)
            cd_ok = (now - last) >= params["cooldown_bars"] * bar_period
            is_repeat = self.last_chosen == effect.name
            all_eligible.append((effect, final_w, cd_ok, is_repeat))

        if not all_eligible:
            return None

        # Preference order: cooled-down + new > cooled-down > new > anything.
        # Only when ``allow_repeat`` is True do we collapse the "no-repeat"
        # constraint — used by force-pick paths.
        if allow_repeat:
            tiers = [
                [(e, w) for e, w, cd, _ in all_eligible if cd],
                [(e, w) for e, w, _, _ in all_eligible],
            ]
        else:
            tiers = [
                [(e, w) for e, w, cd, rep in all_eligible if cd and not rep],
                [(e, w) for e, w, cd, rep in all_eligible if cd],
                [(e, w) for e, w, cd, rep in all_eligible if not rep],
                [(e, w) for e, w, _, _ in all_eligible],
            ]
        pool = next((t for t in tiers if t), [])
        if not pool:
            return None

        total = sum(w for _, w in pool)
        if total <= 0.0:
            return None
        r = self._rng.random() * total
        acc = 0.0
        for effect, w in pool:
            acc += w
            if r <= acc:
                return effect
        return pool[-1][0]

    def force_trigger_with_bar(self, effect_name: str, now: float, beat_period: float) -> bool:
        """Force a named effect to fire now, using its configured duration."""
        for effect in self._registry:
            if effect.name != effect_name:
                continue
            params = self.effective_effect_params(effect)
            duration = params["duration_beats"] * beat_period
            self._active = _ActiveEffect(effect, now, duration)
            self.last_chosen = effect.name
            self.trigger_count += 1
            self.history.append((now, effect.name))
            if len(self.history) > 20:
                self.history.pop(0)
            return True
        return False
