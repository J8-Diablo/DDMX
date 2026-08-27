#!/usr/bin/env python3
"""
dmx_engine.py - DMX Render Engine

Thread-based engine that:
- Maintains device state (channels, effects, fades)
- Renders effects at 40Hz
- Sends ArtNet packets directly
- Handles identify mode
- Manages cue playback with fades
"""

import sys
import threading
import time
import math
import logging
import os
from typing import Dict, List, Any, Optional, Callable, Tuple
from dataclasses import dataclass, field
from copy import deepcopy

try:
    from DMXE import DMXEngine as ArtNetSender
except ImportError:
    ArtNetSender = None

try:
    import Effect as EffectModule
except ImportError:
    EffectModule = None
try:
    import intelligent_fx as IntelligentFX
except ImportError:
    IntelligentFX = None

log = logging.getLogger("DMXRenderEngine")
_engine_log_level = os.environ.get("DMX_ENGINE_LOG_LEVEL", "INFO").upper()
log.setLevel(getattr(logging, _engine_log_level, logging.INFO))

FIXTURE_SHARED_TARGET_SPECS: Dict[Tuple[str, str], Dict[str, Any]] = {
    ("dimmer", "level"): {"target_key": "family.dimmer.level", "aliases": ["dimmer"]},
    ("color", "red"): {"target_key": "family.color.red", "aliases": ["r"]},
    ("color", "green"): {"target_key": "family.color.green", "aliases": ["g"]},
    ("color", "blue"): {"target_key": "family.color.blue", "aliases": ["b"]},
    ("position", "pan"): {"target_key": "family.position.pan", "aliases": ["pan"]},
    ("position", "tilt"): {"target_key": "family.position.tilt", "aliases": ["tilt"]},
}

ROLE_TO_FAMILY: Dict[str, str] = {
    "level": "dimmer",
    "red": "color",
    "green": "color",
    "blue": "color",
    "pan": "position",
    "tilt": "position",
}

# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class LiveEffect:
    """An active effect on a channel"""
    effect_type: str
    amplitude: float = 100.0
    frequency: float = 1.0
    phase: Any = 0  # can be "0 > 200" for spread
    params: Dict[str, Any] = field(default_factory=dict)
    start_time: float = 0.0

@dataclass
class FadeState:
    """State of an active fade"""
    start_values: Dict[int, Dict[int, int]]  # {universe: {channel: value}}
    end_values: Dict[int, Dict[int, int]]
    start_time: float
    duration_ms: float
    schedule: Dict[str, tuple] = field(default_factory=dict)  # {device_id: (start_ms, end_ms)}

@dataclass
class DeviceState:
    """State of a single device"""
    device_id: str
    universe: int = 0
    base_address: int = 0
    channels: Dict[int, int] = field(default_factory=dict)  # {abs_channel: value}
    effects: Dict[int, List[LiveEffect]] = field(default_factory=dict)  # {channel: [effects]}
    attr_map: Dict[str, int] = field(default_factory=dict)  # {attr_key: abs_channel}
    x: Optional[float] = None
    y: Optional[float] = None
    fixture_template: str = ""
    cname: str = ""
    capabilities: Dict[str, Any] = field(default_factory=dict)
    # Per-fixture movement calibration (AutoLight "home / audience" position).
    # home_pan/home_tilt are DMX values (0-255) the fixture returns to when idle;
    # invert_pan/invert_tilt flip the axis for fixtures mounted upside-down or
    # oriented differently. None means "not calibrated" (engine leaves as-is).
    home_pan: Optional[int] = None
    home_tilt: Optional[int] = None
    invert_pan: bool = False
    invert_tilt: bool = False

def _classify_device_capabilities(attr_map: Dict[str, int], fixture_template: str) -> Dict[str, Any]:
    """Derive AutoLight-relevant capabilities from a device's channel map.

    Returns flags (``has_dimmer``, ``has_color``, ``has_movement``,
    ``strobe_friendly``) plus the raw channel indexes that the overlay will
    drive. Called once at rig registration; results are cached on the device.
    """
    dimmer_ch: Optional[int] = None
    red_ch: Optional[int] = None
    green_ch: Optional[int] = None
    blue_ch: Optional[int] = None
    pan_ch: Optional[int] = None
    tilt_ch: Optional[int] = None
    grouped: Dict[str, Dict[str, int]] = {}
    group_min: Dict[str, int] = {}

    for raw_key, raw_channel in (attr_map or {}).items():
        key = str(raw_key or "").strip().lower()
        if not key:
            continue
        try:
            channel = int(raw_channel)
        except Exception:
            continue
        if not (0 <= channel < 512):
            continue
        if "." in key:
            group_id, role = key.rsplit(".", 1)
        else:
            group_id, role = "_flat_", key
        if role in ("level", "dimmer"):
            canonical = "dimmer"
        elif role in ("red", "r"):
            canonical = "red"
        elif role in ("green", "g"):
            canonical = "green"
        elif role in ("blue", "b"):
            canonical = "blue"
        elif role == "pan":
            canonical = "pan"
        elif role == "tilt":
            canonical = "tilt"
        else:
            continue
        bucket = grouped.setdefault(group_id, {})
        bucket[canonical] = channel
        prev = group_min.get(group_id)
        if prev is None or channel < prev:
            group_min[group_id] = channel

    ordered_groups = sorted(grouped.keys(), key=lambda gid: (group_min.get(gid, 0), gid))
    picked: Dict[str, int] = {}
    for gid in ordered_groups:
        for canonical, channel in grouped[gid].items():
            picked.setdefault(canonical, channel)

    dimmer_ch = picked.get("dimmer")
    red_ch = picked.get("red")
    green_ch = picked.get("green")
    blue_ch = picked.get("blue")
    pan_ch = picked.get("pan")
    tilt_ch = picked.get("tilt")

    has_dimmer = dimmer_ch is not None
    has_color = all(c is not None for c in (red_ch, green_ch, blue_ch))
    has_movement = pan_ch is not None or tilt_ch is not None
    template_lower = (fixture_template or "").lower()
    strobe_friendly = (
        "strob" in template_lower
        or (has_dimmer and not has_color and not has_movement)
    )

    return {
        "has_dimmer": has_dimmer,
        "has_color": has_color,
        "has_movement": has_movement,
        "strobe_friendly": strobe_friendly,
        "dimmer_channel": dimmer_ch,
        "red_channel": red_ch,
        "green_channel": green_ch,
        "blue_channel": blue_ch,
        "pan_channel": pan_ch,
        "tilt_channel": tilt_ch,
    }


@dataclass
class PlaybackPlanEntry:
    plan_index: int
    cue_index: int
    cue_name: str
    cue_payload: Dict[str, Any]
    device_order: List[str]
    fade_ms: int
    sleep_ms: int          # dead time before this entry's fade: the previous
                           # cue's hold, or the sequence lead-in for the first
    wait_start_at_ms: int = 0
    wait_end_at_ms: int = 0
    fade_start_at_ms: int = 0
    fade_end_at_ms: int = 0
    hold_ms: int = 0       # how long THIS cue stays once its fade is done
    hold_cue_index: int = -1   # whose hold `sleep_ms` is (-1 = the lead-in)
    hold_cue_name: str = ""
    hold_only: bool = False    # trailing entry: hold the last cue, fade nothing


@dataclass
class TimelineBlock:
    plan_index: int
    cue_index: int
    cue_name: str
    lane: int
    start_ms: int
    length_ms: int
    end_ms: int
    fade_start_ms: int = 0
    fade_end_ms: int = 0
    # Premiere-style edge fades (durations from each edge, in ms). When > 0 they
    # take precedence over the legacy fade_start/fade_end ramp.
    fade_in_ms: int = 0
    fade_out_ms: int = 0
    fade_operator: str = ""
    cue_payload: Dict[str, Any] = field(default_factory=dict)
    device_order: List[str] = field(default_factory=list)

# ============================================================================
# DMX RENDER ENGINE
# ============================================================================

