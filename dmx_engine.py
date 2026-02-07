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

        # Movement smoothing (pan/tilt) - channels provided by UI
        self._smooth_channels: Dict[int, set] = {}  # {universe: {channel}}
        self._smooth_targets: Dict[int, Dict[int, int]] = {}  # {universe: {channel: target}}
        self._smooth_step = int(self._read_env_float("DMX_SMOOTH_STEP", 2))

        # Dummy channels (keepalive for server mods)
        self._dummy_channels: Dict[int, List[int]] = {}  # {universe: [channels]}
        self._dummy_state: Dict[int, int] = {}  # {universe: 0/255}
        self._dummy_enabled = os.environ.get("DMX_DUMMY", "1").strip().lower() in ("1", "true", "yes", "on")

        # Live effects (from controller, not cues)
        self._live_effects: Dict[str, Dict[int, List[LiveEffect]]] = {}  # {device_id: {channel: [effects]}}

        # Identify mode
        self._identify_devices: List[str] = []
        self._identify_data: List[Dict[str, Any]] = []  # Direct channel info from JS
        self._identify_start: float = 0.0

        # Cue playback
        self._fade: Optional[FadeState] = None
        self._cue_effects: Dict[str, Dict[int, List[LiveEffect]]] = {}  # effects from current cue

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

        self._max_send_hz = self._read_env_float("DMX_MAX_SEND_HZ", self.TICK_HZ)
        self._min_send_interval = 0.0 if self._max_send_hz <= 0 else (1.0 / self._max_send_hz)

        # Keepalive to avoid fixtures timing out (send even if unchanged)
        self._heartbeat_sec = self._read_env_float("DMX_HEARTBEAT_SEC", 0.1)
        self._stats_interval_sec = self._read_env_float("DMX_STATS_SEC", 1.0)

        # Value filtering (reduce jitter)
        self._deadband = int(self._read_env_float("DMX_DEADBAND", 0))
        self._quantize = int(self._read_env_float("DMX_QUANTIZE", 1))

        self._stats_last_log = time.time()
        self._stats = {
            "frames_sent": 0,
            "universes_sent": 0,
            "frames_skipped": 0,
            "bytes_sent": 0,
        }

        log.info(
            "DMXRenderEngine initialized (max_send_hz=%s heartbeat_sec=%s stats_sec=%s)",
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

    # -------------------------------------------------------------------------
    # RENDER LOOP
    # -------------------------------------------------------------------------

    def _render_loop(self):
        """Main render loop running at TICK_HZ"""
        tick_interval = 1.0 / self.TICK_HZ

        while self._running:
            tick_start = time.perf_counter()

            try:
                self._render_frame()
            except Exception as e:
                log.error(f"Render error: {e}")

            # Broadcast state every 100ms for UI
            now = time.time()
            if now - self._last_state_broadcast > 0.1:
                self._broadcast_state()
                self._last_state_broadcast = now

            # Sleep for remaining time
            elapsed = time.perf_counter() - tick_start
            sleep_time = tick_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    def _render_frame(self):
        """Render one frame: calculate all values and send to ArtNet"""
        now = time.time()

        with self._lock:
            # Apply direct channel values (from live UI edits / JS fades)
            # This is the main source of DMX data - JS handles all fades and effects
            for uni_num, ch_map in self._direct_channels.items():
                self._ensure_universe(uni_num)
                uni = self._universes[uni_num]
                for ch, val in ch_map.items():
                    if 0 <= ch < 512:
                        uni[ch] = val

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
                    if self._artnet_diff:
                        sent = self._send_artnet_diff(uni_num, values_to_send, heartbeat_due=heartbeat_due)
                    else:
                        self.artnet.send_universe(uni_num, values_to_send)
                        sent = True
                    if sent:
                        self._packet_count += 1
                        self._last_send_ts = now
                    self._record_send_stats(uni_num, values_to_send, now)

        self._maybe_log_stats(now)

    def _should_send_universe(self, universe: int, values: List[int], now: float) -> Tuple[bool, bool]:
        """Return True if we should send this universe at this tick."""
        last_values = self._last_sent_universes.get(universe)
        changed = (last_values != values)

        last_time = self._last_sent_time.get(universe, 0.0)
        heartbeat_due = (self._heartbeat_sec > 0 and (now - last_time) >= self._heartbeat_sec)

        if not changed and not heartbeat_due:
            self._stats["frames_skipped"] += 1
            return False, heartbeat_due

        if self._min_send_interval > 0 and (now - last_time) < self._min_send_interval:
            self._stats["frames_skipped"] += 1
            return False, heartbeat_due

        return True, heartbeat_due

    def _record_send_stats(self, universe: int, values: List[int], now: float):
        self._last_sent_universes[universe] = values[:]
        self._last_sent_time[universe] = now
        self._stats["frames_sent"] += 1
        self._stats["universes_sent"] += 1
        self._stats["bytes_sent"] += len(values)

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

        elapsed_ms = (now - fade.start_time) * 1000

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

        t_s = now - eff.start_time
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
            if self._is_smooth_channel(universe, channel):
                self._ensure_universe(universe)
                cur = self._universes[universe][channel]
                if self._should_bypass_smoothing(cur, v):
                    self._direct_channels[universe][channel] = v
                    self._smooth_targets.get(universe, {}).pop(channel, None)
                else:
                    self._smooth_targets.setdefault(universe, {})[channel] = v
            else:
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
                if self._is_smooth_channel(universe, ch_int):
                    self._ensure_universe(universe)
                    cur = self._universes[universe][ch_int]
                    if self._should_bypass_smoothing(cur, v):
                        self._direct_channels[universe][ch_int] = v
                        self._smooth_targets.get(universe, {}).pop(ch_int, None)
                    else:
                        self._smooth_targets.setdefault(universe, {})[ch_int] = v
                else:
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
                start_time=time.time()
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

            self._identify_start = time.time()
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

    def go_cue(self, cue_data: Dict[str, Any], device_order: Optional[List[str]] = None):
        """
        Execute a cue with fade.
        cue_data: {"devices": {id: {"channels": {ch: val}, "effects": {...}}}, "duration": "100 > 500"}
        """
        devices = cue_data.get("devices", {})
        duration_field = cue_data.get("duration", "0")

        with self._lock:
            # Snapshot current values as start
            start_values: Dict[int, Dict[int, int]] = {}
            for uni, vals in self._universes.items():
                start_values[uni] = {i: vals[i] for i in range(512)}

            # Parse end values and effects
            end_values: Dict[int, Dict[int, int]] = {}
            new_cue_effects: Dict[str, Dict[int, List[LiveEffect]]] = {}

            ordered_ids = device_order or list(devices.keys())

            for dev_id, dev_spec in devices.items():
                if not isinstance(dev_spec, dict):
                    continue

                channels = dev_spec.get("channels", {})
                universe = int(channels.get("Universe", 0))

                if universe not in end_values:
                    end_values[universe] = {}

                # Update device state with target values
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

                # Parse effects
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
                                    start_time=time.time()
                                ))

            # Calculate schedule
            schedule = self._compute_schedule(duration_field, ordered_ids)
            total_ms = max((end for _, end in schedule.values()), default=0)

            self._cue_effects = new_cue_effects

            if total_ms > 0:
                self._fade = FadeState(
                    start_values=start_values,
                    end_values=end_values,
                    start_time=time.time(),
                    duration_ms=total_ms,
                    schedule=schedule
                )
                log.info(f"Cue started with fade {total_ms}ms")
            else:
                self._fade = None
                log.info("Cue applied (cut)")

    def stop_playback(self):
        """Stop current cue playback"""
        with self._lock:
            self._fade = None
            self._cue_effects.clear()
        log.info("Playback stopped")

    def _compute_schedule(self, duration_field: Any, device_ids: List[str]) -> Dict[str, tuple]:
        """Compute per-device fade schedule from duration field"""
        import re
        import random

        s = str(duration_field or "0").strip()
        if not s:
            return {d: (0, 0) for d in device_ids}

        randomize = "?" in s
        mode = None
        if "||" in s:
            mode = "||"
        elif "|" in s:
            mode = "|"
        elif ">" in s:
            mode = ">"
        elif "<" in s:
            mode = "<"

        nums = [int(x) for x in re.findall(r"\d+", s)]
        if len(nums) == 0:
            return {d: (0, 0) for d in device_ids}
        elif len(nums) == 1:
            # Simple fade, all together
            return {d: (0, nums[0]) for d in device_ids}

        fade_ms, last_end = nums[0], nums[1]
        n = len(device_ids)
        if n == 0:
            return {}

        ordered = list(device_ids)
        if randomize:
            random.shuffle(ordered)

        if mode == "<":
            ordered = list(reversed(ordered))
        elif mode == "||":
            ordered = self._outside_in(ordered)
        elif mode == "|":
            ordered = list(reversed(self._outside_in(ordered)))

        if n == 1:
            return {ordered[0]: (0, fade_ms)}

        schedule = {}
        spread = last_end - fade_ms
        for i, dev_id in enumerate(ordered):
            ratio = i / (n - 1)
            end_i = fade_ms + int(spread * ratio)
            start_i = max(0, end_i - fade_ms)
            schedule[dev_id] = (start_i, end_i)

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

    def _broadcast_state(self):
        """Send current state to all callbacks"""
        if not self._state_callbacks:
            return

        with self._lock:
            state = {
                "universes": {str(u): list(v) for u, v in self._universes.items()},
                "identify_active": bool(self._identify_devices),
                "fade_active": self._fade is not None,
                "timestamp": time.time()
            }

        for cb in self._state_callbacks:
            try:
                cb(state)
            except Exception as e:
                log.error(f"State callback error: {e}")

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
                "fade_active": self._fade is not None
            }

    def get_packet_stats(self) -> Dict[str, Any]:
        return {
            "artnet_packets": self._packet_count,
            "last_send_ts": self._last_send_ts
        }
