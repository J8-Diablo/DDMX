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

@dataclass
class PlaybackPlanEntry:
    plan_index: int
    cue_index: int
    cue_name: str
    cue_payload: Dict[str, Any]
    device_order: List[str]
    fade_ms: int
    sleep_ms: int
    wait_start_at_ms: int = 0
    wait_end_at_ms: int = 0
    fade_start_at_ms: int = 0
    fade_end_at_ms: int = 0

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

        # Direct channel values (from live UI edits, stored per universe)
        self._direct_channels: Dict[int, Dict[int, int]] = {}  # {universe: {channel: value}}

        # Render mode (ui | backend)
        self._render_mode = os.environ.get("DMX_RENDER_MODE", "ui").strip().lower()

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
        self._live_effects: Dict[str, Dict[int, List[LiveEffect]]] = {}  # {device_id: {channel: [effects]}}

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
        self._playback_force_backend_render = False
        self._playback_live_state_backup: Optional[Dict[str, Any]] = None
        self._playback_clock_mode = os.environ.get("DMX_PLAYBACK_CLOCK_MODE", "timeline").strip().lower()
        self._playback_speed = 1.0
        self._playback_run_speed = 1.0
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
        self._playback_engine_hz = max(
            self._tick_hz,
            min(240.0, self._read_env_float("DMX_PLAYBACK_ENGINE_HZ", max(self._tick_hz, 120.0))),
        )
        self._playback_ui_fps = max(1.0, min(30.0, self._read_env_float("DMX_PLAYBACK_UI_FPS", 12.0)))
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

        # Send throttling + stats (to reduce unnecessary network spam)
        self._last_sent_universes: Dict[int, List[int]] = {}
        self._last_sent_time: Dict[int, float] = {}
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
        server_time_ms = int(round(time.time() * 1000.0))
        state["server_time_ms"] = server_time_ms
        phase = str(state.get("phase") or "idle")
        remaining_ms = max(0, int(state.get("phase_remaining_ms", 0) or 0))
        if state.get("active") and not state.get("paused") and phase in ("waiting", "fading") and remaining_ms > 0:
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
            return max(1.0 / fps, 0.033)
        return 0.25

    def _effective_tick_hz_locked(self) -> float:
        if self._playback_state.get("active"):
            return max(self._tick_hz, float(self._playback_engine_hz or self._tick_hz))
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
                "smooth_targets": deepcopy(self._smooth_targets),
                "smooth_last_targets": deepcopy(self._smooth_last_targets),
                "live_effects": deepcopy(self._live_effects),
                "live_effect_groups": deepcopy(self._live_effect_groups),
                "live_groups_by_device": deepcopy(self._live_groups_by_device),
            }
        self._direct_channels.clear()
        self._smooth_targets.clear()
        self._smooth_last_targets.clear()
        self._live_effects.clear()
        self._live_effect_groups.clear()
        self._live_groups_by_device.clear()
        self._playback_force_backend_render = True

    def _restore_playback_render_locked(self):
        backup = self._playback_live_state_backup
        self._playback_force_backend_render = False
        if backup is not None:
            self._direct_channels = backup.get("direct_channels", {})
            self._smooth_targets = backup.get("smooth_targets", {})
            self._smooth_last_targets = backup.get("smooth_last_targets", {})
            self._live_effects = backup.get("live_effects", {})
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

    def _build_cue_payload_from_step(self, step: Dict[str, Any], virtual_groups: Any) -> Tuple[Dict[str, Any], List[str]]:
        devices = step.get("devices") or {}
        order_raw = step.get("device_order")
        if isinstance(order_raw, list) and order_raw:
            device_order = [str(x) for x in order_raw]
        else:
            device_order = [str(x) for x in devices.keys()]
        payload = {
            "devices": devices if isinstance(devices, dict) else {},
            "duration": step.get("duration", "0"),
            "effect_groups": self._resolve_effect_groups_for_step(step, virtual_groups),
        }
        return payload, device_order

    def _expand_playback_sequence(
        self,
        sequence: List[Dict[str, Any]],
        virtual_groups: Any = None,
        speed: float = 1.0,
    ) -> List[PlaybackPlanEntry]:
        out: List[PlaybackPlanEntry] = []
        if not isinstance(sequence, list):
            return out

        speed_factor = max(0.01, float(speed or 1.0))
        cursor_ms = 0
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
                i = group_end + 1
            else:
                indices = [i]
                i += 1

            for cue_index in indices:
                cue_step = sequence[cue_index]
                cue_payload, device_order = self._build_cue_payload_from_step(cue_step, virtual_groups)
                schedule = self._compute_schedule(cue_payload.get("duration", "0"), device_order)
                fade_ms_raw = int(max((end for _, end in schedule.values()), default=0))
                fade_ms = max(0, int(round(fade_ms_raw / speed_factor)))
                sleep_ms_raw = max(0, int(cue_step.get("sleep", 0) or 0))
                sleep_ms = max(0, int(round(sleep_ms_raw / speed_factor)))
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
                )
                out.append(entry)
                cursor_ms = entry.fade_end_at_ms
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
    def _device_group_index(group: Dict[str, Any], device_id: str) -> Tuple[int, int]:
        order = [str(x) for x in (group.get("deviceIds") or [])]
        sel = group.get("selection_groups")
        dev_key = str(device_id)
        if isinstance(sel, list) and sel:
            for gi, grp in enumerate(sel):
                if isinstance(grp, list) and dev_key in [str(x) for x in grp]:
                    return gi, len(sel)
            idx = order.index(dev_key) if dev_key in order else 0
            return idx, max(len(sel), 1)
        idx = order.index(dev_key) if dev_key in order else 0
        total = len(order) if order else 1
        return idx, total

    # -------------------------------------------------------------------------
    # THREAD CONTROL
    # -------------------------------------------------------------------------

    def start(self):
        """Start the render thread"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._render_loop, daemon=True)
        self._thread.start()
        log.info("Render thread started")

    def stop(self):
        """Stop the render thread"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
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

    def _ensure_universe(self, universe: int):
        if universe not in self._universes:
            self._universes[universe] = [0] * 512

    def set_render_mode(self, mode: str):
        """Set render mode (ui | backend)."""
        with self._lock:
            key = str(mode or "").strip().lower()
            next_mode = "backend" if key == "backend" else "ui"
            if next_mode == self._render_mode:
                return
            self._render_mode = next_mode

            # Reset state on mode switch to avoid stale DMX values
            if self._render_mode == "backend":
                # Clear UI-driven direct channels/effects so backend starts clean
                self._direct_channels.clear()
                self._smooth_targets.clear()
                self._smooth_last_targets.clear()
                self._fade = None
                self._cue_effects.clear()
                self._fade_effect_groups = None
                self._live_effects.clear()
            else:
                # Switching back to UI: stop backend fades/cues
                self._fade = None
                self._cue_effects.clear()
                self._fade_effect_groups = None

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

    def set_playback_speed(self, speed: Any):
        with self._lock:
            self._playback_speed = self._normalize_playback_speed(speed)
            if not self._playback_state.get("active"):
                self._playback_run_speed = self._playback_speed
                self._update_playback_state_locked(speed=self._playback_speed)

    def set_playback_ui_fps(self, fps: Any):
        with self._lock:
            try:
                value = float(fps)
            except Exception:
                value = 12.0
            self._playback_ui_fps = max(1.0, min(30.0, value))

    def set_playback_engine_hz(self, hz: Any):
        with self._lock:
            try:
                value = float(hz)
            except Exception:
                value = max(self._tick_hz, 120.0)
            self._playback_engine_hz = max(self._tick_hz, min(240.0, value))

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
        """Render one frame: calculate all values and send to ArtNet"""
        now = time.perf_counter()
        wall_now = time.time()
        frame_start = now

        with self._lock:
            self._cleanup_finished_fade_locked(now)

            # Render mode dispatch
            if self._playback_force_backend_render or self._render_mode == "backend":
                backend_start = time.perf_counter()
                self._render_backend_frame(now)
                self._record_perf("backend", (time.perf_counter() - backend_start) * 1000.0)
            else:
                self._render_ui_frame()

            # Apply smoothing for movement channels (pan/tilt)
            if self._smooth_targets:
                self._apply_smoothing()

            # Apply identify overlay (highest priority, overrides everything)
            if self._identify_devices or self._identify_data:
                self._render_identify(now)

            # Send to ArtNet
            if self.artnet:
                for uni_num, values in self._universes.items():
                    should_send, heartbeat_due = self._should_send_universe(uni_num, values, now)
                    if not should_send:
                        continue
                    if self._log_dmx:
                        if self._log_dmx_full:
                            log.debug("[DMX] universe=%s values=%s", uni_num, values)
                        else:
                            nonzero = [(i, v) for i, v in enumerate(values) if v]
                            sample = nonzero[:8]
                            log.debug(
                                "[DMX] universe=%s nonzero=%s sample=%s",
                                uni_num, len(nonzero), sample
                            )
                    values_to_send = self._apply_dummy_overlay(uni_num, values)
                    send_start = time.perf_counter()
                    if self._artnet_diff:
                        sent = self._send_artnet_diff(uni_num, values_to_send, heartbeat_due=heartbeat_due)
                    else:
                        self.artnet.send_universe(uni_num, values_to_send)
                        sent = True
                    self._record_perf("send", (time.perf_counter() - send_start) * 1000.0)
                    if sent:
                        self._packet_count += 1
                        self._last_send_ts = wall_now
                    self._record_send_stats(uni_num, values_to_send, now)

        self._maybe_log_stats(now)
        self._record_perf("render", (time.perf_counter() - frame_start) * 1000.0)
        self._maybe_log_perf(now)

    def _should_send_universe(self, universe: int, values: List[int], now: float) -> Tuple[bool, bool]:
        """Return True if we should send this universe at this tick."""
        last_values = self._last_sent_universes.get(universe)
        changed = (last_values != values)

        last_time = self._last_sent_time.get(universe, 0.0)
        heartbeat_due = (self._heartbeat_sec > 0 and (now - last_time) >= self._heartbeat_sec)

        if not changed and not heartbeat_due:
            self._stats["frames_skipped"] += 1
            return False, heartbeat_due

        min_send_interval = self._effective_min_send_interval_locked()
        if min_send_interval > 0 and (now - last_time) < min_send_interval:
            self._stats["frames_skipped"] += 1
            return False, heartbeat_due

        return True, heartbeat_due

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

        # Direct channels (live overrides)
        for u, ch_map in self._direct_channels.items():
            if u not in out:
                out[u] = {}
            for ch, val in ch_map.items():
                if 0 <= ch < 512:
                    out[u][ch] = int(val)

        return out

    def _render_ui_frame(self):
        """UI-render mode: apply direct channel values."""
        for uni_num, ch_map in self._direct_channels.items():
            self._ensure_universe(uni_num)
            uni = self._universes[uni_num]
            for ch, val in ch_map.items():
                if 0 <= ch < 512:
                    uni[ch] = val

    def _render_backend_frame(self, now: float):
        """Backend-render mode: compute base + effects per device."""
        base_map = self._build_base_universe_map(now, apply_fade=True)

        # Reuse universe buffers instead of reallocating a new map every tick.
        target_universes = set(self._universes.keys()) | set(base_map.keys())
        for uni_num in target_universes:
            self._ensure_universe(uni_num)
            self._universes[uni_num][:] = self._zero_universe

        for uni_num, ch_map in base_map.items():
            uni = self._universes[uni_num]
            for ch, val in ch_map.items():
                if 0 <= ch < 512:
                    uni[ch] = int(val)

        # Apply effect groups and legacy per-channel effects
        if self._live_effect_groups or self._cue_effect_groups or self._live_effects or self._cue_effects:
            speed = self._playback_run_speed if self._playback_state.get("active") else 1.0
            t_ms = (now - self._effect_epoch) * 1000.0 * max(0.01, float(speed or 1.0))
            device_ids = list(self._devices.keys())
            for dev_id, dev in self._devices.items():
                self._apply_effects_for_device(dev, dev_id, t_ms, device_ids, now)

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
            mode = str(group.get("mode") or "legacy").lower()
            idx, cnt = self._device_group_index(group, dev_id)

            if mode == "intelligent" and IntelligentFX:
                defn = IntelligentFX.get_effect_def(group.get("type"))
                if not defn:
                    return
                targets = IntelligentFX.normalize_targets(group.get("targets") or defn.get("targets"))
                if not targets:
                    return
                for target in targets:
                    ch = dev.attr_map.get(str(target).lower())
                    if ch is None or not (0 <= ch < 512):
                        continue
                    base_val = uni[ch]
                    ctx = {
                        "params": group,
                        "group": group,
                        "t_ms": t_ms,
                        "device_index": idx,
                        "device_count": cnt,
                        "device_id": dev_id,
                        "target": target,
                        "effect": defn,
                    }
                    raw = IntelligentFX.eval_effect(defn["id"], ctx)
                    uni[ch] = IntelligentFX.apply_effect_value(defn, base_val, raw, scale=mix)
                return

            # Legacy group
            attr = str(group.get("attrKey") or group.get("attr") or "").lower()
            if not attr:
                return
            ch = dev.attr_map.get(attr)
            if ch is None or not (0 <= ch < 512):
                return

            if mix <= 0:
                return

            scaled_group = dict(group)
            amp = float(group.get("amplitude", 0) or 0)
            scaled_group["amplitude"] = amp * mix
            delta = self._eval_legacy_group_delta(scaled_group, t_ms, dev_id, idx, cnt)
            uni[ch] = max(0, min(255, int(round(uni[ch] + delta))))

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
        if dev_id in self._live_effects:
            for ch, eff_list in self._live_effects[dev_id].items():
                for eff in eff_list:
                    offset = self._eval_effect(eff, now, dev_idx, dev_count)
                    if 0 <= ch < 512:
                        uni[ch] = max(0, min(255, int(uni[ch] + offset * 255)))

    def _render_device(self, dev: DeviceState, dev_id: str, now: float):
        """Render a single device's channels"""
        self._ensure_universe(dev.universe)
        uni = self._universes[dev.universe]

        # Get device index for phase spread
        device_ids = list(self._devices.keys())
        dev_idx = device_ids.index(dev_id) if dev_id in device_ids else 0
        dev_count = len(device_ids)

        for ch, base_value in dev.channels.items():
            if not (0 <= ch < 512):
                continue

            value = base_value

            # Apply fade if active
            if self._fade:
                value = self._apply_fade(dev.universe, ch, base_value, now, dev_id)

            # Apply cue effects
            if dev_id in self._cue_effects and ch in self._cue_effects[dev_id]:
                for eff in self._cue_effects[dev_id][ch]:
                    offset = self._eval_effect(eff, now, dev_idx, dev_count)
                    value = int(value + offset * 255)

            # Apply live effects
            if dev_id in self._live_effects and ch in self._live_effects[dev_id]:
                for eff in self._live_effects[dev_id][ch]:
                    offset = self._eval_effect(eff, now, dev_idx, dev_count)
                    value = int(value + offset * 255)

            # Clamp and store
            uni[ch] = max(0, min(255, value))

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

                    self._ensure_universe(universe)
                    uni = self._universes[universe]

                    if dimmer_ch is not None and 0 <= dimmer_ch < 512:
                        uni[dimmer_ch] = level
                return

            # Fallback: use registered devices
            for dev_id in self._identify_devices:
                if dev_id not in self._devices:
                    continue
                dev = self._devices[dev_id]
                self._ensure_universe(dev.universe)
                uni = self._universes[dev.universe]

                # Try dimmer first, then RGB
                for ch in dev.channels.keys():
                    # Override with blink
                    if 0 <= ch < 512:
                        uni[ch] = level


    # -------------------------------------------------------------------------
    # PUBLIC API: DEVICE MANAGEMENT
    # -------------------------------------------------------------------------

    def register_device(self, device_id: str, universe: int, channels: Dict[int, int]):
        """Register or update a device"""
        with self._lock:
            if device_id in self._devices:
                dev = self._devices[device_id]
                dev.universe = universe
                dev.channels = dict(channels)
            else:
                self._devices[device_id] = DeviceState(
                    device_id=device_id,
                    universe=universe,
                    channels=dict(channels)
                )
            self._ensure_universe(universe)

    def register_rig_devices(self, devices: List[Any]):
        """Register/update devices with attr_map from UI rig."""
        if not isinstance(devices, list):
            return
        with self._lock:
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

                if dev_id in self._devices:
                    dev = self._devices[dev_id]
                    dev.universe = universe
                    dev.base_address = address
                    if attr_map:
                        dev.attr_map = attr_map
                else:
                    self._devices[dev_id] = DeviceState(
                        device_id=dev_id,
                        universe=universe,
                        base_address=address,
                        channels={},
                        attr_map=attr_map
                    )
                self._ensure_universe(universe)

    def unregister_device(self, device_id: str):
        """Remove a device"""
        with self._lock:
            self._devices.pop(device_id, None)
            self._live_effects.pop(device_id, None)
            self._cue_effects.pop(device_id, None)

    def set_channel(self, device_id: str, universe: int, channel: int, value: int):
        """Set a single channel value (from controller) - uses direct channel storage"""
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
                self._ensure_universe(universe)
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

    def set_channels(self, device_id: str, universe: int, channels: Dict[int, int]):
        """Set multiple channel values - uses direct channel storage"""
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
                    self._ensure_universe(universe)
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

    def set_channels_bulk(self, updates: Dict[str, Dict[int, int]]):
        """Bulk update: {device_id: {channel: value}}"""
        with self._lock:
            for dev_id, channels in updates.items():
                if dev_id in self._devices:
                    for ch, val in channels.items():
                        self._devices[dev_id].channels[ch] = max(0, min(255, val))

    # -------------------------------------------------------------------------
    # PUBLIC API: LIVE EFFECTS
    # -------------------------------------------------------------------------

    def start_live_effect(self, device_id: str, channel: int, effect_type: str,
                          amplitude: float = 100, frequency: float = 1.0,
                          phase: Any = 0, **params):
        """Start a live effect on a device channel"""
        with self._lock:
            if device_id not in self._live_effects:
                self._live_effects[device_id] = {}
            if channel not in self._live_effects[device_id]:
                self._live_effects[device_id][channel] = []

            eff = LiveEffect(
                effect_type=effect_type,
                amplitude=amplitude,
                frequency=frequency,
                phase=phase,
                params=params,
                start_time=time.perf_counter()
            )
            self._live_effects[device_id][channel].append(eff)

    def stop_live_effects(self, device_id: str, channel: Optional[int] = None):
        """Stop live effects on a device (optionally specific channel)"""
        with self._lock:
            if device_id in self._live_effects:
                if channel is not None:
                    self._live_effects[device_id].pop(channel, None)
                else:
                    del self._live_effects[device_id]

    def clear_all_live_effects(self):
        """Clear all live effects"""
        with self._lock:
            self._live_effects.clear()

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
            else:
                normalized = self._normalize_groups(groups)
                if action_key == "add":
                    self._live_effect_groups.update(normalized)
                else:
                    self._live_effect_groups = normalized

            self._live_groups_by_device = self._build_group_device_map(self._live_effect_groups)

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
        duration_field = duration_override if duration_override is not None else cue_data.get("duration", "0")
        effect_groups_raw = cue_data.get("effect_groups") or cue_data.get("effectGroups") or []
        now = float(start_time) if start_time is not None else time.perf_counter()

        start_values = self._build_base_universe_map(now, apply_fade=True)
        end_values: Dict[int, Dict[int, int]] = self._build_base_universe_map(now, apply_fade=False)
        new_cue_effects: Dict[str, Dict[int, List[LiveEffect]]] = {}

        ordered_ids = [str(x) for x in (device_order or list(devices.keys()))]

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

        new_groups = self._normalize_groups(effect_groups_raw)
        group_pool = dict(prev_group_pool)
        group_pool.update(new_groups)
        self._cue_effect_groups = group_pool

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
            self._update_playback_state_locked(paused=bool(paused))
        self._notify_state_change()

    def skip_playback_step(self):
        with self._lock:
            if not self._playback_state.get("active"):
                return
            self._playback_skip_requested = True
            self._fade = None
            self._update_playback_state_locked(wait_remaining_ms=0)
        self._notify_state_change()

    def adjust_playback_wait(self, delta_ms: int):
        with self._lock:
            if not self._playback_state.get("active"):
                return
            if str(self._playback_state.get("phase") or "") != "waiting":
                return
            self._playback_wait_adjust_ms += int(delta_ms)
            self._update_playback_state_locked()
        self._notify_state_change()

    def run_sequence(self, sequence: List[Dict[str, Any]], start_index: int = 0, virtual_groups: Any = None, speed: Any = 1.0):
        if not isinstance(sequence, list):
            raise ValueError("sequence must be a list")

        cleaned = [step for step in sequence if isinstance(step, dict)]
        resolved_start_index = self._resolve_playback_start_index(cleaned, int(start_index or 0)) if cleaned else 0
        run_speed = self._normalize_playback_speed(speed if speed is not None else self._playback_speed)
        full_plan = self._expand_playback_sequence(cleaned, virtual_groups=virtual_groups, speed=run_speed)
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
                    target=self._play_sequence,
                    args=(run_plan, len(full_plan)),
                    daemon=True,
                    name="DMXPlaybackScheduler",
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
        elif action_key in ("adjust_wait", "adjust"):
            self.adjust_playback_wait(delta_ms)
        elif action_key == "stop":
            self.stop_playback()
        else:
            raise ValueError(f"unknown playback action: {action}")

    def _play_sequence(self, sequence: List[PlaybackPlanEntry], sequence_length: int):
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

                cue_token += 1
                with self._lock:
                    self._playback_wait_adjust_ms = 0

                if wait_ms > 0:
                    with self._lock:
                        self._update_playback_state_locked(
                            active=True,
                            paused=False,
                            phase="waiting",
                            cue_index=cue_index,
                            plan_index=absolute_plan_index,
                            cue_name=entry.cue_name,
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
                                phase="waiting",
                                cue_index=cue_index,
                                plan_index=absolute_plan_index,
                                cue_name=entry.cue_name,
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

            with self._lock:
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

    def _outside_in(self, items: List[str]) -> List[str]:
        """Reorder: first, last, second, second-to-last, ..."""
        result = []
        left, right = 0, len(items) - 1
        while left <= right:
            if left == right:
                result.append(items[left])
            else:
                result.append(items[left])
                result.append(items[right])
            left += 1
            right -= 1
        return result

    # -------------------------------------------------------------------------
    # STATE BROADCASTING (for SSE)
    # -------------------------------------------------------------------------

    def add_state_callback(self, callback: Callable):
        """Add callback for state updates"""
        self._state_callbacks.append(callback)

    def remove_state_callback(self, callback: Callable):
        """Remove state callback"""
        if callback in self._state_callbacks:
            self._state_callbacks.remove(callback)

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
                state["universes"] = {str(u): list(v) for u, v in self._universes.items()}

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
            self._ensure_universe(uni)
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


    def get_current_state(self) -> Dict[str, Any]:
        """Get current state snapshot"""
        with self._lock:
            return {
                "universes": {str(u): list(v) for u, v in self._universes.items()},
                "devices": {d: {"universe": dev.universe, "channels": dict(dev.channels)}
                           for d, dev in self._devices.items()},
                "identify_active": bool(self._identify_devices),
                "fade_active": self._fade is not None,
                "playback": self._playback_state_snapshot_locked()
            }

    def get_packet_stats(self) -> Dict[str, Any]:
        return {
            "artnet_packets": self._packet_count,
            "last_send_ts": self._last_send_ts
        }
