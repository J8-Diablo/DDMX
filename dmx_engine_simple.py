#!/usr/bin/env python3
"""
dmx_engine_simple.py - Minimal DMX engine (from-scratch path)

Goals:
- Only send ArtNet when values change (plus optional heartbeat).
- No cues / no fades / no effects (UI handles them).
- Optional identify overlay (blink) for troubleshooting.
"""

import threading
import time
import math
import logging
import os
from typing import Dict, List, Any, Optional, Callable

try:
    from DMXE import DMXEngine as ArtNetSender
except ImportError:
    ArtNetSender = None

log = logging.getLogger("DMXSimpleEngine")
_level_name = os.environ.get("DMX_ENGINE_LOG_LEVEL", "INFO").upper()
log.setLevel(getattr(logging, _level_name, logging.INFO))


class DMXSimpleEngine:
    """
    Very small DMX engine:
    - Stores absolute channel values per universe (512)
    - Sends ArtNet only when values change
    - Optional heartbeat to keep fixtures alive
    """

    TICK_HZ = 40  # 25ms

    def __init__(self, artnet_ip: str = "127.0.0.1", bind_ip: str = "0.0.0.0"):
        self.artnet: Optional[ArtNetSender] = None
        if ArtNetSender:
            try:
                self.artnet = ArtNetSender(target_ip=artnet_ip, bind_iface=bind_ip, broadcast=False)
                log.info("ArtNet initialized: %s", artnet_ip)
            except Exception as e:
                log.error("ArtNet init failed: %s", e)

        self._lock = threading.RLock()
        self._universes: Dict[int, List[int]] = {}
        self._dirty: set[int] = set()

        # Identify overlay
        self._identify_data: List[Dict[str, Any]] = []
        self._identify_start: float = 0.0

        # Threading
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # State callbacks (SSE)
        self._state_callbacks: List[Callable] = []
        self._last_state_broadcast: float = 0.0

        # Debug flags
        self._log_dmx = os.environ.get("DMX_LOG_DMX", "0").strip().lower() in ("1", "true", "yes", "on")
        self._log_dmx_full = os.environ.get("DMX_LOG_DMX_FULL", "0").strip().lower() in ("1", "true", "yes", "on")

        # Throttle / heartbeat
        self._max_send_hz = self._read_env_float("DMX_MAX_SEND_HZ", self.TICK_HZ)
        self._min_send_interval = 0.0 if self._max_send_hz <= 0 else (1.0 / self._max_send_hz)
        self._heartbeat_sec = self._read_env_float("DMX_HEARTBEAT_SEC", 0.1)
        self._last_sent_time: Dict[int, float] = {}
        self._gap_warn_ms = self._read_env_float("DMX_GAP_WARN_MS", 150.0)
        self._last_gap_warn: Dict[int, float] = {}
        self._last_sent_universes: Dict[int, List[int]] = {}
        self._artnet_diff = os.environ.get("DMX_ARTNET_DIFF", "0").strip().lower() in ("1", "true", "yes", "on")
        self._artnet_heartbeat_full = os.environ.get("DMX_ARTNET_HEARTBEAT_FULL", "1").strip().lower() in ("1", "true", "yes", "on")
        self._packet_count = 0
        self._last_send_ts = 0.0

        # Value filtering (reduce jitter)
        self._deadband = int(self._read_env_float("DMX_DEADBAND", 0))
        self._quantize = int(self._read_env_float("DMX_QUANTIZE", 1))

        # Movement smoothing (pan/tilt) - channels provided by UI
        self._smooth_channels: Dict[int, set] = {}
        self._smooth_targets: Dict[int, Dict[int, int]] = {}
        self._smooth_step = int(self._read_env_float("DMX_SMOOTH_STEP", 2))

        # Dummy channels (keepalive for server mods)
        self._dummy_channels: Dict[int, List[int]] = {}
        self._dummy_state: Dict[int, int] = {}
        self._dummy_enabled = os.environ.get("DMX_DUMMY", "1").strip().lower() in ("1", "true", "yes", "on")

        # Force continuous full-universe sends (for receivers that require constant stream)
        self._force_continuous = self._read_env_bool("DMX_CONTINUOUS", False)

        # Test mode: force a static channel value, ignore UI updates
        self._test_mode = self._read_env_bool("DMX_TEST_MODE", False)
        self._test_universe = int(self._read_env_float("DMX_TEST_UNIVERSE", 0))
        self._test_channel = int(self._read_env_float("DMX_TEST_CHANNEL", 0))
        self._test_value = int(self._read_env_float("DMX_TEST_VALUE", 255))

        log.info(
            "DMXSimpleEngine initialized (max_send_hz=%s heartbeat_sec=%s continuous=%s test_mode=%s gap_warn_ms=%s)",
            self._max_send_hz,
            self._heartbeat_sec,
            self._force_continuous,
            self._test_mode,
            self._gap_warn_ms,
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
    def _read_env_bool(name: str, default: bool) -> bool:
        raw = os.environ.get(name)
        if raw is None:
            return bool(default)
        return str(raw).strip().lower() in ("1", "true", "yes", "on")

    def _ensure_universe(self, universe: int):
        if universe not in self._universes:
            self._universes[universe] = [0] * 512

    # ------------------------------------------------------------------
    # Thread control
    # ------------------------------------------------------------------

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._render_loop, daemon=True)
        self._thread.start()
        log.info("Render thread started")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        log.info("Render thread stopped")

    # ------------------------------------------------------------------
    # Public API (compat)
    # ------------------------------------------------------------------

    def set_channel(self, device_id: str, universe: int, channel: int, value: int):
        if self._test_mode:
            return
        with self._lock:
            self._ensure_universe(universe)
            if 0 <= channel < 512:
                val = self._filter_value(universe, channel, value)
                if val is None:
                    return
                if self._is_smooth_channel(universe, channel):
                    cur = self._universes[universe][channel]
                    if self._should_bypass_smoothing(cur, val):
                        if self._universes[universe][channel] != val:
                            self._universes[universe][channel] = val
                            self._dirty.add(universe)
                        self._smooth_targets.get(universe, {}).pop(channel, None)
                    else:
                        self._smooth_targets.setdefault(universe, {})[channel] = val
                elif self._universes[universe][channel] != val:
                    self._universes[universe][channel] = val
                    self._dirty.add(universe)

    def set_channels(self, device_id: str, universe: int, channels: Dict[int, int]):
        if self._test_mode:
            return
        with self._lock:
            self._ensure_universe(universe)
            changed = False
            for ch, val in channels.items():
                ch_int = int(ch) if isinstance(ch, str) else ch
                if 0 <= ch_int < 512:
                    v = self._filter_value(universe, ch_int, val)
                    if v is None:
                        continue
                    if self._is_smooth_channel(universe, ch_int):
                        cur = self._universes[universe][ch_int]
                        if self._should_bypass_smoothing(cur, v):
                            if self._universes[universe][ch_int] != v:
                                self._universes[universe][ch_int] = v
                                changed = True
                            self._smooth_targets.get(universe, {}).pop(ch_int, None)
                        else:
                            self._smooth_targets.setdefault(universe, {})[ch_int] = v
                            changed = True
                    elif self._universes[universe][ch_int] != v:
                        self._universes[universe][ch_int] = v
                        changed = True
            if changed:
                self._dirty.add(universe)

    def set_channels_bulk(self, updates: Dict[str, Dict[int, int]]):
        # Compatibility stub; we don't track devices here.
        for _dev_id, ch_map in (updates or {}).items():
            # No universe in this API, so skip (not used by current UI)
            pass

    def register_device(self, device_id: str, universe: int, channels: Dict[int, int]):
        # Compatibility stub
        pass

    def unregister_device(self, device_id: str):
        # Compatibility stub
        pass

    def go_cue(self, cue_data: Dict[str, Any], device_order: Optional[List[str]] = None):
        # Compatibility stub: apply immediate values if present
        devices = cue_data.get("devices", {}) if isinstance(cue_data, dict) else {}
        for dev_id, dev_spec in devices.items():
            if not isinstance(dev_spec, dict):
                continue
            channels = dev_spec.get("channels", {}) or {}
            universe = int(channels.get("Universe", 0))
            chmap = {k: v for k, v in channels.items() if str(k).lower() != "universe"}
            self.set_channels(dev_id, universe, chmap)

    def stop_playback(self):
        # Compatibility stub
        pass

    def start_identify(self, devices: List[Any]):
        if self._test_mode:
            return
        with self._lock:
            self._identify_data = []
            for dev in devices or []:
                if isinstance(dev, dict):
                    # expect {universe, dimmer_channel}
                    self._identify_data.append(dev)
            self._identify_start = time.time()

    def stop_identify(self):
        if self._test_mode:
            return
        with self._lock:
            self._identify_data = []

    def set_movement_channels(self, universes: Dict[int, List[int]]):
        """Set movement (pan/tilt) channels by universe."""
        with self._lock:
            self._smooth_channels = {int(u): set(map(int, chs)) for u, chs in (universes or {}).items()}
            # Remove smooth channels from direct values to avoid overriding smoothing
            for u, chs in self._smooth_channels.items():
                uni = self._universes.get(u)
                if not uni:
                    continue
                for ch in chs:
                    if 0 <= ch < 512:
                        # keep current value, no change needed
                        pass

    def set_dummy_channels(self, universes: Dict[int, List[int]]):
        """Set dummy channels used to force updates."""
        with self._lock:
            if not self._dummy_enabled:
                self._dummy_channels = {}
                self._dummy_state = {}
                return
            self._dummy_channels = {int(u): [int(c) for c in chs] for u, chs in (universes or {}).items()}
            self._dummy_state = {int(u): 0 for u in self._dummy_channels.keys()}

    def add_state_callback(self, callback: Callable):
        self._state_callbacks.append(callback)

    def remove_state_callback(self, callback: Callable):
        if callback in self._state_callbacks:
            self._state_callbacks.remove(callback)

    def get_current_state(self) -> Dict[str, Any]:
        with self._lock:
            return {"universes": {str(u): list(v) for u, v in self._universes.items()}}

    def get_packet_stats(self) -> Dict[str, Any]:
        return {
            "artnet_packets": self._packet_count,
            "last_send_ts": self._last_send_ts
        }

    def set_artnet_target(self, ip: str) -> bool:
        if not self.artnet or not ip:
            return False
        try:
            setattr(self.artnet, "target_ip", ip)
            log.info("ArtNet target updated: %s", ip)
            return True
        except Exception as e:
            log.error("Failed to update ArtNet target: %s", e)
            return False

    # ------------------------------------------------------------------
    # Render loop
    # ------------------------------------------------------------------

    def _render_loop(self):
        tick_interval = 1.0 / self.TICK_HZ
        while self._running:
            tick_start = time.perf_counter()
            now = time.time()

            try:
                self._render_frame(now)
            except Exception as e:
                log.error("Render error: %s", e)

            # Broadcast state every 100ms for UI
            if now - self._last_state_broadcast > 0.1:
                self._broadcast_state()
                self._last_state_broadcast = now

            elapsed = time.perf_counter() - tick_start
            sleep_time = tick_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    def _render_frame(self, now: float):
        if not self.artnet:
            return

        if self._test_mode:
            self._ensure_universe(self._test_universe)
            with self._lock:
                buf = self._universes[self._test_universe]
                for i in range(512):
                    buf[i] = 0
                if 0 <= self._test_channel < 512:
                    buf[self._test_channel] = max(0, min(255, self._test_value))
                self._dirty.add(self._test_universe)

        # Apply smoothing before sending
        if self._smooth_targets:
            self._apply_smoothing()

        with self._lock:
            universes = list(self._universes.items())
            dirty = set(self._dirty)
            identify_data = list(self._identify_data)

        for uni_num, base_values in universes:
            last_time = self._last_sent_time.get(uni_num, 0.0)
            if self._gap_warn_ms > 0 and last_time > 0:
                gap_ms = (now - last_time) * 1000.0
                if gap_ms >= self._gap_warn_ms:
                    last_warn = self._last_gap_warn.get(uni_num, 0.0)
                    if (now - last_warn) > 1.0:
                        log.warning("DMX gap detected: universe=%s gap_ms=%.1f", uni_num, gap_ms)
                        self._last_gap_warn[uni_num] = now
            heartbeat_due = self._heartbeat_sec > 0 and (now - last_time) >= self._heartbeat_sec

            needs_send = (
                self._force_continuous
                or self._test_mode
                or (uni_num in dirty)
                or heartbeat_due
                or bool(identify_data)
            )
            if not needs_send:
                continue

            if (
                not self._force_continuous
                and not self._test_mode
                and self._min_send_interval > 0
                and (now - last_time) < self._min_send_interval
            ):
                continue

            values = base_values
            if identify_data:
                values = self._apply_identify_overlay(uni_num, base_values, now, identify_data)

            values = self._apply_dummy_overlay(uni_num, values)

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
            if self._artnet_diff:
                sent = self._send_artnet_diff(uni_num, values, heartbeat_due=heartbeat_due)
            else:
                self.artnet.send_universe(uni_num, values)
                sent = True
            if sent:
                self._packet_count += 1
                self._last_send_ts = now
            self._last_sent_time[uni_num] = now
            self._last_sent_universes[uni_num] = values[:]

            with self._lock:
                if uni_num in self._dirty:
                    self._dirty.discard(uni_num)

    def _apply_identify_overlay(
        self,
        universe: int,
        base_values: List[int],
        now: float,
        identify_data: List[Dict[str, Any]],
    ) -> List[int]:
        # 2Hz blink
        elapsed = now - self._identify_start
        blink = (math.sin(elapsed * 4 * math.pi) + 1) / 2
        level = int(255 * blink)

        out = list(base_values)
        for dev in identify_data:
            try:
                uni = int(dev.get("universe", 0))
            except Exception:
                uni = 0
            if uni != universe:
                continue
            ch = dev.get("dimmer_channel")
            if ch is None:
                continue
            try:
                ch = int(ch)
            except Exception:
                continue
            if 0 <= ch < 512:
                out[ch] = level
        return out

    def _apply_dummy_overlay(self, universe: int, values: List[int]) -> List[int]:
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

        self.artnet.send_channels(universe, diff)
        return True

    def _is_smooth_channel(self, universe: int, channel: int) -> bool:
        return channel in self._smooth_channels.get(universe, set())

    def _apply_smoothing(self):
        step = max(1, int(self._smooth_step))
        for uni, targets in list(self._smooth_targets.items()):
            if not targets:
                continue
            self._ensure_universe(uni)
            uni_buf = self._universes[uni]
            changed = False
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
                    changed = True
                    to_remove.append(ch)
                else:
                    uni_buf[ch] = cur + step if diff > 0 else cur - step
                    changed = True
            for ch in to_remove:
                targets.pop(ch, None)
            if changed:
                self._dirty.add(uni)

    @staticmethod
    def _should_bypass_smoothing(cur: int, target: int) -> bool:
        return (cur == 0 and target == 255) or (cur == 255 and target == 0)

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

        current = self._universes.get(universe)
        if current is not None and 0 <= channel < len(current):
            prev = current[channel]
            if self._deadband and abs(v - prev) <= self._deadband:
                return None
        return v

    def _broadcast_state(self):
        if not self._state_callbacks:
            return
        with self._lock:
            state = {
                "universes": {str(u): list(v) for u, v in self._universes.items()},
                "identify_active": bool(self._identify_data),
                "timestamp": time.time(),
            }
        for cb in self._state_callbacks:
            try:
                cb(state)
            except Exception as e:
                log.error("State callback error: %s", e)
