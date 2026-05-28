#!/usr/bin/env python3

import asyncio
import colorsys
import logging
import math
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from audio_analyzer import AudioAnalyzer, DEFAULT_AUDIO_TUNING
from autolight_director import DirectorOverlay
from autolight_effects import EffectContext, EffectScheduler, all_mood_tags, list_effects_meta
from autolight_topology import TopologySnapshot, compute_topology
from autolight_training import TrainingService
from music_sources import MusicContext
# AutoLight 2.0 pipeline (see AUTOLIGHT-REWRITE-DESIGN.md).
from autolight_beatgrid import BeatGrid
from autolight_brain import MusicBrain
from autolight_show import ShowRenderer


log = logging.getLogger(__name__)

_GOLDEN_HUE_STEP = 0.381966011  # 1 - 1/phi, golden-ratio conjugate
_BEAT_PULSE_HALFLIFE_S = 0.18
_SCENE_DWELL_S = 0.25
_DEFAULT_PAN_RANGE_DEG = 540.0   # fallback when the mover doesn't expose range
_STROBE_HZ = 12.0                # square-wave cadence for DROP strobes

# Per-scene policy driving fixture selection and output. ``participation``
# values are advisory — the active ``pattern`` is what actually decides which
# fixtures pulse on a given frame.
_SCENE_POLICY: Dict[str, Dict[str, Any]] = {
    "SILENT":  {"mv_amp_deg": 0,  "mv_freq_hz": 0.00, "strobe": False, "pattern": "all",               "base_dimmer": 0,   "energy_gain": 0.0, "hue_spread": 0.00, "ceiling": 0},
    "VERSE":   {"mv_amp_deg": 10, "mv_freq_hz": 0.12, "strobe": False, "pattern": "chaser_by_x",       "base_dimmer": 25,  "energy_gain": 0.6, "hue_spread": 0.15, "ceiling": 150},
    "CHORUS":  {"mv_amp_deg": 20, "mv_freq_hz": 0.25, "strobe": False, "pattern": "mirror_alternate",  "base_dimmer": 45,  "energy_gain": 0.8, "hue_spread": 0.30, "ceiling": 215},
    "HIGH":    {"mv_amp_deg": 30, "mv_freq_hz": 0.40, "strobe": False, "pattern": "chaser_by_x",       "base_dimmer": 75,  "energy_gain": 1.0, "hue_spread": 0.45, "ceiling": 245},
    "DROP":    {"mv_amp_deg": 45, "mv_freq_hz": 0.90, "strobe": True,  "pattern": "antisymmetric",     "base_dimmer": 120, "energy_gain": 1.0, "hue_spread": 0.70, "ceiling": 255},
}

_LEVEL_TO_SCENE = {0: "SILENT", 1: "VERSE", 2: "CHORUS", 3: "HIGH", 4: "DROP"}


def _role_channels(attr_map: Dict[str, int]) -> Dict[str, int]:
    """Collect dimmer/R/G/B channel indexes from a device attr_map.

    Supports both the legacy flat keys ("dimmer", "r", "g", "b") and the
    grouped structured keys ("<group>.level", "<group>.red", ...). When
    multiple groups exist we take the first one sorted by lowest channel.
    """
    result: Dict[str, int] = {}
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
        else:
            continue

        bucket = grouped.setdefault(group_id, {})
        bucket[canonical] = channel
        prev = group_min.get(group_id)
        if prev is None or channel < prev:
            group_min[group_id] = channel

    ordered_groups = sorted(grouped.keys(), key=lambda gid: (group_min.get(gid, 0), gid))
    for gid in ordered_groups:
        for canonical, channel in grouped[gid].items():
            result.setdefault(canonical, channel)
    return result


DEFAULT_AUTOLIGHT_SETTINGS: Dict[str, Any] = {
    "enabled": False,
    "mode": "live",
    "source_mode": "player_metadata_then_local",
    "override_timeout_ms": 5000,
    "confidence_threshold": 0.75,
    "allow_guarded_channels": False,
    "snapshot_auto_capture": False,
    "energy_sensitivity": 1.0,
    "movement_sensitivity": 1.0,
    "freeze_global": False,
    "audio_device_index": None,
    "soundcloud_client_id": "",
    "effect_config": {},
    "effects_only": False,
    "audio_tuning": dict(DEFAULT_AUDIO_TUNING),
    "mood_filter": [],
    "bpm_confidence_gate": 0.0,
    "genre_preset": "auto",
    "tap_tempo_bpm": 0.0,
    "scene_lock": {"scene": "", "until_ts_ms": 0},
    # Render pipeline selector. "director" runs the new MusicDirector +
    # per-fixture FixtureAgent system. "effects" runs the legacy scene engine
    # + effect scheduler. "off" leaves the rig untouched (channels released).
    "render_mode": "director",
    # When True, the director persists per-track signatures (peak BPM, intent
    # transitions) to DATA_DIR/autolight_memory.json so it can pre-position
    # itself on replays.
    "memory_persistence": False,
    # Structural prior — when "auto", the director uses song position +
    # genre template (or a learned replay prior) to bias intent decisions
    # in ambiguous moments. "off" disables the prior entirely so the
    # director runs purely on live audio. UI doesn't expose a toggle in
    # this milestone — set via API only.
    "structural_prior_mode": "auto",
    # --- AutoLight 2.0 (DJ engine) controls ----------------------------------
    # Global cap on overall intensity (0.05–1.0). The mini guardrails panel
    # exposes this for live correction.
    "intensity_ceiling": 1.0,
    # How hard calm↔peak are spread (0–1). User default: very contrasted.
    "contrast": 1.0,
    # Sober preset for small venues (reduces movement + intensity).
    "small_venue": False,
    # Global strobe permission (the brain still only strobes on build/drop).
    "allow_strobe": True,
    # Online metadata lookup (genre/BPM/key) via Deezer/MusicBrainz/GetSongBPM.
    "metadata_enabled": True,
    # Optional GetSongBPM API key (opt-in source; keyless sources work without).
    "getsongbpm_key": "",
}

_ALLOWED_RENDER_MODES = {"director", "effects", "off"}
_ALLOWED_STRUCTURAL_PRIOR_MODES = {"auto", "off"}


# Default tunings per genre. Start from DEFAULT_AUDIO_TUNING and override a
# few knobs + suggest a mood filter. "auto" leaves everything at defaults.
GENRE_PRESETS: Dict[str, Dict[str, Any]] = {
    "auto": {
        "audio_tuning": {},
        "mood_filter": [],
    },
    "edm": {
        "audio_tuning": {
            "active_rms_floor": 0.020,
            "level_chorus_floor": 0.030,
            "level_high_floor": 0.060,
            "drop_score_min": 1.6,
            "drop_rms_min": 0.030,
            "bpm_window_beats": 8.0,
        },
        "mood_filter": ["energetic", "aggressive"],
    },
    "rock": {
        "audio_tuning": {
            "active_rms_floor": 0.018,
            "level_chorus_floor": 0.028,
            "level_high_floor": 0.050,
            "drop_score_min": 1.7,
            "bpm_window_beats": 10.0,
        },
        "mood_filter": ["energetic", "aggressive", "dramatic"],
    },
    "pop": {
        "audio_tuning": {
            "active_rms_floor": 0.015,
            "level_chorus_floor": 0.025,
            "level_high_floor": 0.050,
            "bpm_window_beats": 8.0,
        },
        "mood_filter": ["energetic", "calm"],
    },
    "ambient": {
        "audio_tuning": {
            "active_rms_floor": 0.008,
            "long_rms_floor": 0.006,
            "level_chorus_floor": 0.015,
            "level_high_floor": 0.030,
            "drop_score_min": 2.2,
            "bpm_window_beats": 16.0,
            "beat_spike_ratio": 1.6,
        },
        "mood_filter": ["calm", "cinematic"],
    },
    "jazz": {
        "audio_tuning": {
            "active_rms_floor": 0.012,
            "level_chorus_floor": 0.025,
            "level_high_floor": 0.045,
            "bpm_window_beats": 16.0,
        },
        "mood_filter": ["calm", "cinematic"],
    },
    "metal": {
        "audio_tuning": {
            "active_rms_floor": 0.025,
            "level_chorus_floor": 0.045,
            "level_high_floor": 0.070,
            "drop_score_min": 1.5,
            "bpm_window_beats": 6.0,
        },
        "mood_filter": ["aggressive", "energetic", "dramatic"],
    },
}

_ALLOWED_MODES = {"off", "assist", "live"}
_ALLOWED_SOURCE_MODES = {
    "player_metadata_then_local",
    "player_metadata_only",
    "local_file_only",
}

_PROBE_POLL_INTERVAL = 1.0


def _clamp_int(value: Any, min_val: int, max_val: int, default: int) -> int:
    try:
        out = int(float(value))
    except Exception:
        return default
    return max(min_val, min(max_val, out))


def _clamp_float(value: Any, min_val: float, max_val: float, default: float) -> float:
    try:
        out = float(value)
    except Exception:
        return default
    return max(min_val, min(max_val, out))