class DMXRenderEngine:
    """
    Main render engine that runs in a background thread.
    Calculates all DMX values including effects and fades,
    then sends to ArtNet.
    """

    TICK_HZ = 40  # 40 Hz = 25ms per tick

    def __init__(self, artnet_ip: str = "127.0.0.1", bind_ip: str = "0.0.0.0"):
        # ArtNet sender
        self.artnet: Optional[ArtNetSender] = None
        if ArtNetSender:
            try:
                self.artnet = ArtNetSender(target_ip=artnet_ip, bind_iface=bind_ip, broadcast=False)
                log.info(f"ArtNet initialized: {artnet_ip}")
            except Exception as e:
                log.error(f"ArtNet init failed: {e}")

        # State
        self._lock = threading.RLock()
        self._devices: Dict[str, DeviceState] = {}
        self._universes: Dict[int, List[int]] = {}  # {universe: [512 values]}

        # Direct channel values (raw low-level API, kept for integrations)
        self._direct_channels: Dict[int, Dict[int, int]] = {}  # {universe: {channel: value}}

        # Manual attribute layer — what the operator is holding on the console.
        # The UI never sends DMX channels: it sends {device, attr, value} and the
        # engine resolves the channel through that device's attr_map, so a
        # re-address or a fixture swap cannot leave a stale write behind.
        self._manual_attrs: Dict[str, Dict[str, int]] = {}  # {device_id: {attr_key: value}}

        # Optional AutoLight render-pipeline overlay. Callable invoked after the
        # base render pass with (universes, now_ts); may mutate values in place.
        self._autolight_overlay: Optional[Any] = None

        # Movement smoothing (pan/tilt) - channels provided by UI
        self._smooth_channels: Dict[int, set] = {}  # {universe: {channel}}
        self._smooth_targets: Dict[int, Dict[int, int]] = {}  # {universe: {channel: target}}
        self._smooth_step = int(self._read_env_float("DMX_SMOOTH_STEP", 2))
        self._smooth_predict = os.environ.get("DMX_SMOOTH_PREDICT", "0").strip().lower() in ("1", "true", "yes", "on")
        self._smooth_disabled = os.environ.get("DMX_SMOOTH_DISABLE", "0").strip().lower() in ("1", "true", "yes", "on")
        self._smooth_last_targets: Dict[int, Dict[int, int]] = {}

        # Dummy channels (keepalive for server mods)
        self._dummy_channels: Dict[int, List[int]] = {}  # {universe: [channels]}
        self._dummy_state: Dict[int, int] = {}  # {universe: 0/255}
        self._dummy_enabled = os.environ.get("DMX_DUMMY", "1").strip().lower() in ("1", "true", "yes", "on")

        # Live effects (from controller, not cues)

        # Live effect groups (legacy/intelligent)
        self._live_effect_groups: Dict[str, Dict[str, Any]] = {}
        self._live_groups_by_device: Dict[str, set] = {}

        # Identify mode
        self._identify_devices: List[str] = []
        self._identify_data: List[Dict[str, Any]] = []  # Direct channel info from JS
        self._identify_start: float = 0.0

        # Cue playback
        self._fade: Optional[FadeState] = None
        self._cue_effects: Dict[str, Dict[int, List[LiveEffect]]] = {}  # effects from current cue
        self._cue_effect_groups: Dict[str, Dict[str, Any]] = {}
        self._cue_groups_by_device: Dict[str, set] = {}
        self._fade_effect_groups: Optional[Dict[str, Any]] = None
        self._effect_epoch = time.perf_counter()

        # Sequence playback scheduler
        self._playback_thread: Optional[threading.Thread] = None
        self._playback_stop_event = threading.Event()
        self._playback_skip_requested = False
        self._playback_wait_adjust_ms = 0
        self._playback_live_state_backup: Optional[Dict[str, Any]] = None
        self._timeline_runtime: Optional[Dict[str, Any]] = None
        self._playback_clock_mode = os.environ.get("DMX_PLAYBACK_CLOCK_MODE", "timeline").strip().lower()
        self._playback_speed = 1.0
        self._playback_run_speed = 1.0
        # Whole-sequence looping (distinct from per-step loop groups):
        # loop_count None/0 = forever, otherwise that many passes.
        self._playback_loop = False
        self._playback_loop_count: Optional[int] = None
        self._playback_loop_pass = 0
        self._log_playback_timing = os.environ.get("DMX_LOG_PLAYBACK_TIMING", "0").strip().lower() in ("1", "true", "yes", "on")
        self._playback_state: Dict[str, Any] = {
            "active": False,
            "paused": False,
            "phase": "idle",
            "cue_index": -1,
            "plan_index": -1,
            "cue_name": "",
            "cue_token": 0,
            "phase_remaining_ms": 0,
            "wait_remaining_ms": 0,
            "wait_adjust_ms": 0,
            "sequence_length": 0,
            "speed": 1.0,
        }
        self._tick_hz = max(10.0, min(240.0, self._read_env_float("DMX_TICK_HZ", self.TICK_HZ)))
        # Idle (non-playback) engine rate. Defaults to 120 Hz so live edits on
        # multi-universe rigs are quantized to ~8 ms instead of 25 ms (40 Hz).
        self._idle_engine_hz = max(
            self._tick_hz,
            min(240.0, self._read_env_float("DMX_IDLE_ENGINE_HZ", 120.0)),
        )
        self._playback_engine_hz = max(
            self._tick_hz,
            min(240.0, self._read_env_float("DMX_PLAYBACK_ENGINE_HZ", max(self._tick_hz, 120.0))),
        )
        self._playback_ui_fps = max(1.0, min(60.0, self._read_env_float("DMX_PLAYBACK_UI_FPS", 12.0)))
        self._profile_runner = os.environ.get("DMX_PROFILE_RUNNER", "0").strip().lower() in ("1", "true", "yes", "on")
        self._perf_last_log = time.perf_counter()
        self._perf_stats: Dict[str, float] = {
            "render_frames": 0.0,
            "render_total_ms": 0.0,
            "render_max_ms": 0.0,
            "backend_frames": 0.0,
            "backend_total_ms": 0.0,
            "backend_max_ms": 0.0,
            "send_universes": 0.0,
            "send_total_ms": 0.0,
            "send_max_ms": 0.0,
            "state_pushes": 0.0,
            "state_push_total_ms": 0.0,
            "state_push_max_ms": 0.0,
        }
        self._zero_universe = [0] * 512

        # Render thread
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # State change callbacks for SSE
        self._state_callbacks: List[Callable] = []
        self._last_state_broadcast: float = 0.0

        # Debug flags
        self._log_dmx = os.environ.get("DMX_LOG_DMX", "0").strip().lower() in ("1", "true", "yes", "on")
        self._log_dmx_full = os.environ.get("DMX_LOG_DMX_FULL", "0").strip().lower() in ("1", "true", "yes", "on")

        # ------------------------------------------------------------------
        # Output stage. The engine behaves like a real DMX interface: a
        # dedicated thread re-emits every known universe at a fixed rate,
        # changed or not. The compute loop never sends — it publishes an
        # immutable snapshot per universe that the emitter picks up, so a slow
        # frame degrades how often the *values* refresh, never the stream.
        # ------------------------------------------------------------------
        self._emit_hz = max(1.0, min(1000.0, self._read_env_float("DMX_EMIT_HZ", 500.0)))
        self._emit_frames: Dict[int, bytes] = {}
        self._emit_thread: Optional[threading.Thread] = None
        self._emit_stats: Dict[str, float] = {"sweeps": 0.0, "packets": 0.0, "late": 0.0, "max_late_ms": 0.0}
        # Universe values pushed to the browser preview (diff + periodic keyframe).
        self._preview_hz = max(1.0, min(120.0, self._read_env_float("DMX_PREVIEW_HZ", 30.0)))
        self._preview_last: Dict[int, bytes] = {}
        self._preview_last_full: float = 0.0
        # Bumped whenever the effect-group pool or the device set changes, so the
        # per-group member resolution can be cached across frames.
        self._effect_generation = 0
        self._effect_runtime_cache: Dict[str, Dict[str, Any]] = {}
        self._effect_runtime_generation = -1

        # Send throttling + stats (to reduce unnecessary network spam)
        self._last_sent_universes: Dict[int, List[int]] = {}
        self._last_sent_time: Dict[int, float] = {}
        # Global gate timestamp for changed-universe sends. Throttling all
        # changed universes against a single timestamp keeps multi-universe
        # live edits in lock-step (no per-universe phase drift).
        self._last_global_send_time: float = 0.0
        self._artnet_diff = os.environ.get("DMX_ARTNET_DIFF", "0").strip().lower() in ("1", "true", "yes", "on")
        self._artnet_heartbeat_full = os.environ.get("DMX_ARTNET_HEARTBEAT_FULL", "1").strip().lower() in ("1", "true", "yes", "on")
        self._packet_count = 0
        self._last_send_ts = 0.0

        self._max_send_hz = self._read_env_float("DMX_MAX_SEND_HZ", self._tick_hz)
        self._min_send_interval = 0.0 if self._max_send_hz <= 0 else (1.0 / self._max_send_hz)

        # Keepalive to avoid fixtures timing out (send even if unchanged)
        self._heartbeat_sec = self._read_env_float("DMX_HEARTBEAT_SEC", 0.1)
        self._stats_interval_sec = self._read_env_float("DMX_STATS_SEC", 1.0)

        # Value filtering (reduce jitter)
        self._deadband = int(self._read_env_float("DMX_DEADBAND", 0))
        self._quantize = int(self._read_env_float("DMX_QUANTIZE", 1))

        self._stats_last_log = time.perf_counter()
        self._stats = {
            "frames_sent": 0,
            "universes_sent": 0,
            "frames_skipped": 0,
            "frames_published": 0,
            "bytes_sent": 0,
        }

        log.info(
            "DMXRenderEngine initialized (tick_hz=%s playback_engine_hz=%s max_send_hz=%s heartbeat_sec=%s stats_sec=%s)",
            self._tick_hz,
            self._playback_engine_hz,
            self._max_send_hz,
            self._heartbeat_sec,
            self._stats_interval_sec,
        )

    @staticmethod
    def _read_env_float(name: str, default: float) -> float:
        raw = os.environ.get(name)
        if raw is None:
            return float(default)
        try:
            return float(raw)
        except ValueError:
            return float(default)

    @staticmethod
    def _format_host_clock_ms(host_ms: int) -> str:
        total_ms = max(0, int(host_ms))
        secs, ms = divmod(total_ms, 1000)
        mins, sec = divmod(secs, 60)
        hours, minute = divmod(mins, 60)
        return f"{hours % 24:02d}:{minute:02d}:{sec:02d}.{ms:03d}"

    def _notify_state_change(self):
        """Push playback/control state changes without forcing a full universe snapshot."""
        try:
            self._broadcast_state(include_universes=False)
        except Exception:
            log.exception("State broadcast failed")

    def _playback_state_snapshot_locked(self) -> Dict[str, Any]:
        state = dict(self._playback_state)
        state["wait_adjust_ms"] = int(self._playback_wait_adjust_ms)
        state["clock_mode"] = str(self._playback_clock_mode or "timeline")
        state["speed"] = float(self._playback_run_speed if self._playback_state.get("active") else self._playback_speed)
        state["loop"] = bool(self._playback_loop)
        state["loop_count"] = int(self._playback_loop_count) if self._playback_loop_count else 0
        state["loop_pass"] = int(self._playback_loop_pass)
        if self._timeline_runtime is not None:
            state["timeline_elapsed_ms"] = int(self._timeline_elapsed_ms_locked())
            state["timeline_total_length_ms"] = int(self._timeline_runtime.get("total_length_ms", 0) or 0)
            state["timeline_priority_mode"] = self._normalize_timeline_priority_mode(self._timeline_runtime.get("priority_mode"))
        else:
            state["timeline_elapsed_ms"] = 0
            state["timeline_total_length_ms"] = 0
            state["timeline_priority_mode"] = ""
        server_time_ms = int(round(time.time() * 1000.0))
        state["server_time_ms"] = server_time_ms
        phase = str(state.get("phase") or "idle")
        remaining_ms = max(0, int(state.get("phase_remaining_ms", 0) or 0))
        if state.get("active") and not state.get("paused") and phase in ("waiting", "fading", "active") and remaining_ms > 0:
            state["phase_end_host_ms"] = server_time_ms + remaining_ms
        else:
            state["phase_end_host_ms"] = 0
        return state

    def _update_playback_state_locked(self, **updates):
        self._playback_state.update(updates)
        self._playback_state["wait_adjust_ms"] = int(self._playback_wait_adjust_ms)
        phase_remaining = int(self._playback_state.get("phase_remaining_ms", 0) or 0)
        phase = str(self._playback_state.get("phase") or "idle")
        self._playback_state["wait_remaining_ms"] = phase_remaining if phase == "waiting" else 0

    def _effective_state_broadcast_sec_locked(self) -> float:
        if self._playback_state.get("active"):
            fps = max(1.0, float(self._playback_ui_fps or 1.0))
            return max(1.0 / fps, 1.0 / 60.0)
        # Idle: the browser preview rate is a setting (dmx_runtime.preview_hz,
        # 30 Hz by default). The engine emits to the nodes at emit_hz (500 Hz)
        # regardless — this only governs how often the *preview* is refreshed.
        return 1.0 / max(1.0, float(self._preview_hz or 30.0))

    def _effective_tick_hz_locked(self) -> float:
        if self._playback_state.get("active"):
            return max(self._tick_hz, float(self._playback_engine_hz or self._tick_hz))
        idle_hz = float(self._idle_engine_hz or 0.0)
        if idle_hz > 0:
            return max(10.0, max(float(self._tick_hz or self.TICK_HZ), idle_hz))
        return max(10.0, float(self._tick_hz or self.TICK_HZ))

    def _effective_min_send_interval_locked(self) -> float:
        max_send_hz = float(self._max_send_hz or 0.0)
        if self._playback_state.get("active"):
            max_send_hz = max(max_send_hz, float(self._playback_engine_hz or 0.0))
        if max_send_hz <= 0:
            return 0.0
        return 1.0 / max_send_hz

    def _playback_poll_interval_sec(self) -> float:
        with self._lock:
            hz = self._effective_tick_hz_locked()
        return max(0.001, min(0.02, 0.5 / max(1.0, hz)))

    def _record_perf(self, key_prefix: str, elapsed_ms: float, count: float = 1.0) -> None:
        if not self._profile_runner:
            return
        if key_prefix == "state":
            total_key = "state_push_total_ms"
            max_key = "state_push_max_ms"
            count_key = "state_pushes"
        elif key_prefix in ("render", "backend"):
            total_key = f"{key_prefix}_total_ms"
            max_key = f"{key_prefix}_max_ms"
            count_key = f"{key_prefix}_frames"
        else:
            total_key = f"{key_prefix}_total_ms"
            max_key = f"{key_prefix}_max_ms"
            count_key = f"{key_prefix}_universes"
        self._perf_stats[count_key] = float(self._perf_stats.get(count_key, 0.0)) + float(count)
        self._perf_stats[total_key] = float(self._perf_stats.get(total_key, 0.0)) + float(elapsed_ms)
        self._perf_stats[max_key] = max(float(self._perf_stats.get(max_key, 0.0)), float(elapsed_ms))

    def _maybe_log_perf(self, now: float) -> None:
        if not self._profile_runner:
            return
        if (now - self._perf_last_log) < 1.0:
            return

        elapsed = max(0.001, now - self._perf_last_log)
        stats = self._perf_stats

        def _avg(total_key: str, count_key: str) -> float:
            count = max(1.0, float(stats.get(count_key, 0.0)))
            return float(stats.get(total_key, 0.0)) / count

        log.info(
            "[PERF] render avg=%.2fms max=%.2fms backend avg=%.2fms max=%.2fms send avg=%.2fms max=%.2fms pushes=%s push_avg=%.2fms push_max=%.2fms fps=%.1f",
            _avg("render_total_ms", "render_frames"),
            float(stats.get("render_max_ms", 0.0)),
            _avg("backend_total_ms", "backend_frames"),
            float(stats.get("backend_max_ms", 0.0)),
            _avg("send_total_ms", "send_universes"),
            float(stats.get("send_max_ms", 0.0)),
            int(stats.get("state_pushes", 0.0)),
            _avg("state_push_total_ms", "state_pushes"),
            float(stats.get("state_push_max_ms", 0.0)),
            float(stats.get("render_frames", 0.0)) / elapsed,
        )

        self._perf_last_log = now
        for key in self._perf_stats.keys():
            self._perf_stats[key] = 0.0

    def _prepare_playback_render_locked(self):
        if self._playback_live_state_backup is None:
            self._playback_live_state_backup = {
                "direct_channels": deepcopy(self._direct_channels),
                "manual_attrs": deepcopy(self._manual_attrs),
                "smooth_targets": deepcopy(self._smooth_targets),
                "smooth_last_targets": deepcopy(self._smooth_last_targets),
                "live_effect_groups": deepcopy(self._live_effect_groups),
                "live_groups_by_device": deepcopy(self._live_groups_by_device),
            }
        self._direct_channels.clear()
        self._manual_attrs.clear()
        self._smooth_targets.clear()
        self._smooth_last_targets.clear()
        self._live_effect_groups.clear()
        self._live_groups_by_device.clear()

    def _sync_live_backup_locked(self, *, replace_with=None, add=None, drop=None) -> None:
        """Apply a live-layer change to the pending playback snapshot too.

        A playback empties the live layer and puts the snapshot back when it
        ends, so the cue owns the rig meanwhile. Anything the operator changes
        during the cue -- Stop FX, deleting an effect, purging a group -- must
        reach that snapshot as well, otherwise the restore resurrects effects
        the UI has already forgotten and they keep being emitted with nothing on
        screen to explain them.

        The change is mirrored in kind, not by copying the live pool: while a
        cue plays that pool holds only what has been pushed since the cue
        started, so a wholesale copy would drop groups a targeted remove never
        asked to touch.
        """
        backup = self._playback_live_state_backup
        if backup is None:
            return
        if replace_with is not None:
            groups = deepcopy(replace_with)
        else:
            groups = backup.get("live_effect_groups") or {}
            if add:
                groups.update(deepcopy(add))
            for gid in (drop or ()):
                groups.pop(gid, None)
        backup["live_effect_groups"] = groups
        backup["live_groups_by_device"] = self._build_group_device_map(groups)

    def _restore_playback_render_locked(self):
        backup = self._playback_live_state_backup
        if backup is not None:
            self._direct_channels = backup.get("direct_channels", {})
            self._manual_attrs = backup.get("manual_attrs", {})
            self._smooth_targets = backup.get("smooth_targets", {})
            self._smooth_last_targets = backup.get("smooth_last_targets", {})
            self._live_effect_groups = backup.get("live_effect_groups", {})
            self._live_groups_by_device = backup.get("live_groups_by_device", {})
        self._playback_live_state_backup = None

    @staticmethod
    def _resolve_playback_start_index(sequence: List[Dict[str, Any]], start_index: int) -> int:
        if not sequence:
            return 0
        idx = max(0, min(int(start_index or 0), len(sequence) - 1))
        step = sequence[idx] if 0 <= idx < len(sequence) else None
        group_id = step.get("loopGroup") if isinstance(step, dict) else None
        if not group_id:
            return idx
        while idx - 1 >= 0:
            prev = sequence[idx - 1]
            if not isinstance(prev, dict) or prev.get("loopGroup") != group_id:
                break
            idx -= 1
        return idx

    @staticmethod
    def _normalize_timeline_priority_mode(priority_mode: Any) -> str:
        key = str(priority_mode or "top").strip().lower()
        return key if key in ("top", "bottom", "merge") else "top"

    @staticmethod
    def _normalize_timeline_operator(operator: Any) -> str:
        key = str(operator or "").strip()
        return key if key in ("|", "<", ">", "<>", "><", "||", "?") else ""

    def _normalize_timeline_blocks(self, blocks: Any) -> List[TimelineBlock]:
        if not isinstance(blocks, list):
            return []

        normalized: List[TimelineBlock] = []
        for raw in blocks:
            if not isinstance(raw, dict):
                continue
            cue_payload = raw.get("cue_payload") or raw.get("cue") or {}
            if not isinstance(cue_payload, dict):
                cue_payload = {}
            try:
                start_ms = max(0, int(raw.get("start_ms", 0) or 0))
                length_ms = max(1, int(raw.get("length_ms", 1) or 1))
                fade_start_ms = max(0, int(raw.get("fade_start_ms", 0) or 0))
                fade_end_ms = max(fade_start_ms, int(raw.get("fade_end_ms", 0) or 0))
                fade_in_ms = max(0, int(raw.get("fade_in_ms", 0) or 0))
                fade_out_ms = max(0, int(raw.get("fade_out_ms", 0) or 0))
                lane = max(0, int(raw.get("lane", 0) or 0))
                plan_index = int(raw.get("plan_index", len(normalized)) or 0)
                cue_index = int(raw.get("cue_index", -1) or -1)
            except Exception:
                continue

            device_order = raw.get("device_order") or cue_payload.get("device_order") or []
            if not isinstance(device_order, list):
                device_order = []

            normalized.append(
                TimelineBlock(
                    plan_index=plan_index,
                    cue_index=cue_index,
                    cue_name=str(raw.get("cue_name") or (f"Cue {cue_index + 1}" if cue_index >= 0 else "Cue")),
                    lane=lane,
                    start_ms=start_ms,
                    length_ms=length_ms,
                    end_ms=start_ms + length_ms,
                    fade_start_ms=min(length_ms, fade_start_ms),
                    fade_end_ms=min(length_ms, fade_end_ms),
                    fade_in_ms=min(length_ms, fade_in_ms),
                    fade_out_ms=min(length_ms, fade_out_ms),
                    fade_operator=self._normalize_timeline_operator(raw.get("fade_operator")),
                    cue_payload=cue_payload,
                    device_order=[str(x) for x in device_order],
                )
            )

        normalized.sort(key=lambda block: (block.start_ms, block.lane, block.plan_index))
        return normalized

    def _is_timeline_mode_active_locked(self) -> bool:
        return isinstance(self._timeline_runtime, dict) and bool(self._timeline_runtime.get("blocks"))

    def _timeline_elapsed_ms_locked(self, now: Optional[float] = None) -> int:
        runtime = self._timeline_runtime
        if not runtime:
            return 0
        anchor = float(runtime.get("anchor_time") or 0.0)
        base_offset_ms = float(runtime.get("base_offset_ms") or 0.0)
        current = float(now if now is not None else time.perf_counter())
        if self._playback_state.get("paused"):
            return max(0, int(round(base_offset_ms)))
        return max(0, int(round(base_offset_ms + max(0.0, current - anchor) * 1000.0)))

    @staticmethod
    def _timeline_attr_kind(attr_key: Optional[str]) -> str:
        key = str(attr_key or "").strip().lower()
        if not key:
            return "other"
        if key == "dimmer":
            return "dimmer"
        if key in ("red", "green", "blue", "white", "amber", "uv"):
            return "color"
        if "color" in key:
            return "color"
        return "other"

    def _attr_key_for_channel(self, dev: DeviceState, channel: int) -> str:
        for attr_key, abs_channel in dev.attr_map.items():
            try:
                if int(abs_channel) == int(channel):
                    return str(attr_key).strip().lower()
            except Exception:
                continue
        return ""

    def _timeline_block_fade_mix(self, block: TimelineBlock, elapsed_ms: int) -> float:
        local_ms = elapsed_ms - int(block.start_ms)
        length = max(1, int(block.length_ms))
        if local_ms < 0:
            return 0.0
        if local_ms >= length:
            return 0.0

        fade_in = max(0, int(getattr(block, "fade_in_ms", 0)))
        fade_out = max(0, int(getattr(block, "fade_out_ms", 0)))
        # Premiere-style edge fades take precedence when set.
        if fade_in > 0 or fade_out > 0:
            mix = 1.0
            if fade_in > 0 and local_ms < fade_in:
                mix = min(mix, float(local_ms) / float(fade_in))
            remaining = length - local_ms
            if fade_out > 0 and remaining < fade_out:
                mix = min(mix, float(remaining) / float(fade_out))
            return max(0.0, min(1.0, mix))

        # Legacy single-ramp model (fade_start -> fade_end).
        fade_start = max(0, int(block.fade_start_ms))
        fade_end = max(fade_start, int(block.fade_end_ms))
        if fade_end <= fade_start:
            return 1.0
        if local_ms <= fade_start:
            return 0.0
        if local_ms >= fade_end:
            return 1.0
        return max(0.0, min(1.0, float(local_ms - fade_start) / float(max(1, fade_end - fade_start))))

    def _timeline_active_blocks_locked(self, elapsed_ms: int) -> List[TimelineBlock]:
        runtime = self._timeline_runtime or {}
        out: List[TimelineBlock] = []
        for block in runtime.get("blocks", []):
            if int(block.start_ms) <= elapsed_ms < int(block.end_ms):
                out.append(block)
        return out

    def _timeline_next_blocks_locked(self, elapsed_ms: int) -> List[TimelineBlock]:
        runtime = self._timeline_runtime or {}
        blocks = [block for block in runtime.get("blocks", []) if int(block.start_ms) > elapsed_ms]
        if not blocks:
            return []
        next_start = min(int(block.start_ms) for block in blocks)
        return [block for block in blocks if int(block.start_ms) == next_start]

    def _timeline_sort_blocks_for_render(self, blocks: List[TimelineBlock], priority_mode: str) -> List[TimelineBlock]:
        if priority_mode == "bottom":
            return sorted(blocks, key=lambda block: (block.lane, block.start_ms, block.plan_index))
        if priority_mode == "top":
            return sorted(blocks, key=lambda block: (-block.lane, block.start_ms, block.plan_index))
        return sorted(blocks, key=lambda block: (block.start_ms, block.lane, block.plan_index))

    def _timeline_pick_focus_block_locked(self, blocks: List[TimelineBlock], priority_mode: str) -> Optional[TimelineBlock]:
        if not blocks:
            return None
        if priority_mode == "bottom":
            return sorted(blocks, key=lambda block: (-block.lane, block.start_ms, block.plan_index))[0]
        if priority_mode == "top":
            return sorted(blocks, key=lambda block: (block.lane, block.start_ms, block.plan_index))[0]
        return sorted(blocks, key=lambda block: (block.start_ms, block.lane, block.plan_index))[-1]

    def _resolve_effect_groups_for_step(self, step: Dict[str, Any], virtual_groups: Any) -> List[Dict[str, Any]]:
        mapping = step.get("device_groups") or {}
        if not isinstance(mapping, dict) or not isinstance(virtual_groups, dict):
            return []
        group_ids = set()
        for groups in mapping.values():
            if isinstance(groups, list):
                for gid in groups:
                    group_ids.add(str(gid))
        out: List[Dict[str, Any]] = []
        for gid in group_ids:
            raw = virtual_groups.get(gid)
            if raw is None:
                raw = virtual_groups.get(str(gid))
            if not isinstance(raw, dict):
                continue
            clone = deepcopy(raw)
            clone["id"] = str(clone.get("id") or gid)
            out.append(clone)
        return out

    # -------------------------------------------------------------------------
    # TIME MODEL
    # -------------------------------------------------------------------------
    # A cue occupies `fade` then `duration`: it crossfades toward its values
    # over `fade` (which may carry a per-device spread operator), then holds
    # them for `duration`. That is exactly one timeline block of
    # fade + duration, whose fade-in is `fade`.
    #
    # The older model said "wait `sleep`, then crossfade over `duration`", so a
    # look's on-stage time lived in the NEXT step's sleep and no block could
    # describe it. Files are converted on the way in.

    TIME_MODEL = 2

    @staticmethod
    def step_fade_field(step: Dict[str, Any]) -> Any:
        """The fade time of a step, spread operator included."""
        if not isinstance(step, dict):
            return "0"
        fade = step.get("fade")
        if fade is not None:
            return fade
        return step.get("duration", "0")

    @staticmethod
    def step_hold_ms(step: Dict[str, Any]) -> int:
        """How long the cue holds once its fade is done."""
        if not isinstance(step, dict):
            return 0
        if step.get("fade") is None:
            # A step with no `fade` is still v1: its hold lives elsewhere.
            return 0
        try:
            return max(0, int(float(step.get("duration", 0) or 0)))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def step_exit_hold_ms(step: Dict[str, Any]) -> Optional[int]:
        """The hold used on the LAST pass of a loop group, when it differs.

        None means "same as every other pass".
        """
        if not isinstance(step, dict):
            return None
        raw = step.get("exit_duration")
        if raw is None:
            return None
        try:
            return max(0, int(float(raw or 0)))
        except (TypeError, ValueError):
            return None

    @classmethod
    def is_time_model_v2(cls, sequence: Any) -> bool:
        """True when every step states its own fade."""
        if not isinstance(sequence, list) or not sequence:
            return True
        return all(
            isinstance(step, dict) and step.get("fade") is not None
            for step in sequence
            if isinstance(step, dict)
        )

    @classmethod
    def migrate_sequence_time_model(cls, sequence: Any) -> Tuple[List[Dict[str, Any]], int]:
        """Convert a v1 sequence to v2. Returns (sequence, lead_in_ms).

        The conversion is exact: the hold of step i is the old sleep of step
        i+1, and the first sleep becomes the sequence's lead-in. Nothing is
        rounded, nothing is guessed.
        """
        if not isinstance(sequence, list):
            return [], 0
        if cls.is_time_model_v2(sequence):
            return sequence, 0

        def _sleep_of(step: Any) -> int:
            if not isinstance(step, dict):
                return 0
            try:
                return max(0, int(float(step.get("sleep", 0) or 0)))
            except (TypeError, ValueError):
                return 0

        steps = [step for step in sequence if isinstance(step, dict)]
        lead_in_ms = _sleep_of(steps[0]) if steps else 0

        # The hold of a step is the wait that used to sit in front of whatever
        # plays next -- and inside a loop group, what plays after its last step
        # is its FIRST step, not the step following the group. Reading the file
        # order there would shorten every looped show.
        holds: List[int] = []
        exits: List[Optional[int]] = []
        for idx, step in enumerate(steps):
            group = step.get("loopGroup")
            nxt = steps[idx + 1] if idx + 1 < len(steps) else None
            if group and (nxt is None or nxt.get("loopGroup") != group):
                first = idx
                while first - 1 >= 0 and steps[first - 1].get("loopGroup") == group:
                    first -= 1
                loop_back = _sleep_of(steps[first])
                exit_wait = _sleep_of(nxt)
                holds.append(loop_back)
                # Only worth writing when the two really differ.
                exits.append(exit_wait if exit_wait != loop_back else None)
            else:
                holds.append(_sleep_of(nxt))
                exits.append(None)

        out: List[Dict[str, Any]] = []
        hold_iter = iter(holds)
        exit_iter = iter(exits)
        for raw in sequence:
            if not isinstance(raw, dict):
                out.append(raw)
                continue
            step = dict(raw)
            if step.get("fade") is None:
                step["fade"] = raw.get("duration", "0")
            step["duration"] = next(hold_iter, 0)
            exit_hold = next(exit_iter, None)
            if exit_hold is None:
                step.pop("exit_duration", None)
            else:
                step["exit_duration"] = exit_hold
            step.pop("sleep", None)
            out.append(step)
        return out, lead_in_ms

    def _build_cue_payload_from_step(self, step: Dict[str, Any], virtual_groups: Any) -> Tuple[Dict[str, Any], List[str]]:
        devices = step.get("devices") or {}
        order_raw = step.get("device_order")
        if isinstance(order_raw, list) and order_raw:
            device_order = [str(x) for x in order_raw]
        else:
            device_order = [str(x) for x in devices.keys()]
        payload = {
            "devices": devices if isinstance(devices, dict) else {},
            "fade": self.step_fade_field(step),
            "effect_groups": self._resolve_effect_groups_for_step(step, virtual_groups),
        }
        return payload, device_order

    def _expand_playback_sequence(
        self,
        sequence: List[Dict[str, Any]],
        virtual_groups: Any = None,
        speed: float = 1.0,
        lead_in_ms: int = 0,
    ) -> List[PlaybackPlanEntry]:
        out: List[PlaybackPlanEntry] = []
        if not isinstance(sequence, list):
            return out

        speed_factor = max(0.01, float(speed or 1.0))
        cursor_ms = 0
        # The dead time before a cue's fade is the previous cue's hold; before
        # the first one it is the sequence lead-in.
        pending_hold_ms = max(0, int(round(max(0, int(lead_in_ms or 0)) / speed_factor)))
        pending_hold_index = -1
        pending_hold_name = ""
        i = 0
        while i < len(sequence):
            step = sequence[i]
            if not isinstance(step, dict):
                i += 1
                continue

            group_id = step.get("loopGroup")
            if group_id:
                group_start = i
                group_end = i
                while group_end + 1 < len(sequence):
                    nxt = sequence[group_end + 1]
                    if not isinstance(nxt, dict) or nxt.get("loopGroup") != group_id:
                        break
                    group_end += 1
                loop_count = max(1, int(step.get("loopCount", 1) or 1))
                indices = [j for _ in range(loop_count) for j in range(group_start, group_end + 1)]
                # The very last occurrence is the one that leaves the group.
                exit_position = len(indices) - 1
                i = group_end + 1
            else:
                indices = [i]
                exit_position = -1
                i += 1

            for position, cue_index in enumerate(indices):
                cue_step = sequence[cue_index]
                cue_payload, device_order = self._build_cue_payload_from_step(cue_step, virtual_groups)
                schedule = self._compute_schedule(cue_payload.get("fade", "0"), device_order)
                fade_ms_raw = int(max((end for _, end in schedule.values()), default=0))
                fade_ms = max(0, int(round(fade_ms_raw / speed_factor)))
                hold_raw = self.step_hold_ms(cue_step)
                if position == exit_position:
                    exit_hold = self.step_exit_hold_ms(cue_step)
                    if exit_hold is not None:
                        hold_raw = exit_hold
                hold_ms = max(0, int(round(hold_raw / speed_factor)))
                sleep_ms = pending_hold_ms
                cue_name = str(cue_step.get("name") or f"Cue {cue_index + 1}")
                entry = PlaybackPlanEntry(
                    plan_index=len(out),
                    cue_index=cue_index,
                    cue_name=cue_name,
                    cue_payload=cue_payload,
                    device_order=device_order,
                    fade_ms=fade_ms,
                    sleep_ms=sleep_ms,
                    wait_start_at_ms=cursor_ms,
                    wait_end_at_ms=cursor_ms + sleep_ms,
                    fade_start_at_ms=cursor_ms + sleep_ms,
                    fade_end_at_ms=cursor_ms + sleep_ms + fade_ms,
                    hold_ms=hold_ms,
                    hold_cue_index=pending_hold_index,
                    hold_cue_name=pending_hold_name,
                )
                out.append(entry)
                cursor_ms = entry.fade_end_at_ms
                pending_hold_ms = hold_ms
                pending_hold_index = cue_index
                pending_hold_name = cue_name

        # The last cue holds too: without this the sequence would end on its
        # fade, cutting the look short and shortening every loop pass.
        if out and pending_hold_ms > 0:
            out.append(PlaybackPlanEntry(
                plan_index=len(out),
                cue_index=pending_hold_index,
                cue_name=pending_hold_name,
                cue_payload={},
                device_order=[],
                fade_ms=0,
                sleep_ms=pending_hold_ms,
                wait_start_at_ms=cursor_ms,
                wait_end_at_ms=cursor_ms + pending_hold_ms,
                fade_start_at_ms=cursor_ms + pending_hold_ms,
                fade_end_at_ms=cursor_ms + pending_hold_ms,
                hold_ms=0,
                hold_cue_index=pending_hold_index,
                hold_cue_name=pending_hold_name,
                hold_only=True,
            ))
        return out

    @staticmethod
    def _normalize_group(group: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(group, dict):
            return None
        out = dict(group)
        gid = str(out.get("id") or "").strip()
        if not gid:
            return None
        out["id"] = gid
        mode = str(out.get("mode") or "legacy").strip().lower()
        out["mode"] = "intelligent" if mode == "intelligent" else "legacy"
        out["selectionScope"] = "fixture_elements" if str(out.get("selectionScope") or "").strip().lower() == "fixture_elements" else "devices"

        dev_ids = out.get("deviceIds") or out.get("device_ids") or out.get("device_ids".lower()) or []
        if isinstance(dev_ids, list):
            out["deviceIds"] = [str(x) for x in dev_ids]
        else:
            out["deviceIds"] = []

        sel = out.get("selection_groups") or out.get("selectionGroups")
        if isinstance(sel, list):
            out["selection_groups"] = [
                [str(x) for x in grp] for grp in sel if isinstance(grp, list)
            ]
        return out

    @classmethod
    def _normalize_groups(cls, groups: Any) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        if not isinstance(groups, list):
            return out
        for g in groups:
            ng = cls._normalize_group(g)
            if not ng:
                continue
            out[ng["id"]] = ng
        return out

    @staticmethod
    def _build_group_device_map(groups: Dict[str, Dict[str, Any]]) -> Dict[str, set]:
        mapping: Dict[str, set] = {}
        for gid, group in groups.items():
            devs = group.get("deviceIds") or []
            if not isinstance(devs, list):
                continue
            for dev_id in devs:
                key = str(dev_id)
                mapping.setdefault(key, set()).add(gid)
        return mapping

    @staticmethod
    def _group_selection_scope(group: Dict[str, Any]) -> str:
        return "fixture_elements" if str(group.get("selectionScope") or "").strip().lower() == "fixture_elements" else "devices"

    @staticmethod
    def _group_target_key(group: Dict[str, Any]) -> str:
        return str(group.get("targetKey") or group.get("attrKey") or group.get("attr") or "").strip().lower()

    @staticmethod
    def _fixture_elements_for_attr_map(attr_map: Dict[str, int]) -> List[Dict[str, Any]]:
        if not isinstance(attr_map, dict) or not attr_map:
            return []

        group_defs: Dict[str, Dict[str, Any]] = {}
        for raw_key, raw_channel in attr_map.items():
            key = str(raw_key or "").strip().lower()
            if not key or key.startswith("family.") or key in ("dimmer", "r", "g", "b", "pan", "tilt"):
                continue
            if "." not in key:
                continue
            group_id, role = key.rsplit(".", 1)
            family = ROLE_TO_FAMILY.get(role)
            if not family:
                continue
            try:
                channel = int(raw_channel)
            except Exception:
                continue
            entry = group_defs.setdefault(group_id, {
                "family": family,
                "channels": {},
                "min_channel": channel,
            })
            entry["channels"][role] = channel
            entry["min_channel"] = min(int(entry.get("min_channel", channel)), channel)

        by_family: Dict[str, List[Dict[str, Any]]] = {}
        for group_id, entry in group_defs.items():
            family = str(entry.get("family") or "").strip().lower()
            if not family:
                continue
            item = {
                "group_id": group_id,
                "channels": dict(entry.get("channels") or {}),
                "min_channel": int(entry.get("min_channel") or 0),
            }
            by_family.setdefault(family, []).append(item)

        elements: List[Dict[str, Any]] = []
        for family, groups in by_family.items():
            groups.sort(key=lambda item: (int(item.get("min_channel") or 0), str(item.get("group_id") or "")))
            for idx, entry in enumerate(groups):
                while len(elements) <= idx:
                    elements.append({"targets": {}})
                element_targets = elements[idx]["targets"]
                for role, channel in dict(entry.get("channels") or {}).items():
                    spec = FIXTURE_SHARED_TARGET_SPECS.get((family, str(role)))
                    if not spec:
                        continue
                    element_targets[str(spec["target_key"])] = int(channel)
                    for alias in spec.get("aliases") or []:
                        element_targets[str(alias)] = int(channel)

        return [element for element in elements if isinstance(element.get("targets"), dict) and element["targets"]]

    def _effect_runtime_for_group(self, group: Dict[str, Any]) -> Dict[str, Any]:
        """Memoised member resolution, one entry per group per computed frame.

        _resolve_effect_members() walks the group's whole member list, and it
        used to be called once per *device* — quadratic, and it dominated the
        frame: 41 ms for a single group covering 311 devices. Devices and groups
        cannot change during a frame (the lock is held), so resolving once per
        frame is equivalent and ~300x cheaper on a big rig.
        """
        key = id(group)
        cached = self._effect_runtime_cache.get(key)
        if cached is not None:
            return cached
        runtime = self._resolve_effect_members(group)
        self._effect_runtime_cache[key] = runtime
        return runtime

    def _resolve_effect_members(self, group: Dict[str, Any]) -> Dict[str, Any]:
        scope = self._group_selection_scope(group)
        device_ids = [str(x) for x in (group.get("deviceIds") or [])]
        order: List[Dict[str, Any]] = []
        by_device: Dict[str, List[Dict[str, Any]]] = {}

        for device_id in device_ids:
            dev = self._devices.get(device_id)
            if not dev:
                continue
            if scope == "fixture_elements":
                element_defs = self._fixture_elements_for_attr_map(dev.attr_map)
                members = []
                for idx, element in enumerate(element_defs):
                    members.append({
                        "member_id": f"{device_id}::{idx + 1}",
                        "device_id": device_id,
                        "targets": dict(element.get("targets") or {}),
                    })
                if not members:
                    members = [{
                        "member_id": device_id,
                        "device_id": device_id,
                        "targets": dict(dev.attr_map or {}),
                    }]
            else:
                members = [{
                    "member_id": device_id,
                    "device_id": device_id,
                    "targets": dict(dev.attr_map or {}),
                }]

            device_members: List[Dict[str, Any]] = []
            for member in members:
                resolved = dict(member)
                resolved["index"] = len(order)
                order.append(resolved)
                device_members.append(resolved)
            by_device[device_id] = device_members

        runtime_group = dict(group)
        if scope == "fixture_elements":
            runtime_group["effect_member_ids"] = [str(entry.get("member_id") or "") for entry in order]

        return {
            "scope": scope,
            "order": order,
            "count": max(1, len(order)),
            "by_device": by_device,
            "runtime_group": runtime_group,
        }

    # -------------------------------------------------------------------------
    # THREAD CONTROL
    # -------------------------------------------------------------------------

    def start(self):
        """Start the render thread"""
        if self._running:
            return
        # On Windows, request 1 ms scheduler resolution so time.sleep()
        # in the render loop matches the requested tick interval (~1 ms jitter
        # instead of 10-15 ms default).
        self._win_timer_period_active = False
        if sys.platform == "win32":
            try:
                import ctypes
                if ctypes.windll.winmm.timeBeginPeriod(1) == 0:
                    self._win_timer_period_active = True
            except Exception as e:
                log.debug("timeBeginPeriod failed: %s", e)
        self._running = True
        self._thread = threading.Thread(target=self._render_loop, daemon=True)
        self._thread.start()
        self._emit_thread = threading.Thread(target=self._emit_loop, daemon=True, name="DMXEmitter")
        self._emit_thread.start()
        log.info("Render thread started (compute best-effort, emitter at %.0f Hz)", self._emit_hz)

    def stop(self):
        """Stop the render + emitter threads"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._emit_thread:
            self._emit_thread.join(timeout=1.0)
            self._emit_thread = None
        if getattr(self, "_win_timer_period_active", False):
            try:
                import ctypes
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass
            self._win_timer_period_active = False
        log.info("Render thread stopped")

    def set_artnet_target(self, ip: str) -> bool:
        """Update ArtNet target IP at runtime."""
        if not self.artnet or not ip:
            return False
        try:
            # DMXEngine stores target_ip attribute
            setattr(self.artnet, "target_ip", ip)
            log.info("ArtNet target updated: %s", ip)
            return True
        except Exception as e:
            log.error("Failed to update ArtNet target: %s", e)
            return False

    MAX_UNIVERSE = 32767  # Art-Net encodes the universe on 15 bits

    @classmethod
    def valid_universe(cls, raw: Any) -> Optional[int]:
        """Universe number an ArtDMX packet can actually carry, else None."""
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value if 0 <= value <= cls.MAX_UNIVERSE else None

    def _ensure_universe(self, universe: Any) -> bool:
        """Allocate the 512-slot buffer for a universe. False = out of range.

        Out-of-range numbers are refused *here* rather than at send time: one
        bogus value used to raise OverflowError inside the emit loop on every
        tick, taking the rest of the rig's output down with it.
        """
        uni = self.valid_universe(universe)
        if uni is None:
            log.warning("ignoring out-of-range universe %r (0-%d)", universe, self.MAX_UNIVERSE)
            return False
        if uni not in self._universes:
            self._universes[uni] = [0] * 512
        return True

    def set_playback_clock_mode(self, mode: str):
        with self._lock:
            key = str(mode or "").strip().lower()
            self._playback_clock_mode = "absolute_clock" if key == "absolute_clock" else "timeline"

    @staticmethod
    def _normalize_playback_speed(speed: Any) -> float:
        allowed = (0.25, 0.5, 1.0, 1.5, 2.0)
        try:
            raw = float(speed)
        except Exception:
            return 1.0
        closest = min(allowed, key=lambda val: abs(val - raw))
        return float(closest)

    def set_playback_ui_fps(self, fps: Any):
        with self._lock:
            try:
                value = float(fps)
            except Exception:
                value = 12.0
            self._playback_ui_fps = max(1.0, min(60.0, value))

    def set_playback_engine_hz(self, hz: Any):
        with self._lock:
            try:
                value = float(hz)
            except Exception:
                value = max(self._tick_hz, 120.0)
            self._playback_engine_hz = max(self._tick_hz, min(240.0, value))

    def set_idle_engine_hz(self, hz: Any):
        """Tick rate used when not in playback. Higher idle Hz reduces the
        quantization between live edits across multiple universes."""
        with self._lock:
            try:
                value = float(hz)
            except Exception:
                value = max(self._tick_hz, 120.0)
            self._idle_engine_hz = max(self._tick_hz, min(240.0, value))

    def set_profile_runner(self, enabled: Any):
        with self._lock:
            self._profile_runner = bool(enabled)

    # -------------------------------------------------------------------------
    # RENDER LOOP
    # -------------------------------------------------------------------------

    def _render_loop(self):
        """Main render loop running at TICK_HZ"""
        while self._running:
            tick_start = time.perf_counter()

            try:
                self._render_frame()
            except Exception as e:
                log.error(f"Render error: {e}")

            # Broadcast state every 100ms for UI
            now = time.perf_counter()
            with self._lock:
                broadcast_interval = self._effective_state_broadcast_sec_locked()
            if now - self._last_state_broadcast > broadcast_interval:
                self._broadcast_state(include_universes=True)
                self._last_state_broadcast = now

            # Sleep for remaining time
            elapsed = time.perf_counter() - tick_start
            with self._lock:
                tick_interval = 1.0 / max(1.0, self._effective_tick_hz_locked())
            sleep_time = tick_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    def _render_frame(self):
        """Compute one frame and publish it. Never sends — see _emit_loop()."""
        now = time.perf_counter()
        frame_start = now

        with self._lock:
            self._cleanup_finished_fade_locked(now)
            # The effect-group member resolution is memoised for this frame only.
            self._effect_runtime_cache.clear()

            # There is exactly one renderer: this one.
            backend_start = time.perf_counter()
            self._render_backend_frame(now)
            self._record_perf("backend", (time.perf_counter() - backend_start) * 1000.0)

            # Apply smoothing for movement channels (pan/tilt)
            if self._smooth_targets:
                self._apply_smoothing()

            # AutoLight overlay: audio-reactive values written on top of the
            # base render, but below the identify overlay.
            if self._autolight_overlay is not None:
                try:
                    self._autolight_overlay(self._universes, now)
                except Exception as exc:
                    log.debug("autolight overlay failed: %s", exc)

            # Apply identify overlay (highest priority, overrides everything)
            if self._identify_devices or self._identify_data:
                self._render_identify(now)

            # Publish an immutable snapshot per universe. The emitter thread
            # streams these at a fixed rate; the compute loop never touches the
            # socket, so a heavy frame can never stall the DMX output.
            published = 0
            for uni_num, values in self._universes.items():
                frame = bytes(values)
                if self._emit_frames.get(uni_num) != frame:
                    self._emit_frames[uni_num] = frame
                    published += 1
            self._stats["frames_published"] += published

        self._maybe_log_stats(now)
        self._record_perf("render", (time.perf_counter() - frame_start) * 1000.0)
        self._maybe_log_perf(now)

    def _emit_loop(self):
        """Stream every known universe at a fixed rate, like a DMX interface.

        Reads the snapshots published by the compute loop — no engine lock, so
        API writes and heavy frames can never delay the output. Pacing uses an
        absolute deadline so it does not drift; if a sweep runs long the missed
        slots are dropped rather than bursted.
        """
        next_send = time.perf_counter()
        while self._running:
            period = 1.0 / max(1.0, float(self._emit_hz or 1.0))
            frames = self._emit_frames  # atomic read of the current mapping
            if frames and self.artnet:
                sweep_start = time.perf_counter()
                packets = 0
                for uni_num, payload in list(frames.items()):
                    try:
                        # The dummy keepalive toggles per send, so it lives here.
                        if self._dummy_enabled and self._dummy_channels.get(uni_num):
                            payload = bytes(self._apply_dummy_overlay(uni_num, list(payload)))
                        self.artnet.send_universe(uni_num, payload)
                        packets += 1
                    except Exception as exc:
                        # A bad universe number must never kill the stream: drop
                        # it and keep every other universe alive.
                        log.warning("emit failed for universe %s (dropped): %s", uni_num, exc)
                        self._emit_frames.pop(uni_num, None)
                self._packet_count += packets
                self._last_send_ts = time.time()
                self._emit_stats["sweeps"] += 1.0
                self._emit_stats["packets"] += packets
                self._record_perf(
                    "send", (time.perf_counter() - sweep_start) * 1000.0, count=float(packets or 1)
                )

            next_send += period
            now = time.perf_counter()
            delay = next_send - now
            if delay > 0:
                time.sleep(delay)
            else:
                # Behind schedule: resync instead of accumulating a backlog.
                late_ms = -delay * 1000.0
                self._emit_stats["late"] += 1.0
                if late_ms > self._emit_stats["max_late_ms"]:
                    self._emit_stats["max_late_ms"] = late_ms
                next_send = now

    def set_emit_hz(self, hz: Any):
        """Output refresh rate in Hz — what the nodes actually see."""
        try:
            value = float(hz)
        except (TypeError, ValueError):
            return
        self._emit_hz = max(1.0, min(1000.0, value))

    def set_preview_hz(self, hz: Any):
        """Rate at which universe values are pushed to the browser preview."""
        try:
            value = float(hz)
        except (TypeError, ValueError):
            return
        self._preview_hz = max(1.0, min(120.0, value))

    def get_emit_stats(self) -> Dict[str, Any]:
        return {
            "emit_hz": float(self._emit_hz),
            "universes": len(self._emit_frames),
            "sweeps": int(self._emit_stats["sweeps"]),
            "packets": int(self._emit_stats["packets"]),
            "late_sweeps": int(self._emit_stats["late"]),
            "max_late_ms": round(float(self._emit_stats["max_late_ms"]), 2),
        }

    def _record_send_stats(self, universe: int, values: List[int], now: float):
        self._last_sent_universes[universe] = values[:]
        self._last_sent_time[universe] = now
        self._stats["frames_sent"] += 1
        self._stats["universes_sent"] += 1
        self._stats["bytes_sent"] += len(values)

    def _cleanup_finished_fade_locked(self, now: float):
        fade = self._fade
        if not fade:
            return
        if (now - fade.start_time) * 1000.0 < fade.duration_ms:
            return
        self._fade = None
        self._fade_effect_groups = None

    def _maybe_log_stats(self, now: float):
        if self._stats_interval_sec <= 0:
            return
        if (now - self._stats_last_log) < self._stats_interval_sec:
            return

        # Avoid log spam when nothing is happening
        if (
            self._stats["frames_sent"] == 0
            and self._stats["frames_skipped"] == 0
            and self._stats["universes_sent"] == 0
        ):
            self._stats_last_log = now
            self._stats = {
                "frames_sent": 0,
                "universes_sent": 0,
                "frames_skipped": 0,
                "frames_published": 0,
                "bytes_sent": 0,
            }
            return

        elapsed = max(0.001, now - self._stats_last_log)
        fps = self._stats["frames_sent"] / elapsed
        kbps = (self._stats["bytes_sent"] / 1024.0) / elapsed

        log.debug(
            "DMX stats: frames_sent=%s frames_skipped=%s universes_sent=%s fps=%.1f kbps=%.1f",
            self._stats["frames_sent"],
            self._stats["frames_skipped"],
            self._stats["universes_sent"],
            fps,
            kbps,
        )

        self._stats_last_log = now
        self._stats = {
            "frames_sent": 0,
            "universes_sent": 0,
            "frames_skipped": 0,
            "bytes_sent": 0,
        }

    def _send_artnet_diff(self, universe: int, values: List[int], heartbeat_due: bool) -> bool:
        last = self._last_sent_universes.get(universe)
        if last is None or (heartbeat_due and self._artnet_heartbeat_full):
            self.artnet.send_universe(universe, values)
            return True

        diff = {}
        for i, v in enumerate(values):
            if last[i] != v:
                diff[i] = v
        if not diff:
            return False

        # send only changed channels
        self.artnet.send_channels(universe, diff)
        return True

    def _filter_value(self, universe: int, channel: int, value: int) -> Optional[int]:
        """Apply quantize + deadband to reduce jitter. Returns None if ignored."""
        try:
            v = int(value)
        except Exception:
            return None
        v = max(0, min(255, v))

        if self._quantize and self._quantize > 1:
            v = int(round(v / self._quantize) * self._quantize)
            v = max(0, min(255, v))

        current = self._direct_channels.get(universe, {})
        prev = current.get(channel)
        if prev is not None and self._deadband and abs(v - prev) <= self._deadband:
            return None

        return v

    def _predict_target(self, universe: int, channel: int, target: int) -> int:
        last = self._smooth_last_targets.get(universe, {}).get(channel)
        if last is None:
            return target
        delta = target - last
        if delta == 0:
            return target
        cap = max(1, int(self._smooth_step) * 2)
        if delta > cap:
            delta = cap
        elif delta < -cap:
            delta = -cap
        predicted = target + delta
        return max(0, min(255, int(predicted)))

    def _release_overrides_for_cue_locked(self, written: Dict[str, set]) -> int:
        """Hand the channels a cue addresses back to the cue.

        The manual layer is applied ON TOP of the cue base so that a fader held
        on the desk wins -- otherwise the next frame would snap it back. But
        loading a cue is the operator's newest instruction, so what it addresses
        stops being held by hand. Without this, a device that was touched once
        (or merely selected, which pushes its values as intents) ignored every
        cue for the rest of the session: the blackout cue left it lit, and the
        next look left it dark.

        Only the channels the cue actually writes are released: a pan held by
        hand survives a cue that speaks about dimmers, and devices the cue does
        not address are left alone.
        """
        released = 0
        for dev_id, channels in written.items():
            if not channels:
                continue
            dev = self._devices.get(dev_id)
            if dev is None:
                continue

            held = self._manual_attrs.get(dev_id)
            if held:
                attr_map = dev.attr_map or {}
                for attr_key in list(held.keys()):
                    ch = attr_map.get(attr_key)
                    if ch is None:
                        continue
                    try:
                        ch = int(ch)
                    except (TypeError, ValueError):
                        continue
                    if ch in channels:
                        held.pop(attr_key, None)
                        released += 1
                if not held:
                    self._manual_attrs.pop(dev_id, None)

            # Raw channel overrides on the same channels go the same way.
            direct = self._direct_channels.get(int(dev.universe or 0))
            if direct:
                for ch in list(direct.keys()):
                    if ch in channels:
                        direct.pop(ch, None)
                        released += 1
                if not direct:
                    self._direct_channels.pop(int(dev.universe or 0), None)
        return released

    def _build_base_universe_map(self, now: float, apply_fade: bool = True) -> Dict[int, Dict[int, int]]:
        """Build base universe map from device channels (+ direct overrides)."""
        out: Dict[int, Dict[int, int]] = {}

        for dev_id, dev in self._devices.items():
            u = int(dev.universe or 0)
            if u not in out:
                out[u] = {}
            for ch, base_val in dev.channels.items():
                if not (0 <= ch < 512):
                    continue
                val = base_val
                if apply_fade and self._fade:
                    val = self._apply_fade(u, ch, base_val, now, dev_id)
                out[u][ch] = int(val)

        # Direct channels (raw low-level overrides)
        for u, ch_map in self._direct_channels.items():
            if u not in out:
                out[u] = {}
            for ch, val in ch_map.items():
                if 0 <= ch < 512:
                    out[u][ch] = int(val)

        # Manual attribute layer: the operator's console position wins over the
        # cue base, exactly like a fader held on a real desk.
        for dev_id, attrs in self._manual_attrs.items():
            dev = self._devices.get(dev_id)
            if not dev or not attrs:
                continue
            u = int(dev.universe or 0)
            if u not in out:
                out[u] = {}
            attr_map = dev.attr_map or {}
            for attr_key, val in attrs.items():
                ch = attr_map.get(attr_key)
                if ch is None or not (0 <= int(ch) < 512):
                    continue
                out[u][int(ch)] = max(0, min(255, int(val)))

        return out

    def _render_backend_frame(self, now: float):
        """Backend-render mode: compute base + effects per device."""
        if self._is_timeline_mode_active_locked():
            self._render_timeline_frame(now)
            return

        base_map = self._build_base_universe_map(now, apply_fade=True)

        # Reuse universe buffers instead of reallocating a new map every tick.
        target_universes = set(self._universes.keys()) | set(base_map.keys())
        for uni_num in target_universes:
            if not self._ensure_universe(uni_num):
                continue
            self._universes[uni_num][:] = self._zero_universe

        for uni_num, ch_map in base_map.items():
            uni = self._universes.get(uni_num)
            if uni is None:
                continue
            for ch, val in ch_map.items():
                if 0 <= ch < 512:
                    uni[ch] = int(val)

        # Apply effect groups and legacy per-channel effects
        if self._live_effect_groups or self._cue_effect_groups or self._cue_effects:
            speed = self._playback_run_speed if self._playback_state.get("active") else 1.0
            t_ms = (now - self._effect_epoch) * 1000.0 * max(0.01, float(speed or 1.0))
            device_ids = list(self._devices.keys())
            for dev_id, dev in self._devices.items():
                self._apply_effects_for_device(dev, dev_id, t_ms, device_ids, now)

    def _render_timeline_frame(self, now: float):
        runtime = self._timeline_runtime or {}
        blocks: List[TimelineBlock] = list(runtime.get("blocks") or [])
        priority_mode = self._normalize_timeline_priority_mode(runtime.get("priority_mode"))
        elapsed_ms = self._timeline_elapsed_ms_locked(now)
        active_blocks = self._timeline_active_blocks_locked(elapsed_ms)
        render_blocks = self._timeline_sort_blocks_for_render(active_blocks, priority_mode)

        active_devices = set()
        for block in active_blocks:
            devices = block.cue_payload.get("devices") or {}
            if isinstance(devices, dict):
                active_devices.update(str(dev_id) for dev_id in devices.keys())

        prep_blocks: Dict[str, TimelineBlock] = {}
        for block in blocks:
            if block.start_ms <= elapsed_ms:
                continue
            devices = block.cue_payload.get("devices") or {}
            if not isinstance(devices, dict):
                continue
            for dev_id in devices.keys():
                dev_key = str(dev_id)
                if dev_key in active_devices or dev_key in prep_blocks:
                    continue
                prep_blocks[dev_key] = block

        target_universes = set(self._universes.keys())
        for block in active_blocks:
            devices = block.cue_payload.get("devices") or {}
            if isinstance(devices, dict):
                for dev_spec in devices.values():
                    if isinstance(dev_spec, dict):
                        channels = dev_spec.get("channels") or {}
                        try:
                            target_universes.add(int(channels.get("Universe", 0) or 0))
                        except Exception:
                            continue
        for block in prep_blocks.values():
            devices = block.cue_payload.get("devices") or {}
            if isinstance(devices, dict):
                for dev_spec in devices.values():
                    if isinstance(dev_spec, dict):
                        channels = dev_spec.get("channels") or {}
                        try:
                            target_universes.add(int(channels.get("Universe", 0) or 0))
                        except Exception:
                            continue

        for uni_num in target_universes:
            if not self._ensure_universe(uni_num):
                continue
            self._universes[uni_num][:] = self._zero_universe

        for dev_id, block in prep_blocks.items():
            self._apply_timeline_prep_block(dev_id, block)

        for block in render_blocks:
            self._apply_timeline_block(block, elapsed_ms, now)

    def _compute_group_mix_for_device(self, dev_id: str, now: float) -> Dict[str, float]:
        """Compute per-group mix for cue groups during fades."""
        fade = self._fade
        fe = self._fade_effect_groups
        if not fade or not fe:
            return {}

        if dev_id in fade.schedule:
            start_ms, end_ms = fade.schedule[dev_id]
        else:
            start_ms, end_ms = 0, fade.duration_ms

        speed = self._playback_run_speed if self._playback_state.get("active") else 1.0
        elapsed_ms = (now - fade.start_time) * 1000 * max(0.01, float(speed or 1.0))
        if elapsed_ms <= start_ms:
            progress = 0.0
        elif elapsed_ms >= end_ms:
            progress = 1.0
        else:
            denom = max(1.0, float(end_ms - start_ms))
            progress = (elapsed_ms - start_ms) / denom

        prev = fe.get("prev", {}).get(dev_id, set())
        nxt = fe.get("next", {}).get(dev_id, set())
        union = fe.get("union", {}).get(dev_id, set())

        mix: Dict[str, float] = {}
        for gid in union:
            if gid in prev and gid in nxt:
                mix[gid] = 1.0
            elif gid in prev and gid not in nxt:
                mix[gid] = max(0.0, 1.0 - progress)
            elif gid not in prev and gid in nxt:
                mix[gid] = max(0.0, min(1.0, progress))
        return mix

    def _eval_legacy_group_delta(self, group: Dict[str, Any], t_ms: float, dev_id: str, dev_idx: int, dev_count: int) -> float:
        """Evaluate legacy group and return delta to add."""
        amp = float(group.get("amplitude", 0) or 0)
        typ = str(group.get("type") or "").lower()

        # Chaser returns 0-1 "on"
        if typ == "chaser" and IntelligentFX:
            ctx = {
                "device_index": dev_idx,
                "device_count": dev_count,
                "device_id": dev_id,
                "t_ms": t_ms,
                "group": group,
            }
            on = IntelligentFX.chaser_edge_fade(ctx, group)
            return amp * on

        freq = max(0.0, float(group.get("frequency", 0) or 0))
        phase_ms = IntelligentFX.phase_offset_ms(group, dev_id) if IntelligentFX else 0.0
        if freq <= 0:
            y = 0.0
        else:
            w = ((t_ms + phase_ms) / 1000.0) * freq
            frac = w - math.floor(w)
            if typ in ("sinus", "cardinalsinus"):
                y = math.sin(2 * math.pi * frac)
            elif typ == "triangle":
                y = IntelligentFX.tri_wave(frac) if IntelligentFX else (frac * 4 - 1 if frac < 0.5 else 3 - frac * 4)
            elif typ == "sawtooth":
                y = IntelligentFX.saw_wave(frac) if IntelligentFX else (frac * 2 - 1)
            elif typ in ("rectangle", "trapezoid"):
                y = IntelligentFX.sqr_wave(frac) if IntelligentFX else (1 if frac < 0.5 else -1)
            elif typ == "bump":
                y = (1 - frac / 0.1) if frac < 0.1 else 0.0
            else:
                y = math.sin(2 * math.pi * frac)

        delta = (amp / 100.0) * 127.5
        return delta * y

    def _apply_effect_group_to_device(
        self,
        group: Dict[str, Any],
        dev: DeviceState,
        dev_id: str,
        uni: List[int],
        t_ms: float,
        mix: float,
    ) -> None:
        if not group:
            return

        runtime = self._effect_runtime_for_group(group)
        members = runtime.get("by_device", {}).get(dev_id) or []
        if not members:
            return

        runtime_group = runtime.get("runtime_group") or group
        mode = str(group.get("mode") or "legacy").strip().lower()

        if mode == "intelligent" and IntelligentFX:
            defn = IntelligentFX.get_effect_def(group.get("type"))
            if not defn:
                return
            targets = IntelligentFX.normalize_targets(group.get("targets") or defn.get("targets"))
            if not targets or mix <= 0:
                return
            for member in members:
                member_id = str(member.get("member_id") or dev_id)
                member_idx = int(member.get("index") or 0)
                member_targets = dict(member.get("targets") or {})
                for target in targets:
                    channel = member_targets.get(str(target).lower())
                    if channel is None or not (0 <= int(channel) < 512):
                        continue
                    base_val = uni[int(channel)]
                    ctx = {
                        "params": runtime_group,
                        "group": runtime_group,
                        "t_ms": t_ms,
                        "device_index": member_idx,
                        "device_count": int(runtime.get("count") or 1),
                        "device_id": member_id,
                        "target": target,
                        "effect": defn,
                    }
                    raw = IntelligentFX.eval_effect(defn["id"], ctx)
                    uni[int(channel)] = IntelligentFX.apply_effect_value(defn, base_val, raw, scale=mix)
            return

        target_key = self._group_target_key(group)
        if not target_key or mix <= 0:
            return

        scaled_group = dict(runtime_group)
        amp = float(group.get("amplitude", 0) or 0)
        scaled_group["amplitude"] = amp * mix

        for member in members:
            member_targets = dict(member.get("targets") or {})
            channel = member_targets.get(target_key)
            if channel is None or not (0 <= int(channel) < 512):
                continue
            delta = self._eval_legacy_group_delta(
                scaled_group,
                t_ms,
                str(member.get("member_id") or dev_id),
                int(member.get("index") or 0),
                int(runtime.get("count") or 1),
            )
            uni[int(channel)] = max(0, min(255, int(round(uni[int(channel)] + delta))))

    def _apply_effects_for_device(self, dev: DeviceState, dev_id: str, t_ms: float, device_ids: List[str], now: float):
        """Apply cue/live groups + legacy per-channel effects for a device."""
        uni = self._universes.get(dev.universe)
        if uni is None:
            return

        dev_idx = device_ids.index(dev_id) if dev_id in device_ids else 0
        dev_count = len(device_ids)

        # Group-based effects (cue + live)
        group_mix = self._compute_group_mix_for_device(dev_id, now)

        # Cue groups: union during fade, otherwise current cue groups
        if self._fade_effect_groups:
            cue_group_ids = self._fade_effect_groups.get("union", {}).get(dev_id, set())
            group_pool = self._fade_effect_groups.get("pool", {}) or {}
        else:
            cue_group_ids = self._cue_groups_by_device.get(dev_id, set())
            group_pool = self._cue_effect_groups

        live_group_ids = self._live_groups_by_device.get(dev_id, set())

        def apply_group(gid: str, group: Dict[str, Any], mix: float):
            if not group:
                return
            self._apply_effect_group_to_device(group, dev, dev_id, uni, t_ms, mix)

        for gid in cue_group_ids:
            group = group_pool.get(gid)
            mix = group_mix.get(gid, 1.0) if group_mix else 1.0
            apply_group(gid, group, mix)

        for gid in live_group_ids:
            group = self._live_effect_groups.get(gid)
            if group:
                apply_group(gid, group, 1.0)

        # Legacy per-channel effects (old API)
        if dev_id in self._cue_effects:
            for ch, eff_list in self._cue_effects[dev_id].items():
                for eff in eff_list:
                    offset = self._eval_effect(eff, now, dev_idx, dev_count)
                    if 0 <= ch < 512:
                        uni[ch] = max(0, min(255, int(uni[ch] + offset * 255)))

    def _apply_timeline_prep_block(self, dev_id: str, block: TimelineBlock) -> None:
        devices = block.cue_payload.get("devices") or {}
        dev_spec = devices.get(dev_id)
        if not isinstance(dev_spec, dict):
            return

        channels = dev_spec.get("channels") or {}
        try:
            universe = int(channels.get("Universe", 0) or 0)
        except Exception:
            universe = 0
        if not self._ensure_universe(universe):
            return
        uni = self._universes[universe]
        dev_state = self._devices.get(str(dev_id))

        for ch_str, raw_val in channels.items():
            if str(ch_str).lower() == "universe":
                continue
            try:
                channel = int(ch_str)
                value = max(0, min(255, int(raw_val)))
            except Exception:
                continue
            attr_key = self._attr_key_for_channel(dev_state, channel) if dev_state else ""
            if self._timeline_attr_kind(attr_key) == "other" and 0 <= channel < 512:
                uni[channel] = value

    def _apply_timeline_block_effect_groups(
        self,
        block: TimelineBlock,
        dev_state: DeviceState,
        dev_id: str,
        elapsed_ms: int,
        now: float,
        fade_mix: float,
    ) -> None:
        if fade_mix <= 0:
            return
        uni = self._universes.get(dev_state.universe)
        if uni is None:
            return

        group_defs = block.cue_payload.get("effect_groups") or []
        if not isinstance(group_defs, list):
            group_defs = []

        local_ms = max(0.0, float(elapsed_ms - block.start_ms))
        speed = self._playback_run_speed if self._playback_state.get("active") else 1.0
        t_ms = local_ms * max(0.01, float(speed or 1.0))

        for group in group_defs:
            if not isinstance(group, dict):
                continue
            targets_devices = group.get("deviceIds") or group.get("device_ids") or []
            if isinstance(targets_devices, list) and targets_devices:
                if str(dev_id) not in {str(x) for x in targets_devices}:
                    continue
            self._apply_effect_group_to_device(group, dev_state, dev_id, uni, t_ms, fade_mix)

    def _apply_timeline_block_channel_effects(
        self,
        block: TimelineBlock,
        dev_state: DeviceState,
        dev_id: str,
        dev_spec: Dict[str, Any],
        elapsed_ms: int,
        now: float,
        fade_mix: float,
    ) -> None:
        if fade_mix <= 0 or not EffectModule:
            return
        effects_raw = dev_spec.get("effects") or {}
        if not isinstance(effects_raw, dict):
            return

        uni = self._universes.get(dev_state.universe)
        if uni is None:
            return

        device_ids = [str(x) for x in (block.device_order or list((block.cue_payload.get("devices") or {}).keys()))]
        dev_count = max(1, len(device_ids))
        dev_idx = device_ids.index(dev_id) if dev_id in device_ids else 0
        speed = self._playback_run_speed if self._playback_state.get("active") else 1.0
        t_s = (max(0.0, float(elapsed_ms - block.start_ms)) / 1000.0) * max(0.01, float(speed or 1.0))

        for ch_str, effect_specs in effects_raw.items():
            try:
                channel = int(ch_str)
            except Exception:
                continue
            if not (0 <= channel < 512):
                continue
            if not isinstance(effect_specs, list):
                effect_specs = [effect_specs]
            for effect_spec in effect_specs:
                if not isinstance(effect_spec, dict):
                    continue
                effect_dict = {
                    "type": effect_spec.get("type"),
                    "amplitude": float(effect_spec.get("amplitude", 100) or 100),
                    "frequency": float(effect_spec.get("frequency", 1) or 1),
                    "phase": effect_spec.get("phase", 0),
                    **{k: v for k, v in effect_spec.items() if k not in ("type", "amplitude", "frequency", "phase")},
                }
                offset = EffectModule.eval_effects(effect_dict, t_s, idx=dev_idx, count=dev_count)
                uni[channel] = max(0, min(255, int(round(uni[channel] + (offset * 255.0 * fade_mix)))))

    def _apply_timeline_block(self, block: TimelineBlock, elapsed_ms: int, now: float) -> None:
        devices = block.cue_payload.get("devices") or {}
        if not isinstance(devices, dict):
            return

        fade_mix = self._timeline_block_fade_mix(block, elapsed_ms)
        for dev_id, dev_spec in devices.items():
            if not isinstance(dev_spec, dict):
                continue
            channels = dev_spec.get("channels") or {}
            try:
                universe = int(channels.get("Universe", 0) or 0)
            except Exception:
                universe = 0
            if not self._ensure_universe(universe):
                continue
            uni = self._universes[universe]

            dev_key = str(dev_id)
            dev_state = self._devices.get(dev_key)
            if dev_state is None:
                dev_state = DeviceState(device_id=dev_key, universe=universe)
                self._devices[dev_key] = dev_state

            for ch_str, raw_val in channels.items():
                if str(ch_str).lower() == "universe":
                    continue
                try:
                    channel = int(ch_str)
                    target_val = max(0, min(255, int(raw_val)))
                except Exception:
                    continue
                if not (0 <= channel < 512):
                    continue
                attr_key = self._attr_key_for_channel(dev_state, channel)
                attr_kind = self._timeline_attr_kind(attr_key)
                if attr_kind in ("dimmer", "color"):
                    uni[channel] = max(0, min(255, int(round(float(target_val) * float(fade_mix)))))
                else:
                    uni[channel] = target_val

            self._apply_timeline_block_effect_groups(block, dev_state, dev_key, elapsed_ms, now, fade_mix)
            self._apply_timeline_block_channel_effects(block, dev_state, dev_key, dev_spec, elapsed_ms, now, fade_mix)

    def _apply_fade(self, universe: int, channel: int, base: int, now: float, dev_id: str) -> int:
        """Apply fade interpolation"""
        fade = self._fade
        if not fade:
            return base

        speed = self._playback_run_speed if self._playback_state.get("active") else 1.0
        elapsed_ms = (now - fade.start_time) * 1000 * max(0.01, float(speed or 1.0))

        # Per-device schedule
        if dev_id in fade.schedule:
            start_ms, end_ms = fade.schedule[dev_id]
        else:
            start_ms, end_ms = 0, fade.duration_ms

        if elapsed_ms < start_ms:
            # Not started yet
            start_val = fade.start_values.get(universe, {}).get(channel, base)
            return start_val
        elif elapsed_ms >= end_ms:
            # Finished
            return base
        else:
            # Interpolate
            if end_ms <= start_ms:
                return base
            progress = (elapsed_ms - start_ms) / (end_ms - start_ms)
            start_val = fade.start_values.get(universe, {}).get(channel, 0)
            return int(start_val + (base - start_val) * progress)

    def _eval_effect(self, eff: LiveEffect, now: float, idx: int, count: int) -> float:
        """Evaluate an effect, returns offset in [-1, 1]"""
        if not EffectModule:
            return 0.0

        speed = self._playback_run_speed if self._playback_state.get("active") else 1.0
        t_s = (now - eff.start_time) * max(0.01, float(speed or 1.0))
        effect_dict = {
            "type": eff.effect_type,
            "amplitude": eff.amplitude,
            "frequency": eff.frequency,
            "phase": eff.phase,
            **eff.params
        }
        return EffectModule.eval_effects(effect_dict, t_s, idx=idx, count=count)

    def _render_identify(self, now: float):
        """Render identify blink overlay"""
        elapsed = now - self._identify_start
        blink = (math.sin(elapsed * 4 * math.pi) + 1) / 2  # ~2Hz blink
        level = int(255 * blink)

        with self._lock:
            # First, try using identify_data (direct channel info from JS)
            if hasattr(self, '_identify_data') and self._identify_data:
                for dev_info in self._identify_data:
                    universe = dev_info.get("universe", 0)
                    dimmer_ch = dev_info.get("dimmer_channel")

                    if not self._ensure_universe(universe):
                        continue
                    uni = self._universes[universe]

                    if dimmer_ch is not None and 0 <= dimmer_ch < 512:
                        uni[dimmer_ch] = level
                return

            # Fallback: use registered devices
            for dev_id in self._identify_devices:
                if dev_id not in self._devices:
                    continue
                dev = self._devices[dev_id]
                if not self._ensure_universe(dev.universe):
                    continue
                uni = self._universes[dev.universe]

                # Try dimmer first, then RGB
                for ch in dev.channels.keys():
                    # Override with blink
                    if 0 <= ch < 512:
                        uni[ch] = level


    # -------------------------------------------------------------------------
    # PUBLIC API: DEVICE MANAGEMENT
    # -------------------------------------------------------------------------

    def _calib_value(self, raw: Any) -> Optional[int]:
        """Coerce a calibration DMX value to an int in [0, 255] or None."""
        if raw is None or raw == "":
            return None
        try:
            v = int(round(float(raw)))
        except Exception:
            return None
        return max(0, min(255, v))

    def _purge_device_output(self, dev: "DeviceState") -> None:
        """Zero a device's channels in its universe and clear any live edits.

        Called when a device is removed (rig change / project switch) so its
        last DMX values do not linger as ghost output on the next frame.
        Caller must hold ``self._lock``.
        """
        universe = int(getattr(dev, "universe", 0) or 0)
        chans = set(int(c) for c in (dev.channels or {}).keys() if 0 <= int(c) < 512)
        for ch in (dev.attr_map or {}).values():
            try:
                ci = int(ch)
            except Exception:
                continue
            if 0 <= ci < 512:
                chans.add(ci)
        uni = self._universes.get(universe)
        direct = self._direct_channels.get(universe)
        for ch in chans:
            if uni is not None:
                uni[ch] = 0
            if direct is not None:
                direct.pop(ch, None)
            smooth = self._smooth_targets.get(universe)
            if smooth is not None:
                smooth.pop(ch, None)

    def reset_rig(self) -> None:
        """Fully clear the rig and all derived state, zeroing every universe.

        Used when opening a new/blank project so the engine starts from a
        clean slate (no ghost devices, no residual channel values). The UI is
        responsible for resetting its own device-id counter back to 1.
        """
        with self._lock:
            self._devices.clear()
            self._cue_effects.clear()
            self._live_effect_groups.clear()
            self._live_groups_by_device.clear()
            self._cue_effect_groups.clear()
            self._cue_groups_by_device.clear()
            self._fade = None
            self._fade_effect_groups = None
            self._direct_channels.clear()
            self._manual_attrs.clear()
            self._smooth_targets.clear()
            self._smooth_last_targets.clear()
            for uni in self._universes.values():
                uni[:] = self._zero_universe
            overlay = self._autolight_overlay
        if overlay is not None and hasattr(overlay, "on_rig_changed"):
            try:
                overlay.on_rig_changed(self._devices)
            except Exception as exc:
                log.debug("autolight overlay on_rig_changed failed: %s", exc)

    def register_rig_devices(self, devices: List[Any], replace: bool = False):
        """Register/update devices with attr_map from UI rig.

        When ``replace`` is True the incoming list is treated as the complete
        rig: any device currently known but absent from ``devices`` is removed
        and its channels zeroed. This prevents "ghost" fixtures lingering after
        loading a cue list / project with fewer devices.
        """
        if not isinstance(devices, list):
            return
        with self._lock:
            if replace:
                incoming_ids = set()
                for entry in devices:
                    if not isinstance(entry, dict):
                        continue
                    did = str(entry.get("device_id") or entry.get("id") or "").strip()
                    if did:
                        incoming_ids.add(did)
                for stale_id in [d for d in self._devices if d not in incoming_ids]:
                    self._purge_device_output(self._devices[stale_id])
                    self._devices.pop(stale_id, None)
                    self._cue_effects.pop(stale_id, None)
                    self._live_groups_by_device.pop(stale_id, None)
                    self._cue_groups_by_device.pop(stale_id, None)
            for entry in devices:
                if not isinstance(entry, dict):
                    continue
                dev_id = str(entry.get("device_id") or entry.get("id") or "").strip()
                if not dev_id:
                    continue
                universe = int(entry.get("universe", 0) or 0)
                address = int(entry.get("address", 0) or 0)
                attr_raw = entry.get("attr_map") or entry.get("attrMap") or {}
                attr_map: Dict[str, int] = {}
                if isinstance(attr_raw, dict):
                    for k, v in attr_raw.items():
                        key = str(k).strip().lower()
                        if not key:
                            continue
                        try:
                            ch = int(v)
                        except Exception:
                            continue
                        if 0 <= ch < 512:
                            attr_map[key] = ch

                x_raw = entry.get("x")
                y_raw = entry.get("y")
                try:
                    x_val = float(x_raw) if x_raw is not None else None
                except Exception:
                    x_val = None
                try:
                    y_val = float(y_raw) if y_raw is not None else None
                except Exception:
                    y_val = None
                fixture_template = str(entry.get("fixture") or "").strip()
                cname = str(entry.get("cname") or "").strip()
                capabilities = _classify_device_capabilities(attr_map, fixture_template)
                home_pan = self._calib_value(entry.get("home_pan"))
                home_tilt = self._calib_value(entry.get("home_tilt"))
                invert_pan = bool(entry.get("invert_pan"))
                invert_tilt = bool(entry.get("invert_tilt"))

                if dev_id in self._devices:
                    dev = self._devices[dev_id]
                    dev.universe = universe
                    dev.base_address = address
                    if attr_map:
                        dev.attr_map = attr_map
                    if x_val is not None:
                        dev.x = x_val
                    if y_val is not None:
                        dev.y = y_val
                    if fixture_template:
                        dev.fixture_template = fixture_template
                    if cname:
                        dev.cname = cname
                    dev.capabilities = capabilities
                    dev.home_pan = home_pan
                    dev.home_tilt = home_tilt
                    dev.invert_pan = invert_pan
                    dev.invert_tilt = invert_tilt
                else:
                    self._devices[dev_id] = DeviceState(
                        device_id=dev_id,
                        universe=universe,
                        base_address=address,
                        channels={},
                        attr_map=attr_map,
                        x=x_val,
                        y=y_val,
                        fixture_template=fixture_template,
                        cname=cname,
                        capabilities=capabilities,
                        home_pan=home_pan,
                        home_tilt=home_tilt,
                        invert_pan=invert_pan,
                        invert_tilt=invert_tilt,
                    )
                self._ensure_universe(universe)

            overlay = self._autolight_overlay
        if overlay is not None and hasattr(overlay, "on_rig_changed"):
            try:
                overlay.on_rig_changed(self._devices)
            except Exception as exc:
                log.debug("autolight overlay on_rig_changed failed: %s", exc)

    def set_autolight_overlay(self, overlay: Optional[Any]) -> None:
        """Install (or clear) a callable invoked each render tick.

        Signature: ``overlay(universes: Dict[int, List[int]], now: float)``.
        The callable runs inside the render lock and may mutate universe
        values in place. Pass ``None`` to remove the overlay.
        """
        with self._lock:
            self._autolight_overlay = overlay
            devices_snapshot = dict(self._devices) if self._devices else {}
        if overlay is not None and hasattr(overlay, "on_rig_changed") and devices_snapshot:
            try:
                overlay.on_rig_changed(devices_snapshot)
            except Exception as exc:
                log.debug("autolight overlay on_rig_changed (install) failed: %s", exc)

    def has_active_fade_for(self, device_id: str) -> bool:
        """True when a cue fade is currently touching ``device_id``.

        Used by AutoLight to yield a fixture while a manual cue is fading it
        in/out. Must be called without the engine lock; acquires it briefly.
        """
        with self._lock:
            fade = self._fade
            if fade is None:
                return False
            schedule = getattr(fade, "schedule", None)
            if not isinstance(schedule, dict) or not schedule:
                return True
            return str(device_id) in schedule

    def set_channel(self, device_id: str, universe: int, channel: int, value: int):
        """Set a single channel value (from controller) - uses direct channel storage"""
        if self.valid_universe(universe) is None:
            log.warning("set_channel: out-of-range universe %r ignored", universe)
            return
        with self._lock:
            # Use direct channel storage (simpler, no device overhead)
            if universe not in self._direct_channels:
                self._direct_channels[universe] = {}
            v = self._filter_value(universe, channel, value)
            if v is None:
                return
            if self._is_smooth_channel(universe, channel) and not self._smooth_disabled:
                raw_target = v
                if self._smooth_predict:
                    v = self._predict_target(universe, channel, raw_target)
                self._smooth_last_targets.setdefault(universe, {})[channel] = raw_target
                if not self._ensure_universe(universe):
                    return
                cur = self._universes[universe][channel]
                if self._should_bypass_smoothing(cur, v):
                    self._direct_channels[universe][channel] = v
                    self._smooth_targets.get(universe, {}).pop(channel, None)
                else:
                    self._direct_channels[universe].pop(channel, None)
                    self._smooth_targets.setdefault(universe, {})[channel] = v
            else:
                if self._smooth_disabled:
                    self._smooth_targets.get(universe, {}).pop(channel, None)
                self._direct_channels[universe][channel] = v

    # -------------------------------------------------------------------------
    # PUBLIC API: MANUAL ATTRIBUTE LAYER (what the UI actually drives)
    # -------------------------------------------------------------------------

    def set_manual_attrs(self, updates: Any) -> int:
        """Hold attributes on devices: [{device_id, attr, value}, ...].

        `attr` is a fixture attribute key as registered in the device's attr_map
        ("main.dimmer", "pos.pan", "red"...) - never a DMX channel. A null value
        releases that attribute. Returns how many updates were applied.
        """
        if not isinstance(updates, list):
            return 0
        applied = 0
        with self._lock:
            for entry in updates:
                if not isinstance(entry, dict):
                    continue
                dev_id = str(entry.get("device_id") or entry.get("id") or "").strip()
                attr = str(entry.get("attr") or entry.get("attribute") or "").strip().lower()
                if not dev_id or not attr:
                    continue
                dev = self._devices.get(dev_id)
                if not dev or attr not in (dev.attr_map or {}):
                    continue
                raw = entry.get("value")
                if raw is None:
                    bucket = self._manual_attrs.get(dev_id)
                    if bucket:
                        bucket.pop(attr, None)
                        if not bucket:
                            self._manual_attrs.pop(dev_id, None)
                    applied += 1
                    continue
                try:
                    value = max(0, min(255, int(raw)))
                except (TypeError, ValueError):
                    continue
                self._manual_attrs.setdefault(dev_id, {})[attr] = value
                applied += 1
        return applied

    def release_manual_attrs(self, device_ids: Any = None) -> int:
        """Drop the manual hold - for the given devices, or for all of them."""
        with self._lock:
            if device_ids is None:
                count = len(self._manual_attrs)
                self._manual_attrs.clear()
                return count
            if not isinstance(device_ids, list):
                return 0
            count = 0
            for dev_id in device_ids:
                if self._manual_attrs.pop(str(dev_id), None) is not None:
                    count += 1
            return count

    def get_manual_attrs(self) -> Dict[str, Dict[str, int]]:
        with self._lock:
            return {dev: dict(attrs) for dev, attrs in self._manual_attrs.items()}

    def set_channels(self, device_id: str, universe: int, channels: Dict[int, int]):
        """Set multiple channel values - uses direct channel storage"""
        if self.valid_universe(universe) is None:
            log.warning("set_channels: out-of-range universe %r ignored", universe)
            return
        with self._lock:
            if universe not in self._direct_channels:
                self._direct_channels[universe] = {}
            for ch, val in channels.items():
                ch_int = int(ch) if isinstance(ch, str) else ch
                v = self._filter_value(universe, ch_int, val)
                if v is None:
                    continue
                if self._is_smooth_channel(universe, ch_int) and not self._smooth_disabled:
                    raw_target = v
                    if self._smooth_predict:
                        v = self._predict_target(universe, ch_int, raw_target)
                    self._smooth_last_targets.setdefault(universe, {})[ch_int] = raw_target
                    if not self._ensure_universe(universe):
                        continue
                    cur = self._universes[universe][ch_int]
                    if self._should_bypass_smoothing(cur, v):
                        self._direct_channels[universe][ch_int] = v
                        self._smooth_targets.get(universe, {}).pop(ch_int, None)
                    else:
                        self._direct_channels[universe].pop(ch_int, None)
                        self._smooth_targets.setdefault(universe, {})[ch_int] = v
                else:
                    if self._smooth_disabled:
                        self._smooth_targets.get(universe, {}).pop(ch_int, None)
                    self._direct_channels[universe][ch_int] = v

    def set_channels_multi(self, device_id: str, updates: Dict[int, Dict[int, int]]):
        """Atomic write of channels across multiple universes (single lock acquisition).

        `updates` shape: {universe: {channel: value}}. All writes land in the
        engine state in the same critical section so the next render frame sees
        them as one consistent snapshot — required for cross-universe sync on
        live edits."""
        if not updates:
            return
        with self._lock:
            for uni, channels in updates.items():
                if not channels:
                    continue
                if self.valid_universe(uni) is None:
                    log.warning("set_channels_multi: out-of-range universe %r ignored", uni)
                    continue
                try:
                    universe = int(uni)
                except (TypeError, ValueError):
                    continue
                if universe not in self._direct_channels:
                    self._direct_channels[universe] = {}
                direct = self._direct_channels[universe]
                for ch, val in channels.items():
                    try:
                        ch_int = int(ch) if isinstance(ch, str) else ch
                    except (TypeError, ValueError):
                        continue
                    v = self._filter_value(universe, ch_int, val)
                    if v is None:
                        continue
                    if self._is_smooth_channel(universe, ch_int) and not self._smooth_disabled:
                        raw_target = v
                        if self._smooth_predict:
                            v = self._predict_target(universe, ch_int, raw_target)
                        self._smooth_last_targets.setdefault(universe, {})[ch_int] = raw_target
                        if not self._ensure_universe(universe):
                            continue
                        cur = self._universes[universe][ch_int]
                        if self._should_bypass_smoothing(cur, v):
                            direct[ch_int] = v
                            self._smooth_targets.get(universe, {}).pop(ch_int, None)
                        else:
                            direct.pop(ch_int, None)
                            self._smooth_targets.setdefault(universe, {})[ch_int] = v
                    else:
                        if self._smooth_disabled:
                            self._smooth_targets.get(universe, {}).pop(ch_int, None)
                        direct[ch_int] = v

    # -------------------------------------------------------------------------
    # PUBLIC API: LIVE EFFECTS
    # -------------------------------------------------------------------------

    def set_live_effect_groups(self, groups: Any, action: str = "set", group_ids: Any = None):
        """Set/add/remove live effect groups (legacy or intelligent)."""
        action_key = str(action or "set").strip().lower()
        with self._lock:
            if action_key in ("remove", "delete", "stop"):
                ids = set()
                if isinstance(group_ids, list):
                    ids.update([str(x) for x in group_ids])
                if isinstance(groups, list):
                    for g in groups:
                        if isinstance(g, dict) and g.get("id"):
                            ids.add(str(g.get("id")))
                for gid in ids:
                    self._live_effect_groups.pop(gid, None)
                self._sync_live_backup_locked(drop=ids)
            else:
                normalized = self._normalize_groups(groups)
                if action_key == "add":
                    self._live_effect_groups.update(normalized)
                    self._sync_live_backup_locked(add=normalized)
                else:
                    # "set" carries the UI's whole truth, so it replaces both.
                    self._live_effect_groups = normalized
                    self._sync_live_backup_locked(replace_with=normalized)

            self._live_groups_by_device = self._build_group_device_map(self._live_effect_groups)

    def remove_effect_group_everywhere(self, group_ids: Any) -> int:
        """Remove the given group id(s) from BOTH live and cue effect state.
        Used when the UI explicitly deletes a group while a cue is playing,
        so the effect stops immediately instead of continuing from the cue
        snapshot until the cue ends ("rémanence" bug).
        Returns the number of groups actually removed (across both pools).
        """
        ids: set = set()
        if isinstance(group_ids, (list, tuple, set)):
            for x in group_ids:
                if x is None:
                    continue
                ids.add(str(x))
        elif group_ids is not None:
            ids.add(str(group_ids))
        if not ids:
            return 0

        removed = 0
        with self._lock:
            for gid in ids:
                if self._live_effect_groups.pop(gid, None) is not None:
                    removed += 1
                if self._cue_effect_groups.pop(gid, None) is not None:
                    removed += 1
                if self._fade_effect_groups:
                    pool = self._fade_effect_groups.get("pool") or {}
                    if isinstance(pool, dict) and pool.pop(gid, None) is not None:
                        removed += 1
                    union = self._fade_effect_groups.get("union") or {}
                    if isinstance(union, dict):
                        for dev_set in union.values():
                            if isinstance(dev_set, set):
                                dev_set.discard(gid)
            self._live_groups_by_device = self._build_group_device_map(self._live_effect_groups)
            self._cue_groups_by_device = self._build_group_device_map(self._cue_effect_groups)
            self._sync_live_backup_locked(drop=ids)
        return removed

    # -------------------------------------------------------------------------
    # PUBLIC API: IDENTIFY
    # -------------------------------------------------------------------------

    def start_identify(self, devices: List[Any]):
        """
        Start identify mode for devices.
        devices: list of dicts with {device_id, universe, dimmer_channel} or just device_id strings
        """
        with self._lock:
            self._identify_devices = []
            self._identify_data = []  # Store full device info for identify

            for dev in devices:
                if isinstance(dev, str):
                    # Just device_id string
                    self._identify_devices.append(dev)
                elif isinstance(dev, dict):
                    # Full device info: {device_id, universe, dimmer_channel}
                    dev_id = dev.get("device_id", "")
                    if dev_id:
                        self._identify_devices.append(dev_id)
                        self._identify_data.append(dev)

            self._identify_start = time.perf_counter()
        log.info(f"Identify started: {self._identify_devices}")

    def stop_identify(self):
        """Stop identify mode"""
        with self._lock:
            self._identify_devices = []
            self._identify_data = []
        log.info("Identify stopped")

    # -------------------------------------------------------------------------
    # PUBLIC API: CUE PLAYBACK
    # -------------------------------------------------------------------------

    def _apply_cue_locked(
        self,
        cue_data: Dict[str, Any],
        device_order: Optional[List[str]] = None,
        start_time: Optional[float] = None,
        duration_override: Optional[Any] = None,
    ):
        devices = cue_data.get("devices", {})
        # "fade" is the v2 name; "duration" is what older clients send.
        if duration_override is not None:
            duration_field = duration_override
        elif cue_data.get("fade") is not None:
            duration_field = cue_data.get("fade")
        else:
            duration_field = cue_data.get("duration", "0")
        effect_groups_raw = cue_data.get("effect_groups") or cue_data.get("effectGroups") or []
        now = float(start_time) if start_time is not None else time.perf_counter()

        start_values = self._build_base_universe_map(now, apply_fade=True)
        end_values: Dict[int, Dict[int, int]] = self._build_base_universe_map(now, apply_fade=False)
        new_cue_effects: Dict[str, Dict[int, List[LiveEffect]]] = {}

        ordered_ids = [str(x) for x in (device_order or list(devices.keys()))]
        written_channels: Dict[str, set] = {}

        prev_groups_by_device = deepcopy(self._cue_groups_by_device)
        prev_group_pool = dict(self._cue_effect_groups)

        for dev_id, dev_spec in devices.items():
            if not isinstance(dev_spec, dict):
                continue

            dev_id = str(dev_id)
            channels = dev_spec.get("channels", {})
            universe = int(channels.get("Universe", 0))

            if universe not in end_values:
                end_values[universe] = {}

            if dev_id not in self._devices:
                self._devices[dev_id] = DeviceState(device_id=dev_id, universe=universe)

            for ch_str, val in channels.items():
                if str(ch_str).lower() == "universe":
                    continue
                try:
                    ch = int(ch_str)
                    val = int(val)
                    end_values[universe][ch] = val
                    self._devices[dev_id].channels[ch] = val
                    written_channels.setdefault(dev_id, set()).add(ch)
                except (ValueError, TypeError):
                    continue

            effects_raw = dev_spec.get("effects", {})
            if effects_raw:
                new_cue_effects[dev_id] = {}
                for ch_str, eff_list in effects_raw.items():
                    try:
                        ch = int(ch_str)
                    except ValueError:
                        continue
                    if not isinstance(eff_list, list):
                        eff_list = [eff_list]
                    new_cue_effects[dev_id][ch] = []
                    for eff_spec in eff_list:
                        if isinstance(eff_spec, dict):
                            new_cue_effects[dev_id][ch].append(LiveEffect(
                                effect_type=eff_spec.get("type", ""),
                                amplitude=float(eff_spec.get("amplitude", 100)),
                                frequency=float(eff_spec.get("frequency", 1)),
                                phase=eff_spec.get("phase", 0),
                                params={k: v for k, v in eff_spec.items()
                                        if k not in ("type", "amplitude", "frequency", "phase")},
                                start_time=now
                            ))

        # The cue owns what it just wrote; a hand-held value on those channels
        # would otherwise override it on every single frame.
        self._release_overrides_for_cue_locked(written_channels)

        new_groups = self._normalize_groups(effect_groups_raw)
        groups_by_device_payload = self._build_group_device_map(new_groups)
        devices_in_cue = set(str(x) for x in devices.keys())
        devices_in_cue.update(groups_by_device_payload.keys())

        next_groups_by_device = deepcopy(prev_groups_by_device)
        for dev_id in devices_in_cue:
            next_groups_by_device[dev_id] = set(groups_by_device_payload.get(dev_id, set()))
        self._cue_groups_by_device = next_groups_by_device

        schedule = self._compute_schedule(duration_field, ordered_ids)
        speed = self._playback_run_speed if self._playback_state.get("active") else 1.0
        speed_factor = max(0.01, float(speed or 1.0))
        if speed_factor != 1.0 and schedule:
            scaled_schedule: Dict[str, tuple] = {}
            for dev_id, (start_ms, end_ms) in schedule.items():
                scaled_start = max(0, int(round(float(start_ms) / speed_factor)))
                scaled_end = max(scaled_start, int(round(float(end_ms) / speed_factor)))
                scaled_schedule[dev_id] = (scaled_start, scaled_end)
            schedule = scaled_schedule
        total_ms = max((end for _, end in schedule.values()), default=0)

        self._cue_effects = new_cue_effects

        # The pool used to be purely additive: a group that entered it once kept
        # animating for the rest of the session. Keep only what some device still
        # belongs to -- plus, while crossfading, the groups being faded out.
        referenced = {gid for gids in next_groups_by_device.values() for gid in gids}
        if total_ms > 0:
            referenced |= {gid for gids in prev_groups_by_device.values() for gid in gids}
        group_pool = {gid: g for gid, g in prev_group_pool.items() if gid in referenced}
        group_pool.update(new_groups)
        self._cue_effect_groups = group_pool

        if total_ms > 0:
            self._fade = FadeState(
                start_values=start_values,
                end_values=end_values,
                start_time=now,
                duration_ms=total_ms,
                schedule=schedule
            )
            union: Dict[str, set] = {}
            for dev_id in set(prev_groups_by_device.keys()).union(next_groups_by_device.keys()):
                prev_set = prev_groups_by_device.get(dev_id, set())
                next_set = next_groups_by_device.get(dev_id, set())
                union[dev_id] = set(prev_set).union(set(next_set))
            self._fade_effect_groups = {
                "prev": prev_groups_by_device,
                "next": next_groups_by_device,
                "union": union,
                "pool": group_pool,
            }
            log.info("Cue started with fade %sms", total_ms)
        else:
            self._fade = None
            self._fade_effect_groups = None
            log.info("Cue applied (cut)")

    def go_cue(
        self,
        cue_data: Dict[str, Any],
        device_order: Optional[List[str]] = None,
        start_time: Optional[float] = None,
    ):
        """
        Execute a cue with fade.
        cue_data: {"devices": {id: {"channels": {ch: val}, "effects": {...}}}, "duration": "100 > 500"}
        """
        with self._lock:
            self._apply_cue_locked(cue_data, device_order=device_order, start_time=start_time)

    def stop_playback(self):
        """Stop current cue playback"""
        thread_to_join: Optional[threading.Thread] = None
        with self._lock:
            self._playback_stop_event.set()
            self._playback_skip_requested = False
            self._playback_wait_adjust_ms = 0
            self._playback_loop = False
            self._playback_loop_count = None
            self._playback_loop_pass = 0
            self._timeline_runtime = None
            self._fade = None
            self._cue_effects.clear()
            # A stopped playback owns no effect any more; leaving the cue pool
            # behind kept groups rendering after their cue was gone.
            self._cue_effect_groups = {}
            self._cue_groups_by_device = {}
            self._fade_effect_groups = None
            self._restore_playback_render_locked()
            self._playback_run_speed = self._playback_speed
            self._update_playback_state_locked(
                active=False,
                paused=False,
                phase="idle",
                cue_index=-1,
                plan_index=-1,
                cue_name="",
                phase_remaining_ms=0,
                sequence_length=0,
                speed=self._playback_speed,
            )
            if self._playback_thread and self._playback_thread is not threading.current_thread():
                thread_to_join = self._playback_thread
            self._playback_thread = None
        log.info("Playback stopped")
        if thread_to_join:
            thread_to_join.join(timeout=1.0)
        self._notify_state_change()

    def pause_playback(self, paused: bool):
        with self._lock:
            if not self._playback_state.get("active"):
                return
            if self._timeline_runtime is not None:
                now = time.perf_counter()
                self._timeline_runtime["base_offset_ms"] = self._timeline_elapsed_ms_locked(now)
                self._timeline_runtime["anchor_time"] = now
            self._update_playback_state_locked(paused=bool(paused))
        self._notify_state_change()

    def skip_playback_step(self):
        handled = False
        with self._lock:
            if not self._playback_state.get("active"):
                return
            if self._timeline_runtime is not None:
                now = time.perf_counter()
                elapsed_ms = self._timeline_elapsed_ms_locked(now)
                priority_mode = self._normalize_timeline_priority_mode(self._timeline_runtime.get("priority_mode"))
                active_blocks = self._timeline_active_blocks_locked(elapsed_ms)
                focus_block = self._timeline_pick_focus_block_locked(active_blocks, priority_mode)
                if focus_block is not None:
                    self._timeline_runtime["base_offset_ms"] = int(focus_block.end_ms)
                else:
                    next_blocks = self._timeline_next_blocks_locked(elapsed_ms)
                    next_block = self._timeline_pick_focus_block_locked(next_blocks, priority_mode)
                    self._timeline_runtime["base_offset_ms"] = int(next_block.start_ms) if next_block is not None else elapsed_ms
                self._timeline_runtime["anchor_time"] = now
                self._playback_wait_adjust_ms = 0
                self._update_playback_state_locked(phase_remaining_ms=0)
                handled = True
            if handled:
                pass
            else:
                self._playback_skip_requested = True
                self._fade = None
                self._update_playback_state_locked(wait_remaining_ms=0)
        self._notify_state_change()

    def adjust_playback_wait(self, delta_ms: int):
        handled = False
        with self._lock:
            if not self._playback_state.get("active"):
                return
            if str(self._playback_state.get("phase") or "") != "waiting":
                return
            if self._timeline_runtime is not None:
                now = time.perf_counter()
                self._playback_wait_adjust_ms += int(delta_ms)
                self._timeline_runtime["base_offset_ms"] = max(
                    0,
                    int(self._timeline_elapsed_ms_locked(now) - int(delta_ms))
                )
                self._timeline_runtime["anchor_time"] = now
                self._update_playback_state_locked()
                handled = True
            if not handled:
                self._playback_wait_adjust_ms += int(delta_ms)
                self._update_playback_state_locked()
        self._notify_state_change()

    def seek_playback(self, seek_ms: int):
        with self._lock:
            if not self._playback_state.get("active") or self._timeline_runtime is None:
                return
            runtime = self._timeline_runtime
            total_length_ms = max(0, int(runtime.get("total_length_ms", 0) or 0))
            resolved_seek_ms = max(0, min(int(seek_ms or 0), total_length_ms))
            now = time.perf_counter()
            runtime["base_offset_ms"] = resolved_seek_ms
            runtime["anchor_time"] = now
            runtime["last_focus_key"] = None
            runtime["cue_token"] = 0
            self._playback_wait_adjust_ms = 0
            self._update_playback_state_locked(phase_remaining_ms=0)
        self._notify_state_change()

    def run_sequence(
        self,
        sequence: List[Dict[str, Any]],
        start_index: int = 0,
        virtual_groups: Any = None,
        speed: Any = 1.0,
        mode: str = "classic",
        timeline: Any = None,
        priority_mode: str = "top",
        start_ms: int = 0,
        paused: bool = False,
        loop: bool = False,
        loop_count: Any = None,
        lead_in_ms: int = 0,
    ):
        mode_key = str(mode or "classic").strip().lower()
        if mode_key == "timeline":
            self._run_timeline(timeline, speed=speed, priority_mode=priority_mode, start_ms=start_ms, paused=paused)
            return
        if not isinstance(sequence, list):
            raise ValueError("sequence must be a list")

        # A sequence still written in the old model is converted here, so an
        # untouched show file keeps its exact timing.
        sequence, migrated_lead_in_ms = self.migrate_sequence_time_model(sequence)
        lead_in_ms = max(0, int(lead_in_ms or 0)) or migrated_lead_in_ms

        cleaned = [step for step in sequence if isinstance(step, dict)]
        resolved_start_index = self._resolve_playback_start_index(cleaned, int(start_index or 0)) if cleaned else 0
        run_speed = self._normalize_playback_speed(speed if speed is not None else self._playback_speed)
        full_plan = self._expand_playback_sequence(
            cleaned, virtual_groups=virtual_groups, speed=run_speed, lead_in_ms=lead_in_ms,
        )
        run_start = 0
        if full_plan:
            for idx, entry in enumerate(full_plan):
                if entry.cue_index == resolved_start_index:
                    run_start = idx
                    break
        prefix_plan = full_plan[:run_start]
        run_plan = full_plan[run_start:]
        self.stop_playback()

        with self._lock:
            self._prepare_playback_render_locked()
            self._playback_speed = run_speed
            self._playback_run_speed = run_speed
            self._effect_epoch = time.perf_counter()
            for entry in prefix_plan:
                self._apply_cue_locked(
                    entry.cue_payload,
                    device_order=entry.device_order,
                    start_time=time.perf_counter(),
                    duration_override="0",
                )
            self._fade = None
            self._fade_effect_groups = None
            self._playback_stop_event = threading.Event()
            self._playback_skip_requested = False
            self._playback_wait_adjust_ms = 0
            self._playback_loop = bool(loop)
            try:
                normalized_loop_count = int(loop_count) if loop_count is not None else 0
            except (TypeError, ValueError):
                normalized_loop_count = 0
            self._playback_loop_count = normalized_loop_count if normalized_loop_count > 0 else None
            self._playback_loop_pass = 1 if run_plan else 0
            self._update_playback_state_locked(
                active=bool(run_plan),
                paused=False,
                phase="idle",
                cue_index=-1,
                plan_index=-1,
                cue_name="",
                phase_remaining_ms=0,
                sequence_length=len(full_plan),
                speed=run_speed,
            )
            if run_plan:
                self._playback_thread = threading.Thread(
                    target=self._play_sequence_looped,
                    # Looping replays the whole list from the top, even when the
                    # first pass started mid-sequence ("play from cue 5").
                    args=(run_plan, len(full_plan), full_plan),
                    daemon=True,
                    name="DMXPlaybackScheduler",
                )
                self._playback_thread.start()
            else:
                self._playback_run_speed = self._playback_speed
                self._restore_playback_render_locked()
        self._notify_state_change()

    def _run_timeline(
        self,
        timeline: Any,
        speed: Any = 1.0,
        priority_mode: str = "top",
        start_ms: int = 0,
        paused: bool = False,
    ) -> None:
        blocks = self._normalize_timeline_blocks(timeline)
        run_speed = self._normalize_playback_speed(speed if speed is not None else self._playback_speed)
        resolved_priority = self._normalize_timeline_priority_mode(priority_mode)
        total_length_ms = max((int(block.end_ms) for block in blocks), default=0)
        resolved_start_ms = max(0, min(int(start_ms or 0), total_length_ms))

        self.stop_playback()

        with self._lock:
            self._prepare_playback_render_locked()
            self._playback_speed = run_speed
            self._playback_run_speed = run_speed
            self._effect_epoch = time.perf_counter()
            self._fade = None
            self._cue_effects.clear()
            self._cue_effect_groups = {}
            self._cue_groups_by_device = {}
            self._fade_effect_groups = None
            self._playback_stop_event = threading.Event()
            self._playback_skip_requested = False
            self._playback_wait_adjust_ms = 0
            self._timeline_runtime = {
                "blocks": blocks,
                "priority_mode": resolved_priority,
                "total_length_ms": total_length_ms,
                "anchor_time": time.perf_counter(),
                "base_offset_ms": resolved_start_ms,
                "last_focus_key": None,
                "cue_token": 0,
            }
            self._update_playback_state_locked(
                active=bool(blocks),
                paused=bool(paused),
                phase="idle",
                cue_index=-1,
                plan_index=-1,
                cue_name="",
                cue_token=0,
                phase_remaining_ms=0,
                sequence_length=len(blocks),
                speed=run_speed,
            )
            if blocks:
                self._playback_thread = threading.Thread(
                    target=self._play_timeline,
                    daemon=True,
                    name="DMXTimelineScheduler",
                )
                self._playback_thread.start()
            else:
                self._playback_run_speed = self._playback_speed
                self._restore_playback_render_locked()
        self._notify_state_change()

    def playback_control(self, action: str, delta_ms: int = 0):
        action_key = str(action or "").strip().lower()
        if action_key == "pause":
            self.pause_playback(True)
        elif action_key == "resume":
            self.pause_playback(False)
        elif action_key == "skip":
            self.skip_playback_step()
        elif action_key == "seek":
            self.seek_playback(delta_ms)
        elif action_key in ("adjust_wait", "adjust"):
            self.adjust_playback_wait(delta_ms)
        elif action_key == "stop":
            self.stop_playback()
        else:
            raise ValueError(f"unknown playback action: {action}")

    def _play_timeline(self) -> None:
        stop_event = self._playback_stop_event

        try:
            while not stop_event.is_set():
                now = time.perf_counter()
                with self._lock:
                    runtime = self._timeline_runtime
                    if not runtime:
                        return
                    paused = bool(self._playback_state.get("paused"))
                    elapsed_ms = self._timeline_elapsed_ms_locked(now)
                    total_length_ms = int(runtime.get("total_length_ms", 0) or 0)
                    priority_mode = self._normalize_timeline_priority_mode(runtime.get("priority_mode"))
                    active_blocks = self._timeline_active_blocks_locked(elapsed_ms)
                    next_blocks = self._timeline_next_blocks_locked(elapsed_ms)
                    focus_block = self._timeline_pick_focus_block_locked(active_blocks, priority_mode)
                    waiting_block = self._timeline_pick_focus_block_locked(next_blocks, priority_mode)

                    phase = "idle"
                    phase_remaining_ms = 0
                    cue_index = -1
                    plan_index = -1
                    cue_name = ""

                    if focus_block is not None:
                        local_ms = max(0, elapsed_ms - int(focus_block.start_ms))
                        fade_end_ms = max(int(focus_block.fade_start_ms), int(focus_block.fade_end_ms))
                        if local_ms < fade_end_ms:
                            phase = "fading"
                            phase_remaining_ms = max(0, fade_end_ms - local_ms)
                        else:
                            phase = "active"
                            phase_remaining_ms = max(0, int(focus_block.end_ms) - elapsed_ms)
                        cue_index = int(focus_block.cue_index)
                        plan_index = int(focus_block.plan_index)
                        cue_name = str(focus_block.cue_name)
                        focus_key = (focus_block.plan_index, focus_block.start_ms)
                        if runtime.get("last_focus_key") != focus_key:
                            runtime["last_focus_key"] = focus_key
                            runtime["cue_token"] = int(runtime.get("cue_token", 0) or 0) + 1
                    elif waiting_block is not None:
                        phase = "waiting"
                        phase_remaining_ms = max(0, int(waiting_block.start_ms) - elapsed_ms)
                        cue_index = int(waiting_block.cue_index)
                        plan_index = int(waiting_block.plan_index)
                        cue_name = str(waiting_block.cue_name)
                    else:
                        runtime["last_focus_key"] = None

                    cue_token = int(runtime.get("cue_token", 0) or 0)
                    self._update_playback_state_locked(
                        active=bool(runtime.get("blocks")),
                        paused=paused,
                        phase=phase,
                        cue_index=cue_index,
                        plan_index=plan_index,
                        cue_name=cue_name,
                        cue_token=cue_token,
                        phase_remaining_ms=phase_remaining_ms,
                        sequence_length=len(runtime.get("blocks") or []),
                        speed=self._playback_run_speed,
                    )

                    should_finish = (focus_block is None and waiting_block is None and elapsed_ms >= total_length_ms)

                self._notify_state_change()

                if should_finish:
                    break
                time.sleep(self._playback_poll_interval_sec())

            with self._lock:
                self._timeline_runtime = None
                self._restore_playback_render_locked()
                self._playback_run_speed = self._playback_speed
                self._update_playback_state_locked(
                    active=False,
                    paused=False,
                    phase="idle",
                    cue_index=-1,
                    plan_index=-1,
                    cue_name="",
                    cue_token=0,
                    phase_remaining_ms=0,
                    sequence_length=0,
                    speed=self._playback_speed,
                )
                self._playback_thread = None
            self._notify_state_change()
        finally:
            if stop_event.is_set():
                with self._lock:
                    self._timeline_runtime = None
                    self._fade = None
                    self._cue_effects.clear()
                    self._fade_effect_groups = None
                    self._restore_playback_render_locked()
                    self._playback_run_speed = self._playback_speed
                    self._update_playback_state_locked(
                        active=False,
                        paused=False,
                        phase="idle",
                        cue_index=-1,
                        plan_index=-1,
                        cue_name="",
                        cue_token=0,
                        phase_remaining_ms=0,
                        sequence_length=0,
                        speed=self._playback_speed,
                    )
                    self._playback_thread = None
                self._notify_state_change()

    def _play_sequence_looped(
        self,
        run_plan: List[PlaybackPlanEntry],
        sequence_length: int,
        loop_plan: Optional[List[PlaybackPlanEntry]] = None,
    ):
        """Play the plan once, then again for as long as looping is enabled.

        Each pass runs through ``_play_sequence``, whose timing anchors are
        locals — so a new pass re-anchors itself and absolute-clock playback
        stays correct. Only the last pass restores the live rig state.
        """
        stop_event = self._playback_stop_event
        plan = run_plan
        while True:
            with self._lock:
                loop_enabled = bool(self._playback_loop)
                loop_target = self._playback_loop_count
                current_pass = max(1, int(self._playback_loop_pass))
            last_pass = (not loop_enabled) or (loop_target is not None and current_pass >= loop_target)

            self._play_sequence(plan, sequence_length, finalize=last_pass)

            if last_pass or stop_event.is_set():
                return
            plan = loop_plan or run_plan
            with self._lock:
                self._playback_loop_pass = current_pass + 1

    def _play_sequence(
        self,
        sequence: List[PlaybackPlanEntry],
        sequence_length: int,
        finalize: bool = True,
    ):
        stop_event = self._playback_stop_event
        cue_token = 0
        run_origin = time.perf_counter()
        run_origin_host_ms = int(round(time.time() * 1000.0))
        pause_shift_sec = 0.0
        manual_offset_ms = 0
        absolute_clock_mode = str(getattr(self, "_playback_clock_mode", "timeline") or "timeline").strip().lower() == "absolute_clock"

        try:
            for entry in sequence:
                if stop_event.is_set():
                    return

                cue_index = int(entry.cue_index)
                absolute_plan_index = int(entry.plan_index)
                pause_started: Optional[float] = None
                wait_ms = max(0, int(entry.sleep_ms))
                skipped_before_start = False
                skipped_fade = False
                # A wait that belongs to a cue is that cue HOLDING, and the
                # operator must see it as such; only the lead-in is a plain wait.
                holds_previous = int(getattr(entry, "hold_cue_index", -1)) >= 0
                wait_phase = "holding" if holds_previous else "waiting"
                wait_cue_index = int(entry.hold_cue_index) if holds_previous else cue_index
                wait_cue_name = str(entry.hold_cue_name) if holds_previous else entry.cue_name

                cue_token += 1
                with self._lock:
                    self._playback_wait_adjust_ms = 0

                if wait_ms > 0:
                    with self._lock:
                        self._update_playback_state_locked(
                            active=True,
                            paused=False,
                            phase=wait_phase,
                            cue_index=wait_cue_index,
                            plan_index=absolute_plan_index,
                            cue_name=wait_cue_name,
                            cue_token=cue_token,
                            phase_remaining_ms=wait_ms,
                            sequence_length=sequence_length,
                        )
                    self._notify_state_change()

                    if self._log_playback_timing:
                        wait_start = time.perf_counter()
                        actual_elapsed_ms = int(round((wait_start - run_origin) * 1000.0))
                        planned_host_ms = run_origin_host_ms + int(entry.wait_start_at_ms) + int(manual_offset_ms)
                        log.info(
                            "[PLAYBACK] cue=%s name=%s phase=waiting plan=%sms actual=%sms delta=%sms remain=%sms host=%s mode=%s",
                            cue_index,
                            entry.cue_name,
                            int(entry.wait_start_at_ms),
                            actual_elapsed_ms,
                            actual_elapsed_ms - int(entry.wait_start_at_ms),
                            wait_ms,
                            self._format_host_clock_ms(planned_host_ms),
                            "absolute_clock" if absolute_clock_mode else "timeline",
                        )

                    wait_anchor = time.perf_counter()
                    wait_target = wait_anchor + (wait_ms / 1000.0)
                    while not stop_event.is_set():
                        now = time.perf_counter()
                        with self._lock:
                            paused = bool(self._playback_state.get("paused"))
                            wait_adjust = int(self._playback_wait_adjust_ms)
                            skip_now = self._playback_skip_requested
                        if paused:
                            if pause_started is None:
                                pause_started = now
                            time.sleep(self._playback_poll_interval_sec())
                            continue
                        if pause_started is not None:
                            paused_for = now - pause_started
                            if absolute_clock_mode:
                                pause_shift_sec += paused_for
                            else:
                                wait_target += paused_for
                            pause_started = None
                        if absolute_clock_mode:
                            adjusted_wait_end_ms = int(entry.wait_end_at_ms) + int(manual_offset_ms) + int(wait_adjust)
                            adjusted_target = run_origin + pause_shift_sec + (adjusted_wait_end_ms / 1000.0)
                            planned_slot_end_ms = int(entry.fade_end_at_ms) + int(manual_offset_ms) + int(wait_adjust)
                        else:
                            adjusted_wait_end_ms = wait_ms + int(wait_adjust)
                            adjusted_target = wait_target + (wait_adjust / 1000.0)
                            planned_slot_end_ms = adjusted_wait_end_ms
                        if skip_now:
                            if absolute_clock_mode:
                                actual_elapsed_ms = int(round((now - run_origin) * 1000.0))
                                manual_offset_ms += actual_elapsed_ms - planned_slot_end_ms
                            with self._lock:
                                self._playback_skip_requested = False
                                self._playback_wait_adjust_ms = 0
                                self._update_playback_state_locked(phase_remaining_ms=0)
                            skipped_before_start = True
                            break
                        with self._lock:
                            self._update_playback_state_locked(
                                active=True,
                                paused=False,
                                phase=wait_phase,
                                cue_index=wait_cue_index,
                                plan_index=absolute_plan_index,
                                cue_name=wait_cue_name,
                                cue_token=cue_token,
                                phase_remaining_ms=max(0, int(round((adjusted_target - now) * 1000.0))),
                                sequence_length=sequence_length,
                            )
                        remaining = adjusted_target - now
                        if remaining <= 0:
                            if absolute_clock_mode:
                                manual_offset_ms += int(wait_adjust)
                            with self._lock:
                                self._playback_wait_adjust_ms = 0
                                self._update_playback_state_locked(phase_remaining_ms=0)
                            break
                        time.sleep(min(self._playback_poll_interval_sec(), remaining))

                if stop_event.is_set():
                    return

                if skipped_before_start:
                    continue

                # Trailing hold: nothing left to fade, the look just stays.
                if bool(getattr(entry, "hold_only", False)):
                    continue

                with self._lock:
                    self._playback_wait_adjust_ms = 0
                    self._update_playback_state_locked(
                        active=True,
                        paused=False,
                        phase="fading",
                        cue_index=cue_index,
                        plan_index=absolute_plan_index,
                        cue_name=entry.cue_name,
                        cue_token=cue_token,
                        phase_remaining_ms=max(0, int(entry.fade_ms)),
                        sequence_length=sequence_length,
                    )
                self._notify_state_change()

                planned_fade_start_ms = int(entry.fade_start_at_ms) + int(manual_offset_ms)
                planned_fade_end_ms = int(entry.fade_end_at_ms) + int(manual_offset_ms)
                if absolute_clock_mode:
                    cue_start_target = run_origin + pause_shift_sec + (planned_fade_start_ms / 1000.0)
                else:
                    cue_start_target = time.perf_counter()
                cue_start = time.perf_counter()
                if cue_start < cue_start_target:
                    delay = cue_start_target - cue_start
                    if delay > 0:
                        if delay > 0.002:
                            time.sleep(delay - 0.001)
                        while cue_start < cue_start_target:
                            cue_start = time.perf_counter()
                    cue_start = time.perf_counter()
                if self._log_playback_timing:
                    actual_elapsed_ms = int(round((cue_start - run_origin) * 1000.0))
                    planned_host_ms = run_origin_host_ms + planned_fade_start_ms
                    log.info(
                        "[PLAYBACK] cue=%s name=%s phase=fading plan=%sms actual=%sms delta=%sms host=%s mode=%s",
                        cue_index,
                        entry.cue_name,
                        planned_fade_start_ms,
                        actual_elapsed_ms,
                        actual_elapsed_ms - planned_fade_start_ms,
                        self._format_host_clock_ms(planned_host_ms),
                        "absolute_clock" if absolute_clock_mode else "timeline",
                    )
                self.go_cue(entry.cue_payload, entry.device_order, start_time=cue_start)

                fade_ms = max(0, int(entry.fade_ms))
                if absolute_clock_mode:
                    fade_target = run_origin + pause_shift_sec + (planned_fade_end_ms / 1000.0)
                else:
                    fade_target = cue_start + (fade_ms / 1000.0)

                if fade_ms > 0:
                    pause_started = None
                    while not stop_event.is_set():
                        now = time.perf_counter()
                        with self._lock:
                            paused = bool(self._playback_state.get("paused"))
                            skip_now = self._playback_skip_requested
                        if paused:
                            if pause_started is None:
                                pause_started = now
                            time.sleep(self._playback_poll_interval_sec())
                            continue
                        if pause_started is not None:
                            paused_for = now - pause_started
                            if absolute_clock_mode:
                                pause_shift_sec += paused_for
                                fade_target = run_origin + pause_shift_sec + (planned_fade_end_ms / 1000.0)
                            else:
                                fade_target += paused_for
                            pause_started = None
                        if skip_now:
                            if absolute_clock_mode:
                                actual_elapsed_ms = int(round((now - run_origin) * 1000.0))
                                manual_offset_ms += actual_elapsed_ms - planned_fade_end_ms
                            with self._lock:
                                self._playback_skip_requested = False
                                self._fade = None
                                self._fade_effect_groups = None
                            skipped_fade = True
                            fade_target = now
                        with self._lock:
                            self._update_playback_state_locked(
                                active=True,
                                paused=False,
                                phase="fading",
                                cue_index=cue_index,
                                plan_index=absolute_plan_index,
                                cue_name=entry.cue_name,
                                cue_token=cue_token,
                                phase_remaining_ms=max(0, int(round((fade_target - now) * 1000.0))),
                                sequence_length=sequence_length,
                            )
                        remaining = fade_target - now
                        if remaining <= 0:
                            break
                        time.sleep(min(self._playback_poll_interval_sec(), remaining))

                if skipped_fade:
                    with self._lock:
                        self._update_playback_state_locked(phase_remaining_ms=0)

            # A looping run keeps the rig and the playback state as they are and
            # comes straight back for the next pass.
            if not finalize:
                return
            with self._lock:
                self._restore_playback_render_locked()
                self._playback_run_speed = self._playback_speed
                self._playback_loop = False
                self._playback_loop_count = None
                self._playback_loop_pass = 0
                self._update_playback_state_locked(
                    active=False,
                    paused=False,
                    phase="idle",
                    cue_index=-1,
                    plan_index=-1,
                    cue_name="",
                    phase_remaining_ms=0,
                    sequence_length=0,
                    speed=self._playback_speed,
                )
                self._playback_thread = None
            self._notify_state_change()
        finally:
            if stop_event.is_set():
                with self._lock:
                    self._fade = None
                    self._cue_effects.clear()
                    self._fade_effect_groups = None
                    self._restore_playback_render_locked()
                    self._playback_run_speed = self._playback_speed
                    self._update_playback_state_locked(
                        active=False,
                        paused=False,
                        phase="idle",
                        cue_index=-1,
                        plan_index=-1,
                        cue_name="",
                        phase_remaining_ms=0,
                        sequence_length=0,
                        speed=self._playback_speed,
                    )
                    self._playback_thread = None
                self._notify_state_change()

    def _compute_schedule(self, duration_field: Any, device_ids: List[str]) -> Dict[str, tuple]:
        """Compute per-device fade schedule from duration field"""
        import re
        import random

        s = str(duration_field or "0").strip()
        if not s:
            return {d: (0, 0) for d in device_ids}

        op = None
        parts: List[str] = []

        if "><" in s:
            op = "><"
            parts = s.split("><")
        elif "<>" in s:
            op = "<>"
            parts = s.split("<>")
        elif "||" in s:
            op = "||"
            parts = s.split("||")
        elif "|" in s:
            op = "|"
            parts = s.split("|")
        elif ">" in s:
            op = ">"
            parts = s.split(">")
        elif "<" in s:
            op = "<"
            parts = s.split("<")
        elif "?" in s:
            op = "?"
            parts = s.split("?")

        base_fade_ms = 0
        spread_ms = 0
        if op and len(parts) >= 2:
            try:
                base_fade_ms = int(float(parts[0].strip() or 0))
            except Exception:
                base_fade_ms = 0
            try:
                spread_ms = int(float(parts[1].strip() or 0))
            except Exception:
                spread_ms = 0
        else:
            nums = [int(x) for x in re.findall(r"\d+", s)]
            base_fade_ms = nums[0] if nums else 0
            spread_ms = 0

        n = len(device_ids)
        if n == 0:
            return {}

        if n <= 1 or spread_ms <= 0 or not op:
            return {d: (0, base_fade_ms) for d in device_ids}

        offsets: Dict[str, float] = {}
        ids = list(device_ids)

        if op == "|":
            for i, dev_id in enumerate(ids):
                offsets[dev_id] = 0 if i % 2 == 0 else spread_ms
        elif op == "||":
            split = int(math.ceil(n / 2))
            for i, dev_id in enumerate(ids):
                offsets[dev_id] = 0 if i < split else spread_ms
        else:
            indices: List[int] = []
            if op == ">":
                indices = list(range(n))
            elif op == "<":
                indices = list(reversed(range(n)))
            elif op == "><":
                left, right = 0, n - 1
                while left <= right:
                    if left == right:
                        indices.append(left)
                    else:
                        indices.append(left)
                        indices.append(right)
                    left += 1
                    right -= 1
            elif op == "<>":
                if n % 2 == 1:
                    mid = (n - 1) // 2
                    indices.append(mid)
                    for step in range(1, mid + 1):
                        if mid - step >= 0:
                            indices.append(mid - step)
                        if mid + step < n:
                            indices.append(mid + step)
                else:
                    mid_left = n // 2 - 1
                    mid_right = n // 2
                    indices.extend([mid_left, mid_right])
                    for step in range(1, mid_left + 1):
                        if mid_left - step >= 0:
                            indices.append(mid_left - step)
                        if mid_right + step < n:
                            indices.append(mid_right + step)
            elif op == "?":
                indices = list(range(n))
                random.shuffle(indices)
            else:
                indices = list(range(n))

            denom = max(n - 1, 1)
            for rank, idx in enumerate(indices):
                dev_id = ids[idx]
                offsets[dev_id] = (spread_ms * rank) / denom

        schedule: Dict[str, tuple] = {}
        for dev_id in ids:
            offset = offsets.get(dev_id, 0)
            start_ms = max(0, int(offset))
            end_ms = max(start_ms, int(offset + base_fade_ms))
            schedule[dev_id] = (start_ms, end_ms)

        return schedule

    # -------------------------------------------------------------------------
    # STATE BROADCASTING (for SSE)
    # -------------------------------------------------------------------------

    def add_state_callback(self, callback: Callable):
        """Add callback for state updates"""
        self._state_callbacks.append(callback)

    def _broadcast_state(self, include_universes: bool = True):
        """Send current state to all callbacks"""
        if not self._state_callbacks:
            return
        push_start = time.perf_counter()

        with self._lock:
            state = {
                "identify_active": bool(self._identify_devices),
                "fade_active": self._fade is not None,
                "playback": self._playback_state_snapshot_locked(),
                "timestamp": time.time()
            }
            if include_universes:
                # The preview gets changed channels only, plus a full keyframe
                # every 2 s (and on the first push after a connect / rig change).
                # Reuses the snapshots the compute loop already built for the
                # emitter, so this costs one bytes-compare per universe.
                now_ts = time.perf_counter()
                frames = {u: self._emit_frames.get(u) or bytes(v) for u, v in self._universes.items()}
                keyframe = (
                    not self._preview_last
                    or set(frames) != set(self._preview_last)
                    or (now_ts - self._preview_last_full) >= 2.0
                )
                if keyframe:
                    self._preview_last_full = now_ts
                    state["preview_full"] = True
                    state["universes"] = {str(u): list(frame) for u, frame in frames.items()}
                else:
                    diff: Dict[str, Dict[str, int]] = {}
                    for u, frame in frames.items():
                        prev = self._preview_last.get(u)
                        if prev == frame:
                            continue
                        changed = {
                            str(i): frame[i]
                            for i in range(min(len(frame), len(prev or b"")))
                            if prev[i] != frame[i]
                        }
                        if changed:
                            diff[str(u)] = changed
                    state["preview_full"] = False
                    if diff:
                        state["universes_diff"] = diff
                self._preview_last = frames

        for cb in self._state_callbacks:
            try:
                cb(state)
            except Exception as e:
                log.error(f"State callback error: {e}")
        self._record_perf("state", (time.perf_counter() - push_start) * 1000.0)

    # -------------------------------------------------------------------------
    # MOVEMENT SMOOTHING
    # -------------------------------------------------------------------------

    def set_movement_channels(self, universes: Dict[int, List[int]]):
        """Set movement (pan/tilt) channels by universe."""
        with self._lock:
            self._smooth_channels = {int(u): set(map(int, chs)) for u, chs in (universes or {}).items()}
            # Remove smooth channels from direct channels to avoid overriding smoothing
            for u, chs in self._smooth_channels.items():
                dc = self._direct_channels.get(u)
                if not dc:
                    continue
                for ch in chs:
                    dc.pop(ch, None)
            if self._smooth_disabled:
                self._smooth_targets.clear()
                self._smooth_last_targets.clear()

    def set_dummy_channels(self, universes: Dict[int, List[int]]):
        """Set dummy channels used to force updates."""
        with self._lock:
            if not self._dummy_enabled:
                self._dummy_channels = {}
                self._dummy_state = {}
                return
            self._dummy_channels = {int(u): [int(c) for c in chs] for u, chs in (universes or {}).items()}
            self._dummy_state = {int(u): 0 for u in self._dummy_channels.keys()}

    def _is_smooth_channel(self, universe: int, channel: int) -> bool:
        return channel in self._smooth_channels.get(universe, set())

    def _apply_smoothing(self):
        """Move current values toward targets for smooth channels."""
        if self._smooth_disabled:
            return
        step = max(1, int(self._smooth_step))
        for uni, targets in list(self._smooth_targets.items()):
            if not targets:
                continue
            if not self._ensure_universe(uni):
                continue
            uni_buf = self._universes[uni]
            to_remove = []
            for ch, target in targets.items():
                if not (0 <= ch < 512):
                    to_remove.append(ch)
                    continue
                cur = int(uni_buf[ch])
                if cur == target:
                    to_remove.append(ch)
                    continue
                diff = target - cur
                if abs(diff) <= step:
                    uni_buf[ch] = target
                    to_remove.append(ch)
                else:
                    uni_buf[ch] = cur + step if diff > 0 else cur - step
            for ch in to_remove:
                targets.pop(ch, None)

    def _apply_dummy_overlay(self, universe: int, values: List[int]) -> List[int]:
        """Toggle dummy channels between 0 and 255 on every send."""
        if not self._dummy_enabled:
            return values
        channels = self._dummy_channels.get(universe)
        if not channels:
            return values

        cur = self._dummy_state.get(universe, 0)
        nxt = 255 if cur == 0 else 0
        self._dummy_state[universe] = nxt

        out = list(values)
        for ch in channels:
            if 0 <= ch < 512:
                out[ch] = nxt
        return out

    @staticmethod
    def _should_bypass_smoothing(cur: int, target: int) -> bool:
        return (cur == 0 and target == 255) or (cur == 255 and target == 0)


    def get_packet_stats(self) -> Dict[str, Any]:
        return {
            "artnet_packets": self._packet_count,
            "last_send_ts": self._last_send_ts
        }