def normalize_autolight_settings(payload: Any, current: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = dict(DEFAULT_AUTOLIGHT_SETTINGS)
    if isinstance(current, dict):
        out.update(current)
    if isinstance(payload, dict):
        out.update(payload)

    mode = str(out.get("mode") or "live").strip().lower()
    out["mode"] = mode if mode in _ALLOWED_MODES else "live"

    source_mode = str(out.get("source_mode") or "player_metadata_then_local").strip().lower()
    out["source_mode"] = source_mode if source_mode in _ALLOWED_SOURCE_MODES else "player_metadata_then_local"

    out["enabled"] = bool(out.get("enabled", False))
    out["freeze_global"] = bool(out.get("freeze_global", False))
    out["allow_guarded_channels"] = bool(out.get("allow_guarded_channels", False))
    out["snapshot_auto_capture"] = bool(out.get("snapshot_auto_capture", False))
    out["override_timeout_ms"] = _clamp_int(out.get("override_timeout_ms"), 500, 60000, 5000)
    out["confidence_threshold"] = _clamp_float(out.get("confidence_threshold"), 0.0, 1.0, 0.75)
    out["energy_sensitivity"] = _clamp_float(out.get("energy_sensitivity"), 0.1, 2.0, 1.0)
    out["movement_sensitivity"] = _clamp_float(out.get("movement_sensitivity"), 0.1, 2.0, 1.0)

    raw_device = out.get("audio_device_index", None)
    if raw_device is None or raw_device == "" or raw_device == "default":
        out["audio_device_index"] = None
    else:
        try:
            out["audio_device_index"] = int(raw_device)
        except Exception:
            out["audio_device_index"] = None
    out["soundcloud_client_id"] = str(out.get("soundcloud_client_id") or "").strip()
    out["effect_config"] = _normalize_effect_config(out.get("effect_config"))
    out["effects_only"] = bool(out.get("effects_only", False))

    # Audio tuning: merge over defaults, clamp each value to the safe range
    # that AudioAnalyzer enforces.
    from audio_analyzer import _clamp_tuning
    out["audio_tuning"] = _clamp_tuning(out.get("audio_tuning") or {})

    # Mood filter: list of strings.
    raw_mood = out.get("mood_filter")
    if isinstance(raw_mood, list):
        out["mood_filter"] = [str(m).strip().lower() for m in raw_mood if str(m).strip()]
    elif isinstance(raw_mood, str) and raw_mood.strip():
        out["mood_filter"] = [raw_mood.strip().lower()]
    else:
        out["mood_filter"] = []

    out["bpm_confidence_gate"] = _clamp_float(out.get("bpm_confidence_gate"), 0.0, 1.0, 0.0)
    genre = str(out.get("genre_preset") or "auto").strip().lower()
    out["genre_preset"] = genre if genre in GENRE_PRESETS else "auto"
    out["tap_tempo_bpm"] = _clamp_float(out.get("tap_tempo_bpm"), 0.0, 300.0, 0.0)

    lock = out.get("scene_lock") if isinstance(out.get("scene_lock"), dict) else {}
    scene_name = str(lock.get("scene") or "").strip().upper()
    if scene_name not in {"SILENT", "VERSE", "CHORUS", "HIGH", "DROP"}:
        scene_name = ""
    try:
        until_ms = int(lock.get("until_ts_ms") or 0)
    except Exception:
        until_ms = 0
    out["scene_lock"] = {"scene": scene_name, "until_ts_ms": until_ms}

    render_mode = str(out.get("render_mode") or "director").strip().lower()
    out["render_mode"] = render_mode if render_mode in _ALLOWED_RENDER_MODES else "director"
    out["memory_persistence"] = bool(out.get("memory_persistence", False))
    spm = str(out.get("structural_prior_mode") or "auto").strip().lower()
    out["structural_prior_mode"] = spm if spm in _ALLOWED_STRUCTURAL_PRIOR_MODES else "auto"

    # AutoLight 2.0 (DJ engine) controls.
    out["intensity_ceiling"] = _clamp_float(out.get("intensity_ceiling"), 0.05, 1.0, 1.0)
    out["contrast"] = _clamp_float(out.get("contrast"), 0.0, 1.0, 1.0)
    out["small_venue"] = bool(out.get("small_venue", False))
    out["allow_strobe"] = bool(out.get("allow_strobe", True))
    out["metadata_enabled"] = bool(out.get("metadata_enabled", True))
    out["getsongbpm_key"] = str(out.get("getsongbpm_key") or "").strip()
    return out


def _normalize_effect_config(raw: Any) -> Dict[str, Dict[str, Any]]:
    """Accept any mapping of effect-name → {enabled, weight, duration_beats, cooldown_bars}.

    Unknown keys are dropped. Values are clamped to safe ranges so a broken
    UI can't brick the scheduler.
    """
    if not isinstance(raw, dict):
        return {}
    cleaned: Dict[str, Dict[str, Any]] = {}
    for name, cfg in raw.items():
        if not isinstance(cfg, dict):
            continue
        key = str(name or "").strip()
        if not key:
            continue
        entry: Dict[str, Any] = {}
        if "enabled" in cfg:
            entry["enabled"] = bool(cfg.get("enabled"))
        if "weight" in cfg:
            entry["weight"] = _clamp_float(cfg.get("weight"), 0.0, 5.0, 1.0)
        if "duration_beats" in cfg:
            entry["duration_beats"] = _clamp_float(cfg.get("duration_beats"), 0.25, 32.0, 4.0)
        if "cooldown_bars" in cfg:
            entry["cooldown_bars"] = _clamp_float(cfg.get("cooldown_bars"), 0.0, 16.0, 2.0)
        if entry:
            cleaned[key] = entry
    return cleaned


_APP_NAME_LOOKUP = {
    "SpotifyAB.SpotifyMusic": "Spotify",
    "Microsoft.ZuneMusic": "Groove Music",
    "AppleInc.iTunes": "iTunes",
    "OperaSoftware.OperaGXWebBrowser": "Opera GX",
    "OperaSoftware.Opera": "Opera",
    "Mozilla.Firefox": "Firefox",
    "Google.Chrome": "Chrome",
    "Microsoft.MicrosoftEdge": "Edge",
    "spotify.exe": "Spotify",
    "foobar2000.exe": "foobar2000",
    "chrome.exe": "Chrome",
    "msedge.exe": "Edge",
    "firefox.exe": "Firefox",
    "opera.exe": "Opera",
    "vlc.exe": "VLC",
}


def _friendly_app_name(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""

    # AUMID: "PackageFamily!AppId" — keep the app id side as a fallback label.
    if "!" in text:
        pkg, app_id = text.split("!", 1)
    else:
        pkg, app_id = text, ""

    # Strip the publisher hash from a package family name ("Name_abcdef" → "Name").
    package_name = pkg.split("_", 1)[0]

    for key, name in _APP_NAME_LOOKUP.items():
        if package_name == key or package_name.startswith(key + ".") or package_name.startswith(key + "_"):
            return name
    lower = package_name.lower()
    for key, name in _APP_NAME_LOOKUP.items():
        key_lower = key.lower()
        if lower == key_lower or lower.startswith(key_lower + ".") or lower.startswith(key_lower + "_"):
            return name

    if lower.endswith(".exe"):
        tail = package_name.replace("/", "\\").split("\\")[-1]
        return tail[:-4]

    # Drop purely-numeric segments (some browsers append a PID or timestamp).
    parts = [p for p in package_name.split(".") if p and not p.isdigit()]
    if len(parts) >= 2:
        return parts[1]
    if parts:
        return parts[0]
    return app_id or package_name


class _MediaProbe:
    """Background Windows Media Session reader using winsdk.

    Runs a daemon thread with its own asyncio loop, polling session state on
    a fixed cadence. HTTP handlers read the cached snapshot under a lock and
    never block on WinRT calls.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot: Dict[str, Any] = {
            "available": False,
            "sessions": [],
            "best_track": None,
            "updated_at": 0,
            "error": None,
        }
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._mgr: Any = None

        try:
            from winsdk.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager,
                GlobalSystemMediaTransportControlsSessionPlaybackStatus,
            )
            self._MgrCls = GlobalSystemMediaTransportControlsSessionManager
            self._PlaybackStatusCls = GlobalSystemMediaTransportControlsSessionPlaybackStatus
            self._import_error: Optional[str] = None
        except Exception as exc:
            self._MgrCls = None
            self._PlaybackStatusCls = None
            self._import_error = f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._snapshot["error"] = self._import_error

        if self._MgrCls is not None:
            self._thread = threading.Thread(target=self._run, name="autolight-probe", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def get_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "available": self._snapshot["available"],
                "sessions": [dict(s) for s in self._snapshot["sessions"]],
                "best_track": dict(self._snapshot["best_track"]) if self._snapshot["best_track"] else None,
                "updated_at": self._snapshot["updated_at"],
                "error": self._snapshot["error"],
            }

    def _run(self) -> None:
        try:
            asyncio.run(self._loop())
        except Exception as exc:
            log.warning("autolight probe thread exited: %s", exc)
            with self._lock:
                self._snapshot["error"] = f"{type(exc).__name__}: {exc}"

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:
                log.debug("autolight probe tick failed: %s", exc)
                with self._lock:
                    self._snapshot["error"] = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(_PROBE_POLL_INTERVAL)

    async def _tick(self) -> None:
        if self._mgr is None:
            self._mgr = await self._MgrCls.request_async()

        sessions_out: List[Dict[str, Any]] = []
        for session in self._mgr.get_sessions():
            try:
                props = await session.try_get_media_properties_async()
            except Exception:
                props = None
            timeline = session.get_timeline_properties()
            playback = session.get_playback_info()

            status = playback.playback_status
            is_playing = status == self._PlaybackStatusCls.PLAYING
            title = (getattr(props, "title", "") or "").strip() or None
            artist = (getattr(props, "artist", "") or "").strip() or None
            album = (getattr(props, "album_title", "") or "").strip() or None

            app_raw = session.source_app_user_model_id or ""
            sessions_out.append({
                "app": app_raw,
                "app_name": _friendly_app_name(app_raw),
                "title": title,
                "artist": artist,
                "album": album,
                "position_ms": int(timeline.position.total_seconds() * 1000) if timeline.position else None,
                "duration_ms": int(timeline.end_time.total_seconds() * 1000) if timeline.end_time else None,
                "is_playing": bool(is_playing),
                "playback_status": getattr(status, "name", str(status)).lower(),
            })

        sessions_out.sort(key=lambda s: (
            not s["is_playing"],
            not bool(s["title"]),
            s["app_name"] or "",
        ))
        best = sessions_out[0] if sessions_out else None

        with self._lock:
            self._snapshot = {
                "available": True,
                "sessions": sessions_out,
                "best_track": best,
                "updated_at": int(time.time() * 1000),
                "error": None,
            }


class _AutoLightRenderer:
    """Audio-reactive overlay. Invoked by the DMX engine each render tick.

    Picks a scene from the audio structure-level, then per-fixture uses the
    cached :class:`TopologySnapshot` (mirror pair / cluster / x-order) plus
    per-device :attr:`DeviceState.capabilities` to produce scene-appropriate
    dimmer, color, pan/tilt and strobe values. Yields any fixture currently
    touched by an active cue fade.
    """

    def __init__(self, audio: AudioAnalyzer, service: "AutoLightService") -> None:
        self._audio = audio
        self._service = service
        self._global_hue = 0.0
        self._global_pulse = 0.0
        self._last_beat_count = 0
        self._last_call_ts: Optional[float] = None
        self._last_values: Dict[str, Dict[str, Any]] = {}
        self._last_audio: Dict[str, Any] = {}
        self._owned_channels: Dict[int, set] = {}
        self._topology: TopologySnapshot = TopologySnapshot()
        self._effect_scheduler = EffectScheduler()
        # The director pipeline is an alternative renderer used when
        # render_mode == "director". Both pipelines share the same audio
        # analyzer and engine reference; the active one is picked per frame.
        self._director_overlay = DirectorOverlay(audio, service)

        # AutoLight 2.0 pipeline: beat-grid → brain → show. This is the active
        # renderer for render_mode "director" (and now "effects" too); the
        # legacy scene/effect engine below is retained only as dead fallback
        # pending removal.
        self._grid = BeatGrid()
        self._brain = MusicBrain()
        self._show = ShowRenderer()
        self._dj_diag: Dict[str, Any] = {}

        # Scene-engine state.
        self._committed_scene = "SILENT"
        self._pending_scene = "SILENT"
        self._pending_since_ts = 0.0

        # Diagnostics published to the UI.
        self._diag_devices_seen = 0
        self._diag_devices_controllable = 0
        self._diag_last_frame_wrote = 0
        self._diag_last_frame_mode = "off"
        self._diag_last_frame_ts = 0
        self._diag_skipped_by_fade = 0

    def on_rig_changed(self, devices: Dict[str, Any]) -> None:
        """Called by the engine after rig registration. Recompute topology."""
        try:
            self._topology = compute_topology(devices)
            n = max(1, len(self._topology.order_by_x))
            for topo_fixture in self._topology.fixtures.values():
                topo_fixture._chaser_len = n  # effects use this to normalize
            log.info(
                "autolight topology: %d devices, %d mirror pairs, clusters %s",
                len(devices), self._topology.mirror_pair_count, self._topology.cluster_summary(),
            )
        except Exception as exc:
            log.warning("autolight topology compute failed: %s", exc)
            self._topology = TopologySnapshot()
        # Mirror the rig change into the director pipeline so its FixtureAgent
        # set + topology stay in sync no matter which mode is active.
        try:
            self._director_overlay.on_rig_changed(devices)
        except Exception as exc:
            log.debug("director on_rig_changed failed: %s", exc)
        # AutoLight 2.0 show renderer needs the role assignment + spatial order.
        try:
            self._show.on_rig_changed(devices, self._topology)
        except Exception as exc:
            log.debug("show on_rig_changed failed: %s", exc)

    def last_snapshot(self) -> Dict[str, Any]:
        topo = self._topology
        sched = self._effect_scheduler
        # Pull live device metadata so the UI can show cname / fixture /
        # universe / address alongside the topology assignment.
        engine_devices = self._service._engine_devices_snapshot_locked()
        topo_fixtures: List[Dict[str, Any]] = []
        for f in topo.fixtures.values():
            dev = engine_devices.get(f.device_id)
            caps = getattr(dev, "capabilities", None) or {}
            topo_fixtures.append({
                "device_id": f.device_id,
                "x": f.x,
                "y": f.y,
                "side": f.mirror_side,
                "pair_id": f.mirror_pair_id,
                "cluster": [f.cluster_x, f.cluster_y],
                "order": f.order_index,
                "cname": str(getattr(dev, "cname", "") or "") if dev else "",
                "fixture": str(getattr(dev, "fixture_template", "") or "") if dev else "",
                "universe": int(getattr(dev, "universe", 0) or 0) if dev else 0,
                "address": int(getattr(dev, "base_address", 0) or 0) if dev else 0,
                "has_movement": bool(caps.get("has_movement")),
                "has_color": bool(caps.get("has_color")),
                "has_dimmer": bool(caps.get("has_dimmer")),
                "strobe_friendly": bool(caps.get("strobe_friendly")),
            })
        return {
            "audio": dict(self._last_audio),
            "fixtures": [dict(v) for v in self._last_values.values()],
            "global_hue": self._global_hue,
            "global_pulse": self._global_pulse,
            "devices_seen": self._diag_devices_seen,
            "devices_controllable": self._diag_devices_controllable,
            "last_frame_wrote": self._diag_last_frame_wrote,
            "last_frame_mode": self._diag_last_frame_mode,
            "last_frame_ts": self._diag_last_frame_ts,
            "scene": self._committed_scene,
            "skipped_by_fade": self._diag_skipped_by_fade,
            "topology": {
                "mirror_pair_count": topo.mirror_pair_count,
                "cluster_summary": topo.cluster_summary(),
                "cluster_counts_x": list(topo.cluster_counts_x),
                "cluster_counts_y": list(topo.cluster_counts_y),
                "has_positions": topo.has_positions,
                "center_x": topo.center_x,
                "fixtures": topo_fixtures,
            },
            "effect": {
                "active": sched.current_effect_name(),
                "last_chosen": sched.last_chosen,
                "trigger_count": sched.trigger_count,
                "history": [{"ts": t, "name": n} for t, n in sched.history[-10:]],
            },
            "director": self._director_overlay.last_snapshot(),
            "dj": dict(self._dj_diag),
        }

    def __call__(self, universes: Dict[int, List[int]], now: float) -> None:
        settings = self._service.get_settings()
        audio = self._audio.snapshot()
        self._last_audio = audio

        enabled = bool(settings.get("enabled"))
        mode = str(settings.get("mode") or "off").lower()
        render_mode = str(settings.get("render_mode") or "director").lower()

        # Decay the beat pulse with real dt so a frozen frame doesn't strand it.
        dt = 0.025 if self._last_call_ts is None else max(0.0, now - self._last_call_ts)
        self._last_call_ts = now
        if self._global_pulse > 0.0:
            decay = math.exp(-math.log(2.0) * dt / _BEAT_PULSE_HALFLIFE_S)
            self._global_pulse *= decay
            if self._global_pulse < 0.01:
                self._global_pulse = 0.0

        # render_mode=="off" → release the rig and bail out, even if AutoLight
        # is otherwise enabled. Lets the user keep AutoLight wired up but
        # silence the overlay temporarily without losing settings.
        if render_mode == "off":
            self._release_owned_channels(universes)
            try:
                self._director_overlay._release(universes)
            except Exception:
                pass
            self._last_values = {}
            self._committed_scene = "SILENT"
            self._pending_scene = "SILENT"
            self._diag_last_frame_wrote = 0
            self._diag_last_frame_mode = mode
            self._diag_last_frame_ts = int(time.time() * 1000)
            return

        # AutoLight 2.0 pipeline (beat-grid → brain → show) is now the active
        # renderer for both "director" and the formerly-legacy "effects" mode.
        # The old scene/effect engine below is retained as dead fallback only.
        if render_mode in ("director", "effects"):
            if not enabled or mode == "off" or not audio.get("available"):
                self._release_owned_channels(universes)
                try:
                    self._director_overlay._release(universes)
                except Exception:
                    pass
                self._last_values = {}
                self._committed_scene = "SILENT"
                self._pending_scene = "SILENT"
                self._diag_last_frame_wrote = 0
                self._diag_last_frame_mode = mode
                self._diag_last_frame_ts = int(time.time() * 1000)
                return
            self._render_dj(universes, now, settings, audio, mode)
            return

        if not enabled or mode == "off" or not audio.get("available"):
            self._release_owned_channels(universes)
            self._last_values = {}
            self._committed_scene = "SILENT"
            self._pending_scene = "SILENT"
            return

        beat_count = int(audio.get("beat_count") or 0)
        new_beat = beat_count != self._last_beat_count
        self._last_beat_count = beat_count
        if new_beat and audio.get("active"):
            intensity = float(audio.get("beat_intensity") or 0.0)
            self._global_pulse = max(self._global_pulse, 0.6 + 0.4 * min(1.0, intensity))
            self._global_hue = (self._global_hue + _GOLDEN_HUE_STEP) % 1.0

        # Pick scene from intensity level with 250 ms dwell hysteresis.
        structure = audio.get("structure") or {}
        raw_scene = _LEVEL_TO_SCENE.get(int(structure.get("level") or 0), "SILENT")
        if not audio.get("active"):
            raw_scene = "SILENT"
        if raw_scene != self._pending_scene:
            self._pending_scene = raw_scene
            self._pending_since_ts = now
        if self._pending_scene != self._committed_scene and (now - self._pending_since_ts) >= _SCENE_DWELL_S:
            self._committed_scene = self._pending_scene

        # Manual scene lock overrides the auto-detected scene.
        lock = settings.get("scene_lock") or {}
        lock_scene = str(lock.get("scene") or "").strip().upper()
        lock_until = int(lock.get("until_ts_ms") or 0)
        if lock_scene and (lock_until <= 0 or lock_until > int(time.time() * 1000)):
            self._committed_scene = lock_scene

        scene = _SCENE_POLICY.get(self._committed_scene, _SCENE_POLICY["SILENT"])

        engine = getattr(self._service, "_engine", None)
        devices = list(self._service._engine_devices_snapshot_locked().items())
        self._diag_devices_seen = len(devices)
        self._diag_devices_controllable = sum(
            1 for _, d in devices
            if (getattr(d, "capabilities", None) or {}).get("has_dimmer")
            or (getattr(d, "capabilities", None) or {}).get("has_color")
            or (getattr(d, "capabilities", None) or {}).get("has_movement")
        )

        effects_only = bool(settings.get("effects_only"))
        # Always run continuously: the rig should never be visually idle
        # while audio is playing. ``effects_only`` still controls whether the
        # scene engine *also* writes a baseline (False = scene + effect
        # layered, True = effect-only).
        bpm = float(audio.get("bpm") or 0.0)
        bar_count = int(audio.get("bar_count") or 0)
        last_beat_ms = float(audio.get("last_beat_ms") or 0.0)
        bpm_conf = float(audio.get("bpm_confidence") or 0.0)
        effect_ctx = self._effect_scheduler.tick(
            now=now,
            scene=self._committed_scene,
            bpm=bpm,
            bar_count=bar_count,
            last_beat_ms=last_beat_ms,
            audio_active=bool(audio.get("active")),
            continuous=True,
            bpm_confidence=bpm_conf,
        )

        # SILENT used to mean "blackout the rig". With AmbientGlow eligible
        # for SILENT, the scheduler always returns an effect_ctx here, so
        # this branch is now only a defensive fallback (e.g. user disabled
        # every ambient effect via effect_config). When that happens we keep
        # the legacy behaviour and dim out.
        if self._committed_scene == "SILENT" and effect_ctx is None:
            self._release_owned_channels_dimmer_only(universes)
            self._last_values = {}
            self._diag_last_frame_wrote = 0
            self._diag_last_frame_mode = mode
            self._diag_last_frame_ts = int(time.time() * 1000)
            self._diag_skipped_by_fade = 0
            return
        controllable = self._diag_devices_controllable
        wrote_count = 0
        skipped_by_fade = 0
        previously_owned = {uni: set(chs) for uni, chs in self._owned_channels.items()}
        self._owned_channels = {}

        energy_sens = float(settings.get("energy_sensitivity") or 1.0)
        move_sens = float(settings.get("movement_sensitivity") or 1.0)
        bass = float(audio.get("bass") or 0.0)
        mid = float(audio.get("mid") or 0.0)
        treble = float(audio.get("treble") or 0.0)
        bass_norm = min(1.0, (bass * energy_sens) / 0.025)
        mid_norm = min(1.0, (mid * energy_sens) / 0.020)
        treble_norm = min(1.0, (treble * energy_sens) / 0.010)

        if effect_ctx is not None:
            effect_ctx.bass_norm = bass_norm
            effect_ctx.mid_norm = mid_norm
            effect_ctx.treble_norm = treble_norm
            effect_ctx.global_hue = self._global_hue
            # The renderer's _global_pulse already integrates the live beat
            # detector with a 180 ms half-life — reuse it as the effects'
            # kick envelope so all beat-locked transforms agree on phase.
            effect_ctx.kick_env = max(effect_ctx.kick_env, float(self._global_pulse))
            effect_ctx.energy = max(bass_norm, mid_norm, treble_norm)

        pattern = scene["pattern"]
        ceiling = int(scene["ceiling"])
        base_dim = int(scene["base_dimmer"])
        energy_gain = float(scene["energy_gain"])
        hue_spread = float(scene["hue_spread"])
        mv_amp_deg = float(scene["mv_amp_deg"]) * move_sens
        mv_freq = float(scene["mv_freq_hz"])
        do_strobe = bool(scene["strobe"])

        # Chaser head sweeps left-to-right at a scene-scaled rate.
        chaser_period = 1.8 if pattern != "chaser_by_x" else max(0.6, 2.4 - bass_norm * 1.2)
        chaser_len = max(1, len(self._topology.order_by_x))
        chaser_head = (now / chaser_period) % 1.0

        # Mirror-alternate toggle on beat parity.
        mirror_select_left = (beat_count % 2 == 0)

        # Strobe gate: square wave so the fixture flashes on/off.
        strobe_on = bool(int(now * _STROBE_HZ * 2) % 2 == 0)

        computed: Dict[str, Dict[str, Any]] = {}
        for idx, (dev_id, dev) in enumerate(devices):
            caps = getattr(dev, "capabilities", None) or {}
            if not (caps.get("has_dimmer") or caps.get("has_color") or caps.get("has_movement")):
                continue

            # Skip fixtures currently being flashed by identify_device. The
            # camera-calibration phase needs the fixture to actually emit
            # its 1.5 s white flash; without this skip our 25 ms render
            # tick would clobber the 255 we just set.
            if self._service.is_identifying(dev_id):
                # Preserve ownership of this device's channels so the
                # "previously owned but not touched" cleanup at the end
                # of this frame doesn't zero them out — identify just
                # wrote them and we want those values to survive.
                if mode == "live":
                    uni_num = int(getattr(dev, "universe", 0))
                    owned = self._owned_channels.setdefault(uni_num, set())
                    for role in ("dimmer_channel", "red_channel", "green_channel",
                                 "blue_channel", "pan_channel", "tilt_channel"):
                        ch = caps.get(role)
                        if ch is not None:
                            owned.add(int(ch))
                continue

            fade_active = False
            if engine is not None:
                try:
                    fade_active = engine.has_active_fade_for(dev_id)
                except Exception:
                    fade_active = False

            topo = self._topology.fixtures.get(dev_id)
            order_index = topo.order_index if topo else idx
            cluster_x = topo.cluster_x if topo else 1
            cluster_y = topo.cluster_y if topo else 1
            mirror_side = topo.mirror_side if topo else None

            has_dimmer = caps.get("has_dimmer")
            red_ch = caps.get("red_channel")
            green_ch = caps.get("green_channel")
            blue_ch = caps.get("blue_channel")
            dimmer_ch = caps.get("dimmer_channel")

            writes: Dict[int, int] = {}
            participation = 0.0
            dimmer_val = 0
            r_val = g_val = b_val = 0
            hue = 0.0
            pan_val: Optional[int] = None
            tilt_val: Optional[int] = None

            if not effects_only:
                # --- Participation (dimmer gate) --------------------------
                participation = 1.0
                if pattern == "chaser_by_x":
                    pos = (order_index / chaser_len) if chaser_len else 0.0
                    d = abs(pos - chaser_head)
                    d = min(d, 1.0 - d)
                    width = 0.25
                    participation = max(0.0, 1.0 - (d / width)) if d < width else 0.0
                elif pattern == "mirror_alternate":
                    if mirror_side == "left":
                        participation = 1.0 if mirror_select_left else 0.25
                    elif mirror_side == "right":
                        participation = 0.25 if mirror_select_left else 1.0
                    else:
                        participation = 0.5
                elif pattern == "antisymmetric":
                    participation = 1.0

                # --- Dimmer ----------------------------------------------
                energy_drive = int(energy_gain * (60 * bass_norm + 30 * mid_norm + 15 * treble_norm))
                pulse_add = int(self._global_pulse * 90)
                dimmer_val = base_dim + int(participation * (energy_drive + pulse_add))
                dimmer_val = max(0, min(ceiling, dimmer_val))
                if do_strobe and caps.get("strobe_friendly") and strobe_on:
                    dimmer_val = 255
                elif do_strobe and caps.get("strobe_friendly") and not strobe_on:
                    dimmer_val = 0

                # --- Hue / color -----------------------------------------
                hue = (self._global_hue + order_index * hue_spread / max(1, chaser_len)) % 1.0
                if mirror_side == "right":
                    hue = (hue + 0.5) % 1.0
                if treble_norm > bass_norm + 0.15:
                    hue = (hue + 0.08) % 1.0
                elif bass_norm > treble_norm + 0.15:
                    hue = (hue - 0.06) % 1.0
                value_scalar = 0.55 + 0.45 * max(bass_norm, mid_norm, treble_norm)
                value_scalar = min(1.0, value_scalar + self._global_pulse * 0.35)
                r_f, g_f, b_f = colorsys.hsv_to_rgb(hue, 1.0, value_scalar)
                if not has_dimmer and caps.get("has_color"):
                    factor = dimmer_val / 255.0
                    r_val = max(0, min(255, int(r_f * 255 * factor)))
                    g_val = max(0, min(255, int(g_f * 255 * factor)))
                    b_val = max(0, min(255, int(b_f * 255 * factor)))
                else:
                    r_val = max(0, min(255, int(r_f * 255)))
                    g_val = max(0, min(255, int(g_f * 255)))
                    b_val = max(0, min(255, int(b_f * 255)))

                # --- Movement --------------------------------------------
                if caps.get("has_movement") and mv_amp_deg > 0:
                    phase = cluster_x * (math.pi / 3.0) + order_index * 0.22
                    if pattern == "antisymmetric" and mirror_side == "right":
                        phase += math.pi
                    t = now
                    deg = mv_amp_deg * math.sin(2.0 * math.pi * mv_freq * t + phase)
                    pan_offset = int(round(127.0 * deg / _DEFAULT_PAN_RANGE_DEG))
                    tilt_offset = int(round(127.0 * (mv_amp_deg * 0.5) * math.cos(2.0 * math.pi * mv_freq * 0.7 * t + phase) / _DEFAULT_PAN_RANGE_DEG))
                    pan_val = max(0, min(255, 128 + pan_offset))
                    tilt_val = max(0, min(255, 128 + tilt_offset))

                # Populate writes from scene values.
                if has_dimmer and dimmer_ch is not None:
                    writes[int(dimmer_ch)] = dimmer_val
                if red_ch is not None:
                    writes[int(red_ch)] = r_val
                if green_ch is not None:
                    writes[int(green_ch)] = g_val
                if blue_ch is not None:
                    writes[int(blue_ch)] = b_val
                if pan_val is not None and caps.get("pan_channel") is not None:
                    writes[int(caps["pan_channel"])] = pan_val
                if tilt_val is not None and caps.get("tilt_channel") is not None:
                    writes[int(caps["tilt_channel"])] = tilt_val

            # Apply the active pre-made effect (if any) on top of the scene
            # writes. Effects use channel semantics via caps.
            topo_fixture = self._topology.fixtures.get(dev_id)
            if effect_ctx is not None:
                self._effect_scheduler.apply(dev_id, caps, topo_fixture, writes, effect_ctx)

            computed[dev_id] = {
                "device_id": dev_id,
                "universe": getattr(dev, "universe", 0),
                "scene": self._committed_scene,
                "participation": participation,
                "controlled": not fade_active,
                "mirror_side": mirror_side,
                "cluster": [cluster_x, cluster_y],
                "writes": writes,
                "hue": hue,
                "dimmer": dimmer_val,
                "r": r_val,
                "g": g_val,
                "b": b_val,
                "pan": pan_val,
                "tilt": tilt_val,
            }

            if fade_active:
                skipped_by_fade += 1
                continue

            if mode == "live":
                uni_num = int(getattr(dev, "universe", 0))
                uni = universes.get(uni_num)
                if uni is None:
                    continue
                owned = self._owned_channels.setdefault(uni_num, set())
                for ch, val in writes.items():
                    if 0 <= ch < len(uni):
                        uni[ch] = val
                        owned.add(ch)
                        wrote_count += 1

        # Zero any channel we owned last frame but didn't touch this one.
        # In effects_only mode this is what lets the rig go dark between
        # effects; otherwise the scene engine writes every frame so nothing
        # ever orphans.
        if mode == "live" and previously_owned:
            for uni_num, prev in previously_owned.items():
                uni = universes.get(uni_num)
                if uni is None:
                    continue
                current = self._owned_channels.get(uni_num, set())
                for ch in prev - current:
                    if 0 <= ch < len(uni):
                        uni[ch] = 0

        self._last_values = computed
        self._diag_devices_controllable = controllable
        self._diag_last_frame_wrote = wrote_count
        self._diag_last_frame_mode = mode
        self._diag_last_frame_ts = int(time.time() * 1000)
        self._diag_skipped_by_fade = skipped_by_fade

    def _render_dj(self, universes: Dict[int, List[int]], now: float,
                   settings: Dict[str, Any], audio: Dict[str, Any], mode: str) -> None:
        """AutoLight 2.0 frame: beat-grid → brain → show → DMX writes."""
        # Guardrails / behaviour from settings.
        self._brain.configure(
            intensity_ceiling=settings.get("intensity_ceiling", 1.0),
            small_venue=settings.get("small_venue", False),
            contrast=settings.get("contrast", 1.0),
            allow_strobe_global=settings.get("allow_strobe", True),
        )

        # Metadata (genre / official BPM / key) feeds the brain + grid.
        meta: Optional[Dict[str, Any]] = None
        try:
            mobj = self._service._music.metadata_for_current()
        except Exception:
            mobj = None
        if mobj is not None:
            meta = {"genre": mobj.genre, "musical_key": mobj.musical_key}
            if mobj.bpm:
                audio = dict(audio)
                audio["db_bpm"] = mobj.bpm
        genre_preset = str(settings.get("genre_preset") or "auto").lower()
        if genre_preset and genre_preset != "auto":
            self._brain.set_genre(genre_preset)

        grid_state = self._grid.observe(now, audio)
        directive = self._brain.decide(now, grid_state, audio, meta)

        devices = self._service._engine_devices_snapshot_locked()
        self._diag_devices_seen = len(devices)
        self._diag_devices_controllable = sum(
            1 for d in devices.values()
            if (getattr(d, "capabilities", None) or {}).get("has_dimmer")
            or (getattr(d, "capabilities", None) or {}).get("has_color")
            or (getattr(d, "capabilities", None) or {}).get("has_movement")
        )

        writes = self._show.render(now, directive, devices, self._topology)

        # Fixtures currently under a manual cue fade keep manual priority.
        engine = getattr(self._service, "_engine", None)
        skip: Dict[int, set] = {}
        write_enabled = mode in ("live", "assist")
        if engine is not None and write_enabled:
            for dev_id, dev in devices.items():
                try:
                    if engine.has_active_fade_for(dev_id) or self._service.is_identifying(dev_id):
                        u = int(getattr(dev, "universe", 0) or 0)
                        caps = getattr(dev, "capabilities", None) or {}
                        s = skip.setdefault(u, set())
                        for role in ("dimmer_channel", "red_channel", "green_channel",
                                     "blue_channel", "pan_channel", "tilt_channel"):
                            c = caps.get(role)
                            if c is not None:
                                s.add(int(c))
                except Exception:
                    pass

        previously_owned = {uni: set(chs) for uni, chs in self._owned_channels.items()}
        self._owned_channels = {}
        wrote = 0
        for uni_num, chans in writes.items():
            uni = universes.get(uni_num)
            if uni is None:
                continue
            owned = self._owned_channels.setdefault(uni_num, set())
            skip_set = skip.get(uni_num, set())
            for ch, val in chans.items():
                if not (0 <= ch < len(uni)) or ch in skip_set:
                    continue
                if write_enabled:
                    uni[ch] = val
                owned.add(ch)
                wrote += 1

        # Zero channels we owned last frame but no longer drive.
        if previously_owned:
            for uni_num, prev in previously_owned.items():
                uni = universes.get(uni_num)
                if uni is None:
                    continue
                cur = self._owned_channels.get(uni_num, set())
                for ch in prev - cur:
                    if 0 <= ch < len(uni):
                        uni[ch] = 0

        # Diagnostics for the "DJ view" UI (étape 6).
        self._dj_diag = {
            "grid": grid_state,
            "intent": directive.intent,
            "energy": round(directive.energy, 3),
            "mode": directive.mode,
            "build_progress": directive.build_progress,
            "bars_to_drop": directive.bars_to_drop,
            "allow_strobe": directive.allow_strobe,
            "palette": directive.palette,
            "guardrails": directive.guardrails,
        }
        self._diag_last_frame_wrote = wrote
        self._diag_last_frame_mode = mode
        self._diag_last_frame_ts = int(time.time() * 1000)
        self._diag_skipped_by_fade = sum(len(s) for s in skip.values())

    def _release_owned_channels(self, universes: Dict[int, List[int]]) -> None:
        if not self._owned_channels:
            return
        for uni_num, channels in self._owned_channels.items():
            uni = universes.get(uni_num)
            if uni is None:
                continue
            for ch in channels:
                if 0 <= ch < len(uni):
                    uni[ch] = 0
        self._owned_channels = {}

    def _release_owned_channels_dimmer_only(self, universes: Dict[int, List[int]]) -> None:
        """Silence scene: zero only dimmer-like writes, leave pan/tilt frozen.

        We don't reliably know which of our owned channels are dimmer vs
        pan/tilt, so in practice we zero every owned channel that isn't a
        pan/tilt channel of a currently-registered mover. Users picked
        "freeze movers on silence"; this implements that.
        """
        if not self._owned_channels:
            return
        mover_channels: set = set()
        try:
            for dev in self._service._engine_devices_snapshot_locked().values():
                caps = getattr(dev, "capabilities", None) or {}
                if not caps.get("has_movement"):
                    continue
                uni = int(getattr(dev, "universe", 0))
                if caps.get("pan_channel") is not None:
                    mover_channels.add((uni, int(caps["pan_channel"])))
                if caps.get("tilt_channel") is not None:
                    mover_channels.add((uni, int(caps["tilt_channel"])))
        except Exception:
            pass
        for uni_num, channels in list(self._owned_channels.items()):
            uni = universes.get(uni_num)
            if uni is None:
                continue
            keep: set = set()
            for ch in channels:
                if (uni_num, ch) in mover_channels:
                    keep.add(ch)
                    continue
                if 0 <= ch < len(uni):
                    uni[ch] = 0
            if keep:
                self._owned_channels[uni_num] = keep
            else:
                self._owned_channels.pop(uni_num, None)


class AutoLightService:
    def __init__(self, settings: Optional[Dict[str, Any]] = None) -> None:
        self._lock = threading.RLock()
        self._settings = normalize_autolight_settings(settings)
        self._snapshots: List[Dict[str, Any]] = []
        self._probe = _MediaProbe()
        self._audio = AudioAnalyzer()
        self._renderer = _AutoLightRenderer(self._audio, self)
        self._music = MusicContext()
        self._music.set_soundcloud_client_id(self._settings.get("soundcloud_client_id") or None)
        self._music.set_getsongbpm_key(self._settings.get("getsongbpm_key") or None)
        self._music.set_metadata_enabled(bool(self._settings.get("metadata_enabled", True)))
        # Training service: library + real-time satisfaction signal pipeline.
        # Uses a callable to fetch the live director instead of capturing a
        # reference, so we never end up writing into a stale director after
        # an attach/detach cycle.
        # The identify callback wraps ``identify_device`` so the camera
        # calibration phase in the training modal can flash one fixture
        # at a time without coupling the training module to this service.
        self._training = TrainingService(
            director_provider=lambda: self._renderer._director_overlay._director,
            identify_callback=lambda dev_id, dur: self.identify_device(dev_id, dur),
        )
        # Devices currently being flashed by ``identify_device``. Both the
        # effects renderer and the director overlay skip writing to these
        # so the flash isn't immediately overwritten by the per-frame
        # decision loop. Without this, ``identify_device`` writes once,
        # the renderer overwrites 25 ms later, and nothing visible
        # happens — exactly the symptom that motivated this set.
        self._identify_active_devices: set = set()
        self._identify_active_lock = threading.Lock()
        self._engine: Any = None
        try:
            self._audio.select_device(self._settings.get("audio_device_index"))
        except Exception:
            pass
        try:
            self._renderer._effect_scheduler.set_config(self._settings.get("effect_config") or {})
        except Exception:
            pass
        self._apply_all_runtime_settings_locked()
        self._status_cache = self._build_status(self._discover_players(), None)

    @property
    def training(self) -> TrainingService:
        """Public accessor used by HTTP route handlers."""
        return self._training

    def _apply_all_runtime_settings_locked(self) -> None:
        """Push current settings into the audio analyzer + scheduler."""
        try:
            self._audio.set_tuning(self._settings.get("audio_tuning") or {})
        except Exception as exc:
            log.debug("audio tuning apply failed: %s", exc)
        try:
            bpm = float(self._settings.get("tap_tempo_bpm") or 0.0)
            self._audio.set_tap_tempo(bpm if bpm > 0 else None)
        except Exception:
            pass
        try:
            self._renderer._effect_scheduler.set_mood_filter(self._settings.get("mood_filter") or [])
        except Exception:
            pass
        try:
            self._renderer._effect_scheduler.set_bpm_confidence_gate(
                float(self._settings.get("bpm_confidence_gate") or 0.0)
            )
        except Exception:
            pass
        # Director-pipeline runtime: propagate structural prior + genre so
        # the StructureTracker picks the right template and can be killed
        # via the API without restarting.
        try:
            director = self._renderer._director_overlay._director
            director.set_structural_prior_mode(self._settings.get("structural_prior_mode") or "auto")
            director.set_genre_preset(self._settings.get("genre_preset") or "auto")
        except Exception:
            pass
        # AutoLight 2.0: online metadata sources (genre/BPM/key).
        try:
            self._music.set_metadata_enabled(bool(self._settings.get("metadata_enabled", True)))
            self._music.set_getsongbpm_key(self._settings.get("getsongbpm_key") or None)
        except Exception:
            pass

    def apply_genre_preset(self, name: str) -> Dict[str, Any]:
        """Apply a genre preset over the current settings, then save."""
        name = str(name or "auto").strip().lower()
        preset = GENRE_PRESETS.get(name, GENRE_PRESETS["auto"])
        with self._lock:
            tuned = dict(self._settings.get("audio_tuning") or {})
            tuned.update(preset.get("audio_tuning") or {})
            next_settings = dict(self._settings)
            next_settings["audio_tuning"] = tuned
            next_settings["mood_filter"] = list(preset.get("mood_filter") or [])
            next_settings["genre_preset"] = name
            self._settings = normalize_autolight_settings(next_settings, self._settings)
            self._apply_all_runtime_settings_locked()
            return dict(self._settings)

    def apply_tap_tempo(self, bpm: Optional[float]) -> Dict[str, Any]:
        with self._lock:
            next_settings = dict(self._settings)
            next_settings["tap_tempo_bpm"] = float(bpm or 0.0)
            self._settings = normalize_autolight_settings(next_settings, self._settings)
            self._apply_all_runtime_settings_locked()
            return dict(self._settings)

    def apply_scene_lock(self, scene: Optional[str], duration_s: float) -> Dict[str, Any]:
        """Pin the committed scene to ``scene`` for ``duration_s`` seconds."""
        scene = (str(scene or "").strip().upper() or "")
        with self._lock:
            if not scene:
                self._settings["scene_lock"] = {"scene": "", "until_ts_ms": 0}
            else:
                until_ms = int((time.time() + max(1.0, float(duration_s or 0))) * 1000)
                self._settings["scene_lock"] = {"scene": scene, "until_ts_ms": until_ms}
            self._settings = normalize_autolight_settings(self._settings, None)
            return dict(self._settings)

    def list_audio_devices(self) -> List[Dict[str, Any]]:
        return self._audio.list_devices()

    def set_audio_device(self, index: Optional[int]) -> Dict[str, Any]:
        with self._lock:
            self._settings["audio_device_index"] = int(index) if index is not None else None
        try:
            self._audio.select_device(index)
        except Exception as exc:
            log.warning("audio device switch failed: %s", exc)
        return dict(self._settings)

    def attach_engine(self, engine: Any) -> None:
        """Wire this service to a DMXRenderEngine and install the overlay."""
        with self._lock:
            self._engine = engine
        if engine is not None and hasattr(engine, "set_autolight_overlay"):
            engine.set_autolight_overlay(self._renderer)

    def detach_engine(self) -> None:
        engine = self._engine
        self._engine = None
        if engine is not None and hasattr(engine, "set_autolight_overlay"):
            try:
                engine.set_autolight_overlay(None)
            except Exception:
                pass

    def shutdown(self) -> None:
        self.detach_engine()
        # Flush any pending track-memory writes before we tear down.
        try:
            self._renderer._director_overlay.force_save_memory()
        except Exception:
            pass
        try:
            self._audio.stop()
        except Exception:
            pass
        try:
            self._probe.stop()
        except Exception:
            pass

    def _media_probe_best_track(self) -> Optional[Dict[str, Any]]:
        """Snapshot of the currently-playing track for the director to learn.

        Returns ``None`` when the media probe didn't recognise anything (no
        title, no media session). The director uses this both for track
        memory and for structural priors — the structural side specifically
        needs ``position_ms`` and ``is_playing`` so it can compute song
        progress and skip the prior on paused playback.
        """
        try:
            snap = self._probe.get_snapshot()
            best = snap.get("best_track") if isinstance(snap, dict) else None
            if not best or not best.get("title"):
                return None
            return {
                "title": best.get("title"),
                "artist": best.get("artist"),
                "duration_ms": best.get("duration_ms"),
                "position_ms": best.get("position_ms"),
                "is_playing": best.get("is_playing", True),
            }
        except Exception:
            return None

    def _engine_devices_snapshot_locked(self) -> Dict[str, Any]:
        """Return a shallow copy of the engine's registered devices.

        Called from inside the engine's render lock (overlay path), so direct
        access is safe. Returns an empty dict when no engine is attached.
        """
        engine = self._engine
        if engine is None:
            return {}
        devices = getattr(engine, "_devices", None) or {}
        return dict(devices)

    def get_settings(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._settings)

    def apply_settings(self, payload: Any) -> Dict[str, Any]:
        with self._lock:
            previous_device = self._settings.get("audio_device_index")
            previous_sc = self._settings.get("soundcloud_client_id")
            self._settings = normalize_autolight_settings(payload, self._settings)
            self._status_cache = self._build_status(self._discover_players(), None)
            new_device = self._settings.get("audio_device_index")
            new_sc = self._settings.get("soundcloud_client_id")
            result = dict(self._settings)
        if new_device != previous_device:
            try:
                self._audio.select_device(new_device)
            except Exception as exc:
                log.warning("audio device switch failed: %s", exc)
        if new_sc != previous_sc:
            try:
                self._music.set_soundcloud_client_id(new_sc or None)
            except Exception:
                pass
        try:
            self._music.set_getsongbpm_key(result.get("getsongbpm_key") or None)
            self._music.set_metadata_enabled(bool(result.get("metadata_enabled", True)))
        except Exception:
            pass
        try:
            self._renderer._effect_scheduler.set_config(result.get("effect_config") or {})
        except Exception:
            pass
        with self._lock:
            self._apply_all_runtime_settings_locked()
        return result

    def list_effects(self) -> List[Dict[str, Any]]:
        """Return effect metadata + currently applied config for the UI."""
        meta = list_effects_meta()
        cfg = self._settings.get("effect_config") or {}
        out: List[Dict[str, Any]] = []
        for entry in meta:
            name = entry["name"]
            override = cfg.get(name, {})
            effective = self._renderer._effect_scheduler.effective_effect_params(
                next(e for e in self._renderer._effect_scheduler._registry if e.name == name)
            )
            out.append({
                **entry,
                "config": override,
                "effective": effective,
            })
        return out

    def list_moods(self) -> List[str]:
        return all_mood_tags()

    def list_genres(self) -> List[str]:
        return sorted(GENRE_PRESETS.keys())

    def get_audio_tuning(self) -> Dict[str, Any]:
        """Return current tuning + valid ranges for the UI sliders."""
        from audio_analyzer import DEFAULT_AUDIO_TUNING
        tuning = dict(self._settings.get("audio_tuning") or DEFAULT_AUDIO_TUNING)
        return {
            "tuning": tuning,
            "defaults": dict(DEFAULT_AUDIO_TUNING),
        }

    def start_calibration(self, duration_s: float = 30.0) -> Dict[str, Any]:
        """Observe audio for N seconds and auto-pose thresholds from percentiles."""
        duration_s = max(5.0, min(120.0, float(duration_s or 30.0)))

        def _worker():
            import statistics
            samples: List[Dict[str, Any]] = []
            end = time.time() + duration_s
            while time.time() < end:
                try:
                    snap = self._audio.snapshot()
                    if snap.get("available"):
                        samples.append(snap)
                except Exception:
                    pass
                time.sleep(0.1)
            if not samples:
                return
            active = [s for s in samples if s.get("active")]
            if len(active) < 10:
                log.info("autolight calibrate: too quiet, no changes applied")
                return
            rms_vals = sorted(float(s.get("rms") or 0.0) for s in active)
            bass_vals = sorted(float(s.get("bass") or 0.0) for s in active)
            def pct(arr: List[float], p: float) -> float:
                if not arr:
                    return 0.0
                idx = max(0, min(len(arr) - 1, int(p * (len(arr) - 1))))
                return arr[idx]
            new_tuning = dict(self._settings.get("audio_tuning") or DEFAULT_AUDIO_TUNING)
            new_tuning["active_rms_floor"] = max(0.005, pct(rms_vals, 0.05) * 1.2)
            new_tuning["long_rms_floor"] = new_tuning["active_rms_floor"] * 0.8
            new_tuning["level_chorus_floor"] = pct(rms_vals, 0.40)
            new_tuning["level_high_floor"] = pct(rms_vals, 0.80)
            new_tuning["drop_rms_min"] = pct(rms_vals, 0.25)
            new_tuning["beat_min_bass"] = max(0.002, pct(bass_vals, 0.10))
            with self._lock:
                self._settings["audio_tuning"] = new_tuning
                self._settings = normalize_autolight_settings(self._settings, None)
                self._apply_all_runtime_settings_locked()
            log.info("autolight calibrate: applied %s", new_tuning)

        t = threading.Thread(target=_worker, name="autolight-calibrate", daemon=True)
        t.start()
        return {"ok": True, "duration_s": duration_s}

    def force_trigger_effect(self, effect_name: str) -> bool:
        audio_snap = self._audio.snapshot() if hasattr(self, "_audio") else {}
        bpm = float(audio_snap.get("bpm") or 0.0)
        beat_period = 60.0 / bpm if bpm >= 50.0 else 0.5
        return self._renderer._effect_scheduler.force_trigger_with_bar(
            effect_name, time.perf_counter(), beat_period,
        )

    def identify_device(self, device_id: str, duration_s: float = 2.0) -> bool:
        """Flash one fixture white at full brightness so the user can spot it.

        Uses the engine's identify-overlay system (reverse-universe render
        path). Returns False when no engine is attached or the device is
        unknown. Duration is clamped to [0.5, 10]s.
        """
        dev_id = str(device_id or "").strip()
        if not dev_id:
            return False
        engine = self._engine
        if engine is None:
            return False
        devs = getattr(engine, "_devices", None) or {}
        dev = devs.get(dev_id)
        if dev is None:
            return False
        duration_s = max(0.5, min(10.0, float(duration_s or 2.0)))
        universe = int(getattr(dev, "universe", 0) or 0)
        caps = getattr(dev, "capabilities", None) or {}
        channels: Dict[int, int] = {}
        if caps.get("dimmer_channel") is not None:
            channels[int(caps["dimmer_channel"])] = 255
        for role in ("red_channel", "green_channel", "blue_channel"):
            ch = caps.get(role)
            if ch is not None:
                channels[int(ch)] = 255

        # Reserve this device against the autolight overlay for the flash
        # duration. Without this, the per-frame render tick (25 ms) writes
        # over the 255 we just set and the flash is invisible. Both
        # pipelines (effects + director) consult ``is_identifying``.
        with self._identify_active_lock:
            self._identify_active_devices.add(dev_id)

        def _flash():
            try:
                for ch, val in channels.items():
                    engine.set_channel(dev_id, universe, ch, val)
                time.sleep(duration_s)
                for ch in channels:
                    engine.set_channel(dev_id, universe, ch, 0)
            except Exception as exc:
                log.debug("identify flash failed: %s", exc)
            finally:
                # Always clear the reservation, even if engine.set_channel
                # raised — otherwise the device would be permanently
                # locked out of the autolight overlay.
                with self._identify_active_lock:
                    self._identify_active_devices.discard(dev_id)

        threading.Thread(target=_flash, name="autolight-identify", daemon=True).start()
        return True

    def is_identifying(self, device_id: str) -> bool:
        """True if ``identify_device`` is currently flashing this fixture.
        The render pipelines call this to skip the device for the flash
        duration so the calibration LED isn't clobbered each frame."""
        with self._identify_active_lock:
            return device_id in self._identify_active_devices

    def control(self, payload: Any) -> Dict[str, Any]:
        payload = payload if isinstance(payload, dict) else {}
        with self._lock:
            next_settings = dict(self._settings)
            if "enabled" in payload:
                next_settings["enabled"] = bool(payload.get("enabled"))
            if "freeze_global" in payload:
                next_settings["freeze_global"] = bool(payload.get("freeze_global"))
            if "mode" in payload:
                next_settings["mode"] = payload.get("mode")
            if "render_mode" in payload:
                next_settings["render_mode"] = payload.get("render_mode")
            if "memory_persistence" in payload:
                next_settings["memory_persistence"] = bool(payload.get("memory_persistence"))
            if "structural_prior_mode" in payload:
                next_settings["structural_prior_mode"] = payload.get("structural_prior_mode")
            # AutoLight 2.0 guardrails / DJ-engine controls (mini-panel).
            for key in ("intensity_ceiling", "contrast", "small_venue",
                        "allow_strobe", "metadata_enabled", "getsongbpm_key"):
                if key in payload:
                    next_settings[key] = payload.get(key)
            self._settings = normalize_autolight_settings(next_settings, self._settings)
            self._apply_all_runtime_settings_locked()
            self._status_cache = self._build_status(self._discover_players(), None)
            return dict(self._status_cache)

    def get_status(self, force_refresh: bool = False) -> Dict[str, Any]:
        with self._lock:
            self._status_cache = self._build_status(self._discover_players(), None)
            return dict(self._status_cache)

    def get_features(self) -> Dict[str, Any]:
        return self.get_status().get("features") or self._empty_features()

    def get_audio_snapshot(self) -> Dict[str, Any]:
        """Lightweight snapshot of just the audio analyzer state — used by the
        UI's high-frequency spectrogram poll. Avoids the heavy player discovery
        and music-service work in `get_status()`."""
        audio_snap = self._audio.snapshot() if hasattr(self, "_audio") else {}
        return {
            "available": bool(audio_snap.get("available")),
            "active": bool(audio_snap.get("active")),
            "rms": float(audio_snap.get("rms") or 0.0),
            "bass": float(audio_snap.get("bass") or 0.0),
            "mid": float(audio_snap.get("mid") or 0.0),
            "treble": float(audio_snap.get("treble") or 0.0),
            "bass_norm": float(audio_snap.get("bass_norm") or 0.0),
            "mid_norm": float(audio_snap.get("mid_norm") or 0.0),
            "treble_norm": float(audio_snap.get("treble_norm") or 0.0),
            "beat": bool(audio_snap.get("beat")),
            "beat_count": int(audio_snap.get("beat_count") or 0),
            "kick": bool(audio_snap.get("kick")),
            "kick_count": int(audio_snap.get("kick_count") or 0),
            "snare": bool(audio_snap.get("snare")),
            "snare_count": int(audio_snap.get("snare_count") or 0),
            "snare_intensity": float(audio_snap.get("snare_intensity") or 0.0),
            "hat": bool(audio_snap.get("hat")),
            "hat_count": int(audio_snap.get("hat_count") or 0),
            "hat_intensity": float(audio_snap.get("hat_intensity") or 0.0),
            "flux_mid": float(audio_snap.get("flux_mid") or 0.0),
            "flux_high": float(audio_snap.get("flux_high") or 0.0),
            "bpm": float(audio_snap.get("bpm") or 0.0),
            "bpm_confidence": float(audio_snap.get("bpm_confidence") or 0.0),
            "bpm_source": str(audio_snap.get("bpm_source") or "auto"),
            "bpm_method": str(audio_snap.get("bpm_method") or "median"),
            "bar_count": int(audio_snap.get("bar_count") or 0),
            "spectrum": list(audio_snap.get("spectrum") or []),
        }

    def list_snapshots(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._snapshots]

    def create_snapshot(self, payload: Any) -> Dict[str, Any]:
        data = payload if isinstance(payload, dict) else {}
        with self._lock:
            current = self.get_status()
            snapshot = {
                "id": f"autolight-{int(time.time() * 1000)}",
                "name": str(data.get("name") or f"Snapshot {len(self._snapshots) + 1}").strip() or f"Snapshot {len(self._snapshots) + 1}",
                "created_at": int(time.time() * 1000),
                "track": current.get("track") or {},
                "summary": current.get("summary") or "",
                "settings": dict(self._settings),
                "payload": data.get("payload") if isinstance(data.get("payload"), dict) else {},
            }
            self._snapshots.insert(0, snapshot)
            self._snapshots = self._snapshots[:24]
            return dict(snapshot)

    def _discover_players(self) -> Dict[str, Any]:
        probe = self._probe.get_snapshot()
        sessions = probe["sessions"]
        best = probe["best_track"]
        running: List[str] = []
        for s in sessions:
            name = s.get("app_name") or s.get("app") or ""
            if name and name not in running:
                running.append(name)

        if not probe["available"]:
            metadata_source = "unavailable"
            confidence = 0.0
        elif best:
            metadata_source = "windows_media_session"
            confidence = 0.9 if best.get("title") else 0.5
        else:
            metadata_source = "idle"
            confidence = 0.0

        preferred = (best.get("app_name") if best else None) or (running[0] if running else None)

        if best and best.get("title"):
            try:
                self._music.observe_track(best.get("title"), best.get("artist"), best.get("duration_ms"))
            except Exception:
                pass

        return {
            "preferred_player": preferred,
            "running_players": running,
            "detected_count": len(running),
            "sessions": sessions,
            "best_track": best,
            "metadata_source": metadata_source,
            "confidence": confidence,
            "probe_error": probe["error"],
            "probe_available": probe["available"],
            "probe_updated_at": probe["updated_at"],
        }

    def _build_status(self, player_info: Dict[str, Any], last_error: Optional[str]) -> Dict[str, Any]:
        settings = dict(self._settings)
        running_players = player_info.get("running_players") or []
        preferred_player = player_info.get("preferred_player")
        best_track = player_info.get("best_track") if isinstance(player_info.get("best_track"), dict) else None
        track = {
            "title": best_track.get("title") if best_track else None,
            "artist": best_track.get("artist") if best_track else None,
            "album": best_track.get("album") if best_track else None,
            "duration_ms": best_track.get("duration_ms") if best_track else None,
            "position_ms": best_track.get("position_ms") if best_track else None,
            "is_playing": bool(best_track.get("is_playing")) if best_track else False,
        }
        features = self._empty_features()
        features.update({
            "title": track["title"],
            "artist": track["artist"],
            "duration_ms": track["duration_ms"],
            "position_ms": track["position_ms"],
            "is_playing": track["is_playing"],
            "confidence": float(player_info.get("confidence") or 0.0),
            "source": str(player_info.get("metadata_source") or "unresolved"),
        })

        probe_error = str(player_info.get("probe_error") or "").strip() or None
        probe_available = bool(player_info.get("probe_available"))
        resolved_error = last_error or (probe_error if not probe_available else None)

        if best_track:
            source_state = "ready"
        elif running_players:
            source_state = "partial"
        elif not probe_available:
            source_state = "error"
        else:
            source_state = "idle"

        summary_parts: List[str] = []
        if settings.get("enabled"):
            summary_parts.append(f"Mode {settings.get('mode')}")
        else:
            summary_parts.append("Disabled")
        if preferred_player:
            summary_parts.append(f"Source {preferred_player}")
        elif not probe_available:
            summary_parts.append("Media probe unavailable")
        else:
            summary_parts.append("No media player detected")
        if track["title"]:
            artist = track["artist"]
            summary_parts.append(f"Track {track['title']}" + (f" — {artist}" if artist else ""))

        audio_snap = self._audio.snapshot() if hasattr(self, "_audio") else {}
        render_snap = self._renderer.last_snapshot() if hasattr(self, "_renderer") else {}

        return {
            "enabled": bool(settings.get("enabled")),
            "mode": settings.get("mode"),
            "freeze_global": bool(settings.get("freeze_global")),
            "source_mode": settings.get("source_mode"),
            "source_state": source_state,
            "source_name": preferred_player,
            "running_players": running_players,
            "player": player_info,
            "track": track,
            "features": features,
            "audio": {
                "available": bool(audio_snap.get("available")),
                "active": bool(audio_snap.get("active")),
                "rms": float(audio_snap.get("rms") or 0.0),
                "bass": float(audio_snap.get("bass") or 0.0),
                "mid": float(audio_snap.get("mid") or 0.0),
                "treble": float(audio_snap.get("treble") or 0.0),
                "bass_norm": float(audio_snap.get("bass_norm") or 0.0),
                "mid_norm": float(audio_snap.get("mid_norm") or 0.0),
                "treble_norm": float(audio_snap.get("treble_norm") or 0.0),
                "beat": bool(audio_snap.get("beat")),
                "beat_count": int(audio_snap.get("beat_count") or 0),
                "kick": bool(audio_snap.get("kick")),
                "kick_count": int(audio_snap.get("kick_count") or 0),
                "snare": bool(audio_snap.get("snare")),
                "snare_count": int(audio_snap.get("snare_count") or 0),
                "snare_intensity": float(audio_snap.get("snare_intensity") or 0.0),
                "hat": bool(audio_snap.get("hat")),
                "hat_count": int(audio_snap.get("hat_count") or 0),
                "hat_intensity": float(audio_snap.get("hat_intensity") or 0.0),
                "flux_mid": float(audio_snap.get("flux_mid") or 0.0),
                "flux_high": float(audio_snap.get("flux_high") or 0.0),
                "bpm": float(audio_snap.get("bpm") or 0.0),
                "bpm_confidence": float(audio_snap.get("bpm_confidence") or 0.0),
                "bpm_source": str(audio_snap.get("bpm_source") or "auto"),
                "bpm_method": str(audio_snap.get("bpm_method") or "median"),
                "bar_count": int(audio_snap.get("bar_count") or 0),
                "sample_rate": int(audio_snap.get("sample_rate") or 0),
                "error": audio_snap.get("error"),
                "spectrum": list(audio_snap.get("spectrum") or []),
            },
            "render": {
                "global_hue": float(render_snap.get("global_hue") or 0.0),
                "global_pulse": float(render_snap.get("global_pulse") or 0.0),
                "fixture_count": len(render_snap.get("fixtures") or []),
                "fixtures": render_snap.get("fixtures") or [],
                "devices_seen": int(render_snap.get("devices_seen") or 0),
                "devices_controllable": int(render_snap.get("devices_controllable") or 0),
                "last_frame_wrote": int(render_snap.get("last_frame_wrote") or 0),
                "last_frame_mode": str(render_snap.get("last_frame_mode") or "off"),
                "last_frame_ts": int(render_snap.get("last_frame_ts") or 0),
                "scene": str(render_snap.get("scene") or "SILENT"),
                "skipped_by_fade": int(render_snap.get("skipped_by_fade") or 0),
                "topology": render_snap.get("topology") or {},
                "effect": render_snap.get("effect") or {},
                "director": render_snap.get("director") or {},
                "dj": render_snap.get("dj") or {},
                "render_mode": str(settings.get("render_mode") or "director"),
                "memory_persistence": bool(settings.get("memory_persistence", False)),
                "engine_attached": self._engine is not None,
                "audio_device_index": audio_snap.get("device_index"),
                "audio_device_name": audio_snap.get("device_name") or "",
            },
            "structure": audio_snap.get("structure") or {},
            "music": self._music.status(),
            "confidence": float(player_info.get("confidence") or 0.0),
            "summary": " | ".join(summary_parts),
            "last_error": resolved_error,
            "updated_at": int(time.time() * 1000),
        }

    def _empty_features(self) -> Dict[str, Any]:
        return {
            "track_id": None,
            "title": None,
            "artist": None,
            "duration_ms": None,
            "position_ms": None,
            "is_playing": False,
            "bpm": None,
            "key": None,
            "scale": None,
            "energy_global": None,
            "segments": [],
            "beats": [],
            "bars": [],
            "confidence": 0.0,
            "source": "unresolved",
        }
