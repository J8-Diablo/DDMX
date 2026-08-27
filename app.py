#!/usr/bin/env python3
# app.py - DMX/ArtNet controller backend (Flask) + New Render Engine

import os
import json
import time
import logging
import logging.handlers
import re
import socket
from typing import Dict, Any, List, Optional
from queue import Queue

from flask import Flask, render_template, jsonify, request, send_from_directory, Response
from autolight import AutoLightService, normalize_autolight_settings
from fixture_runtime import load_fixture_file
from runtime_paths import DATA_DIR, RESOURCE_DIR
from version import APP_LICENSE_CODE, APP_NAME, APP_UPDATE_RELEASES_URL, APP_VERSION

# New render engine (selectable)
ENGINE_MODE = os.environ.get("DMX_ENGINE", "render").strip().lower()
try:
    if ENGINE_MODE == "simple":
        from dmx_engine_simple import DMXSimpleEngine as DMXRenderEngine
    else:
        from dmx_engine import DMXRenderEngine
except ImportError:
    DMXRenderEngine = None

# Effects (for API listing)
try:
    import Effect
except ImportError:
    Effect = None

# Intelligent effects (Python)
try:
    import intelligent_fx as IntelligentFX
except ImportError:
    IntelligentFX = None

BASE_DIR = RESOURCE_DIR
os.makedirs(DATA_DIR, exist_ok=True)

FIXTURES_DIR = os.path.join(DATA_DIR, "fixtures")
CUE_DIR = os.path.join(DATA_DIR, "cue")
CONFIG_DIR = os.path.join(DATA_DIR, "config")
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")
INTELLIGENT_EFFECTS_DIR = os.path.join(DATA_DIR, "intelligent_effects")
PROJECTS_DIR = os.path.join(DATA_DIR, "projects")

os.makedirs(FIXTURES_DIR, exist_ok=True)
os.makedirs(CUE_DIR, exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(INTELLIGENT_EFFECTS_DIR, exist_ok=True)
os.makedirs(PROJECTS_DIR, exist_ok=True)

def _safe_effect_filename(name: str) -> Optional[str]:
    """Allow only safe JS/JSON filenames for intelligent effects."""
    if not isinstance(name, str):
        return None
    base = os.path.basename(name)
    base = re.sub(r"[^a-zA-Z0-9._-]", "_", base)
    if not base or not base.lower().endswith((".js", ".json")):
        return None
    return base

def list_intelligent_effect_files() -> List[str]:
    return sorted(
        f for f in os.listdir(INTELLIGENT_EFFECTS_DIR)
        if f.lower().endswith((".js", ".json"))
    )

# ---------- SETTINGS ----------

# Cue panel view modes: the classic cue table, the Premiere-style timeline, and
# Rapid Fire (a grid of one-click launch pads, one per cue list of the project).
CUE_VIEW_MODES = ("classic", "timeline", "rapidfire")


def get_local_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return float(default)
    try:
        return float(raw)
    except ValueError:
        return float(default)


def load_settings() -> Dict[str, Any]:
    defaults = {
        "dmx_target_ip": get_local_ip(),
        "sync_video": {
            "enabled": False,
            "base_url": "http://127.0.0.1:3000",
            "token": ""
        },
        "ctc": {
            "enabled": False,
            "keybind": "F8",
            "capture_release": False,
        },
        "whats_new": {
            "show_on_startup": True,
            "last_seen_version": "",
        },
        "auto_update": {
            "check_on_startup": True,
        },
        "ui": {
            "language": "en",
        },
        "cue_editor": {
            "view_mode": "classic",
            "timeline_priority_mode": "top",
            "zoom_x": 120.0,
            "zoom_y": 88.0,
            "rapidfire_loop": False,
        },
        "autolight": {
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
        },
        "dmx_runtime": {
            # Output refresh: the engine re-emits every universe at this rate,
            # like a DMX interface. preview_hz is what the browser preview gets.
            "emit_hz": _env_float("DMX_EMIT_HZ", 500),
            "preview_hz": _env_float("DMX_PREVIEW_HZ", 30),
            "playback_clock_mode": os.environ.get("DMX_PLAYBACK_CLOCK_MODE", "timeline").strip().lower(),
            "playback_engine_hz": _env_float("DMX_PLAYBACK_ENGINE_HZ", 120),
            "idle_engine_hz": _env_float("DMX_IDLE_ENGINE_HZ", 120),
            "playback_ui_fps": _env_float("DMX_PLAYBACK_UI_FPS", 12),
            "max_send_hz": _env_float("DMX_MAX_SEND_HZ", 120),
            "heartbeat_sec": _env_float("DMX_HEARTBEAT_SEC", 0.1),
            "artnet_diff": _env_bool("DMX_ARTNET_DIFF", False),
            "artnet_heartbeat_full": _env_bool("DMX_ARTNET_HEARTBEAT_FULL", True),
            "dummy_enabled": _env_bool("DMX_DUMMY", True),
            "smooth_step": int(_env_float("DMX_SMOOTH_STEP", 2)),
            "smooth_predict": _env_bool("DMX_SMOOTH_PREDICT", False),
            "smooth_disable": _env_bool("DMX_SMOOTH_DISABLE", False),
            "deadband": int(_env_float("DMX_DEADBAND", 0)),
            "quantize": int(_env_float("DMX_QUANTIZE", 1)),
            "continuous": _env_bool("DMX_CONTINUOUS", False),
            "ui_force_full_send": _env_bool("DMX_UI_FORCE_FULL_SEND", False),
            "log_ui": _env_bool("DMX_LOG_UI", False),
            "log_ui_full": _env_bool("DMX_LOG_UI_FULL", False),
            "log_dmx": _env_bool("DMX_LOG_DMX", False),
            "log_dmx_full": _env_bool("DMX_LOG_DMX_FULL", False),
            "log_artnet": _env_bool("DMX_LOG_ARTNET", False),
            "log_artnet_full": _env_bool("DMX_LOG_ARTNET_FULL", False),
            "profile_runner": _env_bool("DMX_PROFILE_RUNNER", False),
        },
    }
    if not os.path.exists(SETTINGS_PATH):
        return defaults
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            if isinstance(data.get("dmx_target_ip"), str) and data["dmx_target_ip"].strip():
                defaults["dmx_target_ip"] = data["dmx_target_ip"].strip()
            sync = data.get("sync_video")
            if isinstance(sync, dict):
                if isinstance(sync.get("enabled"), bool):
                    defaults["sync_video"]["enabled"] = sync["enabled"]
                base_url = sync.get("base_url") or sync.get("baseUrl")
                if isinstance(base_url, str) and base_url.strip():
                    defaults["sync_video"]["base_url"] = base_url.strip()
                if isinstance(sync.get("token"), str):
                    defaults["sync_video"]["token"] = sync["token"].strip()
            ctc = data.get("ctc")
            if isinstance(ctc, dict):
                if isinstance(ctc.get("enabled"), bool):
                    defaults["ctc"]["enabled"] = ctc["enabled"]
                keybind = ctc.get("keybind")
                if isinstance(keybind, str) and keybind.strip():
                    defaults["ctc"]["keybind"] = keybind.strip()
                if isinstance(ctc.get("capture_release"), bool):
                    defaults["ctc"]["capture_release"] = ctc["capture_release"]
            whats_new = data.get("whats_new")
            if isinstance(whats_new, dict):
                if isinstance(whats_new.get("show_on_startup"), bool):
                    defaults["whats_new"]["show_on_startup"] = whats_new["show_on_startup"]
                last_seen = whats_new.get("last_seen_version")
                if isinstance(last_seen, str):
                    defaults["whats_new"]["last_seen_version"] = last_seen.strip()
            auto_update = data.get("auto_update")
            if isinstance(auto_update, dict):
                if isinstance(auto_update.get("check_on_startup"), bool):
                    defaults["auto_update"]["check_on_startup"] = auto_update["check_on_startup"]
            ui = data.get("ui")
            if isinstance(ui, dict):
                lang = ui.get("language")
                if isinstance(lang, str) and lang.strip():
                    defaults["ui"]["language"] = lang.strip().lower()
            cue_editor = data.get("cue_editor")
            if isinstance(cue_editor, dict):
                view_mode = cue_editor.get("view_mode")
                if isinstance(view_mode, str) and view_mode.strip():
                    mode = view_mode.strip().lower()
                    defaults["cue_editor"]["view_mode"] = mode if mode in CUE_VIEW_MODES else "classic"
                priority = cue_editor.get("timeline_priority_mode")
                if isinstance(priority, str) and priority.strip():
                    prio = priority.strip().lower()
                    defaults["cue_editor"]["timeline_priority_mode"] = prio if prio in ("top", "bottom", "merge") else "top"
                if cue_editor.get("zoom_x") is not None:
                    defaults["cue_editor"]["zoom_x"] = _clamp_float(cue_editor.get("zoom_x"), 20.0, 480.0, defaults["cue_editor"]["zoom_x"])
                if cue_editor.get("zoom_y") is not None:
                    defaults["cue_editor"]["zoom_y"] = _clamp_float(cue_editor.get("zoom_y"), 48.0, 240.0, defaults["cue_editor"]["zoom_y"])
                if cue_editor.get("rapidfire_loop") is not None:
                    defaults["cue_editor"]["rapidfire_loop"] = bool(cue_editor.get("rapidfire_loop"))
            autolight = data.get("autolight")
            defaults["autolight"] = normalize_autolight_settings(autolight, defaults["autolight"])
            runtime = data.get("dmx_runtime")
            if isinstance(runtime, dict):
                defaults["dmx_runtime"].update(runtime)
    except Exception:
        pass
    return defaults


def save_settings(settings: Dict[str, Any]) -> None:
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


def _clamp_int(value: Any, min_val: int, max_val: int, default: int) -> int:
    try:
        v = int(float(value))
    except Exception:
        return default
    return max(min_val, min(max_val, v))


def _clamp_float(value: Any, min_val: float, max_val: float, default: float) -> float:
    try:
        v = float(value)
    except Exception:
        return default
    return max(min_val, min(max_val, v))


def _normalize_runtime_settings(payload: Any, current: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return current

    out = dict(current or {})

    # Output refresh rate (what the nodes actually see) + browser preview rate.
    out["emit_hz"] = _clamp_float(payload.get("emit_hz"), 1.0, 1000.0, out.get("emit_hz", 500.0))
    out["preview_hz"] = _clamp_float(payload.get("preview_hz"), 1.0, 120.0, out.get("preview_hz", 30.0))
    out.pop("render_mode", None)  # a single renderer now: the engine
    clock_mode_raw = str(payload.get("playback_clock_mode") or out.get("playback_clock_mode") or "timeline").strip().lower()
    out["playback_clock_mode"] = "absolute_clock" if clock_mode_raw == "absolute_clock" else "timeline"
    out["playback_engine_hz"] = _clamp_float(payload.get("playback_engine_hz"), 40.0, 240.0, out.get("playback_engine_hz", 120.0))
    out["idle_engine_hz"] = _clamp_float(payload.get("idle_engine_hz"), 40.0, 240.0, out.get("idle_engine_hz", 120.0))
    out["playback_ui_fps"] = _clamp_float(payload.get("playback_ui_fps"), 1.0, 60.0, out.get("playback_ui_fps", 12.0))

    out["max_send_hz"] = _clamp_float(payload.get("max_send_hz"), 1.0, 240.0, out.get("max_send_hz", 120))
    out["heartbeat_sec"] = _clamp_float(payload.get("heartbeat_sec"), 0.0, 5.0, out.get("heartbeat_sec", 0.1))
    out["artnet_diff"] = bool(payload.get("artnet_diff", out.get("artnet_diff", False)))
    out["artnet_heartbeat_full"] = bool(payload.get("artnet_heartbeat_full", out.get("artnet_heartbeat_full", True)))
    out["dummy_enabled"] = bool(payload.get("dummy_enabled", out.get("dummy_enabled", True)))
    out["smooth_step"] = _clamp_int(payload.get("smooth_step"), 1, 32, out.get("smooth_step", 2))
    out["smooth_predict"] = bool(payload.get("smooth_predict", out.get("smooth_predict", False)))
    out["smooth_disable"] = bool(payload.get("smooth_disable", out.get("smooth_disable", False)))
    out["deadband"] = _clamp_int(payload.get("deadband"), 0, 64, out.get("deadband", 0))
    out["quantize"] = _clamp_int(payload.get("quantize"), 1, 64, out.get("quantize", 1))
    out["continuous"] = bool(payload.get("continuous", out.get("continuous", False)))
    out["ui_force_full_send"] = bool(payload.get("ui_force_full_send", out.get("ui_force_full_send", False)))
    out["log_ui"] = bool(payload.get("log_ui", out.get("log_ui", False)))
    out["log_ui_full"] = bool(payload.get("log_ui_full", out.get("log_ui_full", False)))
    out["log_dmx"] = bool(payload.get("log_dmx", out.get("log_dmx", False)))
    out["log_dmx_full"] = bool(payload.get("log_dmx_full", out.get("log_dmx_full", False)))
    out["log_artnet"] = bool(payload.get("log_artnet", out.get("log_artnet", False)))
    out["log_artnet_full"] = bool(payload.get("log_artnet_full", out.get("log_artnet_full", False)))
    out["profile_runner"] = bool(payload.get("profile_runner", out.get("profile_runner", False)))

    return out


def _normalize_ctc_settings(payload: Any, current: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(current or {})
    if not isinstance(payload, dict):
        out["enabled"] = bool(out.get("enabled", False))
        out["keybind"] = str(out.get("keybind") or "F8").strip() or "F8"
        out["capture_release"] = bool(out.get("capture_release", False))
        return out

    out["enabled"] = bool(payload.get("enabled", out.get("enabled", False)))
    keybind = payload.get("keybind", out.get("keybind", "F8"))
    out["keybind"] = str(keybind or "F8").strip() or "F8"
    out["capture_release"] = bool(payload.get("capture_release", out.get("capture_release", False)))
    return out


def _normalize_whats_new_settings(payload: Any, current: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(current or {})
    if not isinstance(payload, dict):
        out["show_on_startup"] = bool(out.get("show_on_startup", True))
        out["last_seen_version"] = str(out.get("last_seen_version") or "").strip()
        return out

    out["show_on_startup"] = bool(payload.get("show_on_startup", out.get("show_on_startup", True)))
    if "last_seen_version" in payload:
        out["last_seen_version"] = str(payload.get("last_seen_version") or "").strip()
    else:
        out["last_seen_version"] = str(out.get("last_seen_version") or "").strip()
    return out


def _normalize_ui_settings(payload: Any, current: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(current or {})
    if not isinstance(payload, dict):
        out["language"] = str(out.get("language") or "en").strip().lower() or "en"
        return out
    lang = str(payload.get("language") or out.get("language") or "en").strip().lower() or "en"
    out["language"] = lang
    return out


def _normalize_auto_update_settings(payload: Any, current: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(current or {})
    if not isinstance(payload, dict):
        out["check_on_startup"] = bool(out.get("check_on_startup", True))
        return out
    out["check_on_startup"] = bool(payload.get("check_on_startup", out.get("check_on_startup", True)))
    return out


def _normalize_cue_editor_settings(payload: Any, current: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(current or {})
    if not isinstance(payload, dict):
        view_mode = str(out.get("view_mode") or "").strip().lower()
        out["view_mode"] = view_mode if view_mode in CUE_VIEW_MODES else "classic"
        priority = str(out.get("timeline_priority_mode") or "top").strip().lower()
        out["timeline_priority_mode"] = priority if priority in ("top", "bottom", "merge") else "top"
        out["zoom_x"] = _clamp_float(out.get("zoom_x"), 20.0, 480.0, 120.0)
        out["zoom_y"] = _clamp_float(out.get("zoom_y"), 48.0, 240.0, 88.0)
        out["rapidfire_loop"] = bool(out.get("rapidfire_loop", False))
        return out

    view_mode = str(payload.get("view_mode") or out.get("view_mode") or "classic").strip().lower()
    out["view_mode"] = view_mode if view_mode in CUE_VIEW_MODES else "classic"
    priority = str(payload.get("timeline_priority_mode") or out.get("timeline_priority_mode") or "top").strip().lower()
    out["timeline_priority_mode"] = priority if priority in ("top", "bottom", "merge") else "top"
    out["zoom_x"] = _clamp_float(payload.get("zoom_x"), 20.0, 480.0, out.get("zoom_x", 120.0))
    out["zoom_y"] = _clamp_float(payload.get("zoom_y"), 48.0, 240.0, out.get("zoom_y", 88.0))
    # Rapid Fire "Loop": pads fire their cue list on repeat until stopped.
    out["rapidfire_loop"] = bool(
        payload.get("rapidfire_loop", out.get("rapidfire_loop", False))
    )
    return out


def _normalize_autolight_settings(payload: Any, current: Dict[str, Any]) -> Dict[str, Any]:
    return normalize_autolight_settings(payload, current)


def _apply_runtime_settings(engine: Any, runtime: Dict[str, Any]) -> None:
    if not engine or not isinstance(runtime, dict):
        return

    try:
        if hasattr(engine, "_max_send_hz"):
            engine._max_send_hz = float(runtime.get("max_send_hz", engine._max_send_hz))
            engine._min_send_interval = 0.0 if engine._max_send_hz <= 0 else (1.0 / engine._max_send_hz)
        if hasattr(engine, "_heartbeat_sec"):
            engine._heartbeat_sec = float(runtime.get("heartbeat_sec", engine._heartbeat_sec))
        if hasattr(engine, "_artnet_diff"):
            engine._artnet_diff = bool(runtime.get("artnet_diff", engine._artnet_diff))
        if hasattr(engine, "_artnet_heartbeat_full"):
            engine._artnet_heartbeat_full = bool(runtime.get("artnet_heartbeat_full", engine._artnet_heartbeat_full))
        if hasattr(engine, "_dummy_enabled"):
            engine._dummy_enabled = bool(runtime.get("dummy_enabled", engine._dummy_enabled))
            if not engine._dummy_enabled and hasattr(engine, "set_dummy_channels"):
                engine.set_dummy_channels({})
        if hasattr(engine, "_smooth_step"):
            engine._smooth_step = int(runtime.get("smooth_step", engine._smooth_step))
        if hasattr(engine, "_smooth_predict"):
            engine._smooth_predict = bool(runtime.get("smooth_predict", getattr(engine, "_smooth_predict", False)))
        if hasattr(engine, "_smooth_disabled"):
            engine._smooth_disabled = bool(runtime.get("smooth_disable", getattr(engine, "_smooth_disabled", False)))
        if hasattr(engine, "_deadband"):
            engine._deadband = int(runtime.get("deadband", engine._deadband))
        if hasattr(engine, "_quantize"):
            engine._quantize = int(runtime.get("quantize", engine._quantize))
        if hasattr(engine, "_force_continuous"):
            engine._force_continuous = bool(runtime.get("continuous", engine._force_continuous))
        if hasattr(engine, "set_emit_hz"):
            engine.set_emit_hz(runtime.get("emit_hz", 500.0))
        if hasattr(engine, "set_preview_hz"):
            engine.set_preview_hz(runtime.get("preview_hz", 30.0))
        if hasattr(engine, "set_playback_clock_mode"):
            engine.set_playback_clock_mode(runtime.get("playback_clock_mode", "timeline"))
        elif hasattr(engine, "_playback_clock_mode"):
            engine._playback_clock_mode = str(runtime.get("playback_clock_mode", "timeline")).strip().lower()
        if hasattr(engine, "set_playback_engine_hz"):
            engine.set_playback_engine_hz(runtime.get("playback_engine_hz", 120.0))
        elif hasattr(engine, "_playback_engine_hz"):
            engine._playback_engine_hz = float(runtime.get("playback_engine_hz", 120.0))
        if hasattr(engine, "set_idle_engine_hz"):
            engine.set_idle_engine_hz(runtime.get("idle_engine_hz", 120.0))
        elif hasattr(engine, "_idle_engine_hz"):
            engine._idle_engine_hz = float(runtime.get("idle_engine_hz", 120.0))
        if hasattr(engine, "set_playback_ui_fps"):
            engine.set_playback_ui_fps(runtime.get("playback_ui_fps", 12.0))
        elif hasattr(engine, "_playback_ui_fps"):
            engine._playback_ui_fps = float(runtime.get("playback_ui_fps", 12.0))
        if hasattr(engine, "_log_dmx"):
            engine._log_dmx = bool(runtime.get("log_dmx", engine._log_dmx))
        if hasattr(engine, "_log_dmx_full"):
            engine._log_dmx_full = bool(runtime.get("log_dmx_full", engine._log_dmx_full))
        if hasattr(engine, "set_profile_runner"):
            engine.set_profile_runner(runtime.get("profile_runner", False))
        elif hasattr(engine, "_profile_runner"):
            engine._profile_runner = bool(runtime.get("profile_runner", False))
    except Exception as e:
        app.logger.exception("Failed to apply runtime settings: %s", e)

    # Update UI log flags (app-level)
    global LOG_UI_PAYLOADS, LOG_UI_FULL
    LOG_UI_PAYLOADS = bool(runtime.get("log_ui", LOG_UI_PAYLOADS))
    LOG_UI_FULL = bool(runtime.get("log_ui_full", LOG_UI_FULL))

    # Update ArtNet logger flags if possible
    try:
        import DMXE as DMXE_module
        DMXE_module.LOG_ARTNET = bool(runtime.get("log_artnet", getattr(DMXE_module, "LOG_ARTNET", False)))
        DMXE_module.LOG_ARTNET_FULL = bool(runtime.get("log_artnet_full", getattr(DMXE_module, "LOG_ARTNET_FULL", False)))
    except Exception:
        pass


SETTINGS = load_settings()
AUTOLIGHT = AutoLightService(SETTINGS.get("autolight") or {})
if not os.path.exists(SETTINGS_PATH):
    save_settings(SETTINGS)

# ---------- LOGGING ----------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

app = Flask(
    __name__,
    static_folder=os.path.join(RESOURCE_DIR, "static"),
    template_folder=os.path.join(RESOURCE_DIR, "templates"),
)
app.logger.setLevel(logging.DEBUG)
app.config["APP_NAME"] = APP_NAME
app.config["APP_VERSION"] = APP_VERSION
app.config["APP_LICENSE_CODE"] = APP_LICENSE_CODE

# ---------- DEBUG FLAGS ----------
LOG_UI_PAYLOADS = os.environ.get("DMX_LOG_UI", "0").strip().lower() in ("1", "true", "yes", "on")
LOG_UI_FULL = os.environ.get("DMX_LOG_UI_FULL", "0").strip().lower() in ("1", "true", "yes", "on")

# ---------- FILE LOGGING ----------
LOG_DIR = os.path.join(DATA_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

file_handler = logging.handlers.RotatingFileHandler(
    os.path.join(LOG_DIR, "ddmx.log"),
    maxBytes=2 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8"
)
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.getLogger().addHandler(file_handler)

# ---------- DMX RENDER ENGINE ----------
RENDER_ENGINE: Optional[DMXRenderEngine] = None
UPDATE_CALLBACKS: Dict[str, Any] = {
    "status": None,
    "check": None,
    "install": None,
}

def init_engine():
    global RENDER_ENGINE
    if DMXRenderEngine is not None:
        try:
            target_ip = SETTINGS.get("dmx_target_ip") or "127.0.0.1"
            RENDER_ENGINE = DMXRenderEngine(artnet_ip=target_ip, bind_ip="0.0.0.0")
            runtime = SETTINGS.get("dmx_runtime") or {}
            _apply_runtime_settings(RENDER_ENGINE, runtime)
            RENDER_ENGINE.start()
            app.logger.info("DMX Render Engine started (mode=%s).", ENGINE_MODE)
            try:
                AUTOLIGHT.attach_engine(RENDER_ENGINE)
            except Exception:
                app.logger.exception("AutoLight attach failed")
        except Exception as e:
            app.logger.exception("Render Engine init failed: %s", e)
            RENDER_ENGINE = None
    else:
        app.logger.info("DMX Render Engine not available.")

# SSE clients
_sse_clients: List[Queue] = []

def safe_parse_channels_map(raw: Any) -> Dict[int, int]:
    """Tolérant: ignore tout ce qui n'est pas clé int / valeur int."""
    out: Dict[int, int] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        ks = str(k)
        if ks.lower() == "universe":
            continue
        try:
            ch = int(ks)
            val = int(v) & 0xFF
        except Exception:
            continue
        if 0 <= ch < 512:
            out[ch] = val
    return out


# ---------- FIXTURES (XML) ----------
def load_all_fixtures() -> Dict[str, Any]:
    fixtures: Dict[str, Any] = {}
    for fname in sorted(os.listdir(FIXTURES_DIR)):
        lower = fname.lower()
        if not (lower.endswith(".xml") or lower.endswith(".fixture.json")):
            continue
        path = os.path.join(FIXTURES_DIR, fname)
        try:
            fixtures[fname] = load_fixture_file(path)
        except Exception as e:
            fixtures[fname] = {"error": str(e)}
    return fixtures


# ---------- CUE FILES ----------
def list_cue_files() -> List[str]:
    return sorted(
        f for f in os.listdir(CUE_DIR)
        if f.lower().endswith(".json")
    )


def load_cue_file(filename: str) -> Dict[str, Any]:
    path = os.path.join(CUE_DIR, filename)
    if not os.path.exists(path):
        raise FileNotFoundError(filename)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_cue_file(filename: str, data: Dict[str, Any]) -> None:
    path = os.path.join(CUE_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


# ---------- PROJECTS ----------
# A "project" bundles a rig (devices + calibration) and its cue lists into a
# single portable .ddmxproj file (JSON). The rig is owned by the project;
# switching cue lists within a project does NOT redefine the rig.
PROJECT_EXT = ".ddmxproj"
RECENT_PROJECTS_PATH = os.path.join(PROJECTS_DIR, "_recent.json")
MAX_RECENT_PROJECTS = 12


def _safe_project_name(filename: str) -> str:
    """Sanitize a project filename and force the .ddmxproj extension."""
    base = os.path.basename(str(filename or "")).strip()
    if not base:
        raise ValueError("empty project name")
    if not base.lower().endswith(PROJECT_EXT):
        base += PROJECT_EXT
    return base


def list_project_files() -> List[str]:
    return sorted(
        f for f in os.listdir(PROJECTS_DIR)
        if f.lower().endswith(PROJECT_EXT)
    )


def load_project_file(filename: str) -> Dict[str, Any]:
    path = os.path.join(PROJECTS_DIR, _safe_project_name(filename))
    if not os.path.exists(path):
        raise FileNotFoundError(filename)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_project_file(filename: str, data: Dict[str, Any]) -> str:
    name = _safe_project_name(filename)
    path = os.path.join(PROJECTS_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return name


def _read_recent_projects() -> List[str]:
    try:
        with open(RECENT_PROJECTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("recent") if isinstance(data, dict) else data
        if isinstance(items, list):
            # Drop entries whose file no longer exists.
            return [str(x) for x in items if os.path.exists(os.path.join(PROJECTS_DIR, str(x)))]
    except Exception:
        pass
    return []


def _push_recent_project(filename: str) -> None:
    name = _safe_project_name(filename)
    recent = [r for r in _read_recent_projects() if r != name]
    recent.insert(0, name)
    recent = recent[:MAX_RECENT_PROJECTS]
    try:
        with open(RECENT_PROJECTS_PATH, "w", encoding="utf-8") as f:
            json.dump({"recent": recent}, f, ensure_ascii=False)
    except Exception:
        app.logger.warning("[API] could not persist recent projects")


# ---------- FLASK ROUTES ----------

@app.route("/")
def index():
    runtime = SETTINGS.get("dmx_runtime") or {}
    force_full_send = bool(runtime.get("ui_force_full_send"))
    return render_template(
        "index.html",
        dmx_force_full_send=force_full_send,
        app_name=APP_NAME,
        app_version=APP_VERSION,
        app_license_code=APP_LICENSE_CODE,
        preferred_language=(SETTINGS.get("ui") or {}).get("language", "en"),
    )


@app.route("/api/meta", methods=["GET"])
def api_meta():
    return jsonify({
        "app_name": APP_NAME,
        "version": APP_VERSION,
        "license_code": APP_LICENSE_CODE,
        "releases_url": APP_UPDATE_RELEASES_URL,
    })


@app.route("/api/whats_new/current", methods=["GET"])
def api_whats_new_current():
    version_slug = APP_VERSION.replace(".", "_")
    template_name = f"whats_new/{version_slug}.html"
    try:
        html = render_template(
            template_name,
            app_name=APP_NAME,
            app_version=APP_VERSION,
            app_license_code=APP_LICENSE_CODE,
        )
    except Exception:
        html = render_template(
            "whats_new/fallback.html",
            app_name=APP_NAME,
            app_version=APP_VERSION,
            app_license_code=APP_LICENSE_CODE,
        )
    return jsonify({
        "version": APP_VERSION,
        "title": f"What's New in {APP_VERSION}?",
        "html": html,
    })


@app.route("/api/update/status", methods=["GET"])
def api_update_status():
    fn = UPDATE_CALLBACKS.get("status")
    if not callable(fn):
        return jsonify({
            "supported": False,
            "install_supported": False,
            "current_version": APP_VERSION,
            "error": "Update provider unavailable",
        })
    try:
        return jsonify(fn())
    except Exception as e:
        app.logger.exception("[UPDATE] status failed")
        return jsonify({
            "supported": False,
            "install_supported": False,
            "current_version": APP_VERSION,
            "error": str(e),
        }), 500


@app.route("/api/update/check", methods=["POST"])
def api_update_check():
    fn = UPDATE_CALLBACKS.get("check")
    if not callable(fn):
        return jsonify({"error": "Update provider unavailable"}), 503
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(fn(manual=bool(payload.get("manual", True))))
    except Exception as e:
        app.logger.exception("[UPDATE] check failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/update/install", methods=["POST"])
def api_update_install():
    fn = UPDATE_CALLBACKS.get("install")
    if not callable(fn):
        return jsonify({"error": "Update provider unavailable"}), 503
    try:
        return jsonify(fn())
    except Exception as e:
        app.logger.exception("[UPDATE] install failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/fixtures", methods=["GET"])
def api_fixtures():
    return jsonify(load_all_fixtures())


@app.route("/api/cue_files", methods=["GET"])
def api_cue_files():
    return jsonify({"files": list_cue_files()})


@app.route("/api/cues/<filename>", methods=["GET", "POST"])
def api_cues_file(filename: str):
    if ".." in filename or filename.startswith("/"):
        return jsonify({"error": "invalid filename"}), 400

    if request.method == "GET":
        try:
            data = load_cue_file(filename)
        except FileNotFoundError:
            return jsonify({"error": "not found"}), 404
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify(data)

    payload = request.get_json()
    if not isinstance(payload, dict):
        return jsonify({"error": "bad payload"}), 400
    try:
        save_cue_file(filename, payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


# ---------- PROJECTS API ----------

@app.route("/api/projects", methods=["GET"])
def api_projects_list():
    return jsonify({"files": list_project_files(), "recent": _read_recent_projects()})


@app.route("/api/projects/<filename>", methods=["GET", "POST", "DELETE"])
def api_project_file(filename: str):
    if ".." in filename or "/" in filename or "\\" in filename:
        return jsonify({"error": "invalid filename"}), 400

    if request.method == "GET":
        try:
            data = load_project_file(filename)
        except FileNotFoundError:
            return jsonify({"error": "not found"}), 404
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        _push_recent_project(filename)
        return jsonify(data)

    if request.method == "DELETE":
        try:
            path = os.path.join(PROJECTS_DIR, _safe_project_name(filename))
            if os.path.exists(path):
                os.remove(path)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True})

    payload = request.get_json()
    if not isinstance(payload, dict):
        return jsonify({"error": "bad payload"}), 400
    try:
        name = save_project_file(filename, payload)
        _push_recent_project(name)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "file": name})


@app.route("/api/projects/<filename>/download", methods=["GET"])
def api_project_download(filename: str):
    if ".." in filename or "/" in filename or "\\" in filename:
        return jsonify({"error": "invalid filename"}), 400
    name = _safe_project_name(filename)
    if not os.path.exists(os.path.join(PROJECTS_DIR, name)):
        return jsonify({"error": "not found"}), 404
    return send_from_directory(PROJECTS_DIR, name, as_attachment=True)


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    sync = SETTINGS.get("sync_video") or {}
    ctc = SETTINGS.get("ctc") or {}
    whats_new = SETTINGS.get("whats_new") or {}
    auto_update = SETTINGS.get("auto_update") or {}
    ui = SETTINGS.get("ui") or {}
    cue_editor = SETTINGS.get("cue_editor") or {}
    autolight = SETTINGS.get("autolight") or {}
    runtime = SETTINGS.get("dmx_runtime") or {}
    return jsonify({
        "dmx_target_ip": SETTINGS.get("dmx_target_ip", "127.0.0.1"),
        "local_ip": get_local_ip(),
        "sync_video": {
            "enabled": bool(sync.get("enabled")),
            "base_url": sync.get("base_url") or "http://127.0.0.1:3000",
            "token": sync.get("token") or ""
        },
        "ctc": {
            "enabled": bool(ctc.get("enabled")),
            "keybind": str(ctc.get("keybind") or "F8"),
            "capture_release": bool(ctc.get("capture_release")),
        },
        "whats_new": {
            "show_on_startup": bool(whats_new.get("show_on_startup", True)),
            "last_seen_version": str(whats_new.get("last_seen_version") or ""),
        },
        "auto_update": {
            "check_on_startup": bool(auto_update.get("check_on_startup", True)),
        },
        "ui": {
            "language": str(ui.get("language") or "en"),
        },
        "cue_editor": {
            "view_mode": str(cue_editor.get("view_mode") or "classic"),
            "timeline_priority_mode": str(cue_editor.get("timeline_priority_mode") or "top"),
            "zoom_x": float(cue_editor.get("zoom_x") or 120.0),
            "zoom_y": float(cue_editor.get("zoom_y") or 88.0),
            "rapidfire_loop": bool(cue_editor.get("rapidfire_loop", False)),
        },
        "autolight": autolight,
        "autolight_status": AUTOLIGHT.get_status(force_refresh=False),
        "dmx_runtime": runtime
    })


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    payload = request.get_json() or {}
    ip = payload.get("dmx_target_ip") or payload.get("dmx_ip")
    engine_updated = False
    if ip is not None:
        if not isinstance(ip, str) or not ip.strip():
            return jsonify({"error": "invalid dmx_target_ip"}), 400
        ip = ip.strip()
        SETTINGS["dmx_target_ip"] = ip
        if RENDER_ENGINE is not None and hasattr(RENDER_ENGINE, "set_artnet_target"):
            try:
                engine_updated = bool(RENDER_ENGINE.set_artnet_target(ip))
            except Exception as e:
                app.logger.exception("Failed to update DMX target IP: %s", e)

    sync_payload = payload.get("sync_video")
    if isinstance(sync_payload, dict):
        sync = SETTINGS.get("sync_video") or {}
        enabled = sync_payload.get("enabled")
        if isinstance(enabled, bool):
            sync["enabled"] = enabled
        base_url = sync_payload.get("base_url") or sync_payload.get("baseUrl")
        if isinstance(base_url, str):
            sync["base_url"] = base_url.strip()
        token = sync_payload.get("token")
        if isinstance(token, str):
            sync["token"] = token.strip()
        SETTINGS["sync_video"] = sync

    ctc_payload = payload.get("ctc")
    ctc = SETTINGS.get("ctc") or {}
    ctc = _normalize_ctc_settings(ctc_payload, ctc)
    SETTINGS["ctc"] = ctc

    whats_new_payload = payload.get("whats_new")
    whats_new = SETTINGS.get("whats_new") or {}
    whats_new = _normalize_whats_new_settings(whats_new_payload, whats_new)
    SETTINGS["whats_new"] = whats_new

    auto_update_payload = payload.get("auto_update")
    auto_update = SETTINGS.get("auto_update") or {}
    auto_update = _normalize_auto_update_settings(auto_update_payload, auto_update)
    SETTINGS["auto_update"] = auto_update

    ui_payload = payload.get("ui")
    ui = SETTINGS.get("ui") or {}
    ui = _normalize_ui_settings(ui_payload, ui)
    SETTINGS["ui"] = ui

    cue_editor_payload = payload.get("cue_editor")
    cue_editor = SETTINGS.get("cue_editor") or {}
    cue_editor = _normalize_cue_editor_settings(cue_editor_payload, cue_editor)
    SETTINGS["cue_editor"] = cue_editor

    autolight_payload = payload.get("autolight")
    autolight = SETTINGS.get("autolight") or {}
    autolight = _normalize_autolight_settings(autolight_payload, autolight)
    SETTINGS["autolight"] = autolight
    AUTOLIGHT.apply_settings(autolight)

    runtime_payload = payload.get("dmx_runtime")
    runtime = SETTINGS.get("dmx_runtime") or {}
    runtime = _normalize_runtime_settings(runtime_payload, runtime)
    SETTINGS["dmx_runtime"] = runtime

    if RENDER_ENGINE is not None:
        _apply_runtime_settings(RENDER_ENGINE, runtime)

    save_settings(SETTINGS)

    return jsonify({
        "ok": True,
        "dmx_target_ip": SETTINGS.get("dmx_target_ip", "127.0.0.1"),
        "engineUpdated": engine_updated,
        "sync_video": SETTINGS.get("sync_video") or {},
        "ctc": SETTINGS.get("ctc") or {},
        "whats_new": SETTINGS.get("whats_new") or {},
        "auto_update": SETTINGS.get("auto_update") or {},
        "ui": SETTINGS.get("ui") or {},
        "cue_editor": SETTINGS.get("cue_editor") or {},
        "autolight": SETTINGS.get("autolight") or {},
        "autolight_status": AUTOLIGHT.get_status(force_refresh=False),
        "dmx_runtime": SETTINGS.get("dmx_runtime") or {}
    })


@app.route("/api/autolight/status", methods=["GET"])
def api_autolight_status():
    force_refresh = str(request.args.get("refresh") or "").strip().lower() in ("1", "true", "yes", "on")
    return jsonify(AUTOLIGHT.get_status(force_refresh=force_refresh))


@app.route("/api/autolight/audio", methods=["GET"])
def api_autolight_audio():
    """Lightweight audio snapshot for high-frequency spectrogram polling.

    Cheaper than /api/autolight/status — no player discovery, no music service
    calls — so the UI can poll it at 20-30 Hz without overhead."""
    return jsonify(AUTOLIGHT.get_audio_snapshot())


@app.route("/api/autolight/control", methods=["POST"])
def api_autolight_control():
    payload = request.get_json(silent=True) or {}
    status = AUTOLIGHT.control(payload)
    SETTINGS["autolight"] = AUTOLIGHT.get_settings()
    save_settings(SETTINGS)
    return jsonify({
        "ok": True,
        "autolight": SETTINGS["autolight"],
        "status": status,
    })


@app.route("/api/autolight/snapshots", methods=["GET", "POST"])
def api_autolight_snapshots():
    if request.method == "GET":
        return jsonify({"items": AUTOLIGHT.list_snapshots()})
    payload = request.get_json(silent=True) or {}
    item = AUTOLIGHT.create_snapshot(payload)
    return jsonify({"ok": True, "item": item, "items": AUTOLIGHT.list_snapshots()})


@app.route("/api/autolight/effects", methods=["GET"])
def api_autolight_effects():
    return jsonify({"items": AUTOLIGHT.list_effects(), "moods": AUTOLIGHT.list_moods()})


@app.route("/api/autolight/audio-tuning", methods=["GET", "POST"])
def api_autolight_audio_tuning():
    if request.method == "GET":
        return jsonify(AUTOLIGHT.get_audio_tuning())
    payload = request.get_json(silent=True) or {}
    tuning = payload.get("tuning") if isinstance(payload.get("tuning"), dict) else payload
    current = AUTOLIGHT.get_settings()
    merged = dict(current.get("audio_tuning") or {})
    if isinstance(tuning, dict):
        merged.update({k: v for k, v in tuning.items() if isinstance(k, str)})
    current["audio_tuning"] = merged
    settings = AUTOLIGHT.apply_settings(current)
    SETTINGS["autolight"] = settings
    save_settings(SETTINGS)
    return jsonify({"ok": True, "autolight": settings, **AUTOLIGHT.get_audio_tuning()})


@app.route("/api/autolight/mood-filter", methods=["POST"])
def api_autolight_mood_filter():
    payload = request.get_json(silent=True) or {}
    moods = payload.get("moods") or payload.get("mood_filter") or []
    if not isinstance(moods, list):
        moods = [str(moods)]
    current = AUTOLIGHT.get_settings()
    current["mood_filter"] = moods
    settings = AUTOLIGHT.apply_settings(current)
    SETTINGS["autolight"] = settings
    save_settings(SETTINGS)
    return jsonify({"ok": True, "autolight": settings, "moods": AUTOLIGHT.list_moods()})


@app.route("/api/autolight/tap-tempo", methods=["POST"])
def api_autolight_tap_tempo():
    payload = request.get_json(silent=True) or {}
    raw = payload.get("bpm")
    if raw in (None, "", 0):
        bpm: Optional[float] = None
    else:
        try:
            bpm = float(raw)
        except Exception:
            return jsonify({"error": "invalid bpm"}), 400
    settings = AUTOLIGHT.apply_tap_tempo(bpm)
    SETTINGS["autolight"] = settings
    save_settings(SETTINGS)
    return jsonify({"ok": True, "autolight": settings})


@app.route("/api/autolight/scene-lock", methods=["POST"])
def api_autolight_scene_lock():
    payload = request.get_json(silent=True) or {}
    scene = payload.get("scene")
    duration = float(payload.get("duration_s") or 30.0)
    settings = AUTOLIGHT.apply_scene_lock(scene, duration)
    SETTINGS["autolight"] = settings
    save_settings(SETTINGS)
    return jsonify({"ok": True, "autolight": settings})


@app.route("/api/autolight/genres", methods=["GET"])
def api_autolight_genres():
    return jsonify({"items": AUTOLIGHT.list_genres(), "current": AUTOLIGHT.get_settings().get("genre_preset", "auto")})


@app.route("/api/autolight/genre-preset", methods=["POST"])
def api_autolight_genre_preset():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "auto").strip().lower()
    settings = AUTOLIGHT.apply_genre_preset(name)
    SETTINGS["autolight"] = settings
    save_settings(SETTINGS)
    return jsonify({"ok": True, "autolight": settings})


@app.route("/api/autolight/identify", methods=["POST"])
def api_autolight_identify():
    payload = request.get_json(silent=True) or {}
    dev_id = str(payload.get("device_id") or "").strip()
    if not dev_id:
        return jsonify({"error": "device_id required"}), 400
    duration = float(payload.get("duration_s") or 2.0)
    ok = AUTOLIGHT.identify_device(dev_id, duration)
    if not ok:
        return jsonify({"error": f"cannot identify {dev_id}"}), 404
    return jsonify({"ok": True, "device_id": dev_id, "duration_s": duration})


@app.route("/api/autolight/calibrate", methods=["POST"])
def api_autolight_calibrate():
    payload = request.get_json(silent=True) or {}
    duration = float(payload.get("duration_s") or 30.0)
    result = AUTOLIGHT.start_calibration(duration)
    return jsonify(result)


@app.route("/api/autolight/effects/config", methods=["POST"])
def api_autolight_effects_config():
    payload = request.get_json(silent=True) or {}
    cfg = payload.get("effect_config")
    if not isinstance(cfg, dict):
        return jsonify({"error": "effect_config must be an object"}), 400
    current = AUTOLIGHT.get_settings()
    current["effect_config"] = cfg
    settings = AUTOLIGHT.apply_settings(current)
    SETTINGS["autolight"] = settings
    save_settings(SETTINGS)
    return jsonify({"ok": True, "autolight": settings, "items": AUTOLIGHT.list_effects()})


@app.route("/api/autolight/effects/trigger", methods=["POST"])
def api_autolight_effects_trigger():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    ok = AUTOLIGHT.force_trigger_effect(name)
    if not ok:
        return jsonify({"error": f"unknown effect: {name}"}), 404
    return jsonify({"ok": True, "triggered": name, "status": AUTOLIGHT.get_status()})


@app.route("/api/autolight/training/status", methods=["GET"])
def api_autolight_training_status():
    return jsonify(AUTOLIGHT.training.status())


@app.route("/api/autolight/training/moves", methods=["GET"])
def api_autolight_training_moves():
    """Static metadata about all compositional moves. Fetched once when the
    training modal opens to render the score table with proper labels +
    intent badges."""
    return jsonify({"items": AUTOLIGHT.training.list_moves()})


@app.route("/api/autolight/training/control", methods=["POST"])
def api_autolight_training_control():
    """Toggle training mode. When enabling, also auto-enable memory_persistence
    so the satisfaction signal actually has a TrackMemory to write into —
    a training toggle that silently does nothing because of an unrelated
    setting would be a UX trap."""
    payload = request.get_json(silent=True) or {}
    if "enabled" in payload:
        enabled = bool(payload.get("enabled"))
        AUTOLIGHT.training.set_enabled(enabled)
        if enabled:
            updated = AUTOLIGHT.control({"memory_persistence": True})
            SETTINGS["autolight"] = AUTOLIGHT.get_settings()
            save_settings(SETTINGS)
            return jsonify({"ok": True, "training": AUTOLIGHT.training.status(), "autolight_status": updated})
    return jsonify({"ok": True, "training": AUTOLIGHT.training.status()})


@app.route("/api/autolight/training/satisfaction", methods=["POST"])
def api_autolight_training_satisfaction():
    """Hot path: called at ~10 Hz by the modal slider while the user drags.
    Kept minimal — no settings save, no status_cache rebuild, just a
    direct write to the in-memory satisfaction log."""
    payload = request.get_json(silent=True) or {}
    raw = payload.get("value", 0.0)
    result = AUTOLIGHT.training.record_satisfaction(raw)
    return jsonify(result)


@app.route("/api/autolight/training/library", methods=["GET", "POST", "DELETE"])
def api_autolight_training_library():
    if request.method == "GET":
        return jsonify({"items": AUTOLIGHT.training.list_library()})
    if request.method == "DELETE":
        AUTOLIGHT.training.clear_library()
        return jsonify({"ok": True, "items": []})
    payload = request.get_json(silent=True) or {}
    raw_paths = payload.get("paths") or []
    if not isinstance(raw_paths, list):
        return jsonify({"error": "paths must be a list"}), 400
    recursive = bool(payload.get("recursive", True))
    discovered = []
    errors = []
    for raw in raw_paths:
        path = str(raw or "").strip()
        if not path:
            continue
        try:
            discovered.extend(AUTOLIGHT.training.scan_path(path, recursive=recursive))
        except FileNotFoundError:
            errors.append({"path": path, "error": "not_found"})
        except Exception as exc:
            errors.append({"path": path, "error": str(exc)})
    added = AUTOLIGHT.training.add_entries(discovered)
    return jsonify({
        "ok": True,
        "added": added,
        "scanned": len(discovered),
        "errors": errors,
        "items": AUTOLIGHT.training.list_library(),
    })


@app.route("/api/autolight/training/library/<track_id>", methods=["DELETE"])
def api_autolight_training_library_remove(track_id):
    removed = AUTOLIGHT.training.remove_track(track_id)
    return jsonify({"ok": removed, "items": AUTOLIGHT.training.list_library()})


@app.route("/api/autolight/training/devices", methods=["GET"])
def api_autolight_training_devices():
    """Devices the camera-calibration loop can iterate, plus their currently
    stored camera-frame positions (if any). Single fetch for the modal's
    'Calibrate' phase to know what to walk."""
    engine = getattr(AUTOLIGHT, "_engine", None)
    devices_list = []
    if engine is not None:
        engine_devices = getattr(engine, "_devices", None) or {}
        positions = AUTOLIGHT.training.get_camera_positions()
        for dev_id, dev in engine_devices.items():
            caps = getattr(dev, "capabilities", None) or {}
            if not (caps.get("has_dimmer") or caps.get("has_color")):
                continue
            pos = positions.get(str(dev_id))
            devices_list.append({
                "device_id": str(dev_id),
                "cname": str(getattr(dev, "cname", "") or ""),
                "fixture": str(getattr(dev, "fixture_template", "") or ""),
                "universe": int(getattr(dev, "universe", 0) or 0),
                "address": int(getattr(dev, "base_address", 0) or 0),
                "has_movement": bool(caps.get("has_movement")),
                "strobe_friendly": bool(caps.get("strobe_friendly")),
                "x": pos.get("x") if pos else None,
                "y": pos.get("y") if pos else None,
                "captured_at_ms": pos.get("captured_at_ms") if pos else None,
            })
    return jsonify({"items": devices_list})


@app.route("/api/autolight/training/identify", methods=["POST"])
def api_autolight_training_identify():
    """Flash a single fixture so the browser can capture its pixel position
    from the live webcam. Duration defaults to 1.5s — long enough to be
    captured even with browser frame-rate jitter, short enough that the
    full calibration loop completes in a reasonable time."""
    payload = request.get_json(silent=True) or {}
    device_id = str(payload.get("device_id") or "").strip()
    if not device_id:
        return jsonify({"error": "device_id required"}), 400
    try:
        duration_s = float(payload.get("duration_s") or 1.5)
    except (TypeError, ValueError):
        duration_s = 1.5
    duration_s = max(0.5, min(5.0, duration_s))
    ok = AUTOLIGHT.training.identify_fixture(device_id, duration_s)
    return jsonify({"ok": ok, "device_id": device_id, "duration_s": duration_s})


@app.route("/api/autolight/training/camera-position", methods=["POST", "DELETE"])
def api_autolight_training_camera_position():
    """Browser → server: 'I just identified fixture X at (x, y) of the
    video frame.' Stored persistently so positions survive restarts."""
    if request.method == "DELETE":
        AUTOLIGHT.training.clear_camera_positions()
        return jsonify({"ok": True, "positions": {}})
    payload = request.get_json(silent=True) or {}
    device_id = str(payload.get("device_id") or "").strip()
    if not device_id:
        return jsonify({"error": "device_id required"}), 400
    try:
        x = float(payload.get("x"))
        y = float(payload.get("y"))
    except (TypeError, ValueError):
        return jsonify({"error": "x and y must be numeric in [0,1]"}), 400
    ok = AUTOLIGHT.training.set_camera_position(device_id, x, y)
    return jsonify({"ok": ok, "positions": AUTOLIGHT.training.get_camera_positions()})


@app.route("/api/autolight/training/play", methods=["POST"])
def api_autolight_training_play():
    """Hand a library file to the OS's default audio player. Cheapest path
    to "playback through the app" without bundling decoders."""
    payload = request.get_json(silent=True) or {}
    track_id = str(payload.get("track_id") or "").strip()
    path = AUTOLIGHT.training.lookup_path(track_id) if track_id else None
    if path is None:
        explicit = str(payload.get("path") or "").strip()
        if explicit:
            path = explicit
    if not path:
        return jsonify({"error": "track_id or path required"}), 400
    ok = AUTOLIGHT.training.open_in_os_player(path)
    return jsonify({"ok": ok, "path": path})


@app.route("/api/autolight/audio-devices", methods=["GET", "POST"])
def api_autolight_audio_devices():
    if request.method == "GET":
        devices = AUTOLIGHT.list_audio_devices()
        current = AUTOLIGHT.get_settings().get("audio_device_index")
        return jsonify({"items": devices, "current": current})
    payload = request.get_json(silent=True) or {}
    raw = payload.get("index")
    if raw is None or raw == "" or raw == "default":
        index: Optional[int] = None
    else:
        try:
            index = int(raw)
        except Exception:
            return jsonify({"error": "invalid index"}), 400
    settings = AUTOLIGHT.set_audio_device(index)
    SETTINGS["autolight"] = settings
    save_settings(SETTINGS)
    return jsonify({"ok": True, "autolight": settings, "status": AUTOLIGHT.get_status()})


# ---------- NEW API: JS LOGGING ----------

@app.route("/api/js_log", methods=["POST"])
def api_js_log():
    payload = request.get_json() or {}
    try:
        level = str(payload.get("level") or "error").lower()
        msg = str(payload.get("message") or "")
        src = str(payload.get("source") or "")
        line = payload.get("line")
        col = payload.get("column")
        stack = str(payload.get("stack") or "")
        extra = {
            "source": src,
            "line": line,
            "column": col
        }
        if level == "warn":
            app.logger.warning("[JS] %s | %s | stack=%s", msg, extra, stack)
        else:
            app.logger.error("[JS] %s | %s | stack=%s", msg, extra, stack)
    except Exception:
        app.logger.exception("[JS] log error")
    return jsonify({"ok": True})


# ---------- NEW API: RIG REGISTER ----------

@app.route("/api/rig/register", methods=["POST"])
def api_rig_register():
    if RENDER_ENGINE is None:
        return jsonify({"error": "engine not running"}), 503
    payload = request.get_json() or {}
    devices = payload.get("devices")
    replace = bool(payload.get("replace"))
    if not isinstance(devices, list):
        return jsonify({"error": "invalid devices"}), 400
    try:
        if hasattr(RENDER_ENGINE, "register_rig_devices"):
            RENDER_ENGINE.register_rig_devices(devices, replace=replace)
        return jsonify({"ok": True, "count": len(devices)})
    except Exception as e:
        app.logger.exception("[API] rig/register error")
        return jsonify({"error": str(e)}), 500


@app.route("/api/rig/reset", methods=["POST"])
def api_rig_reset():
    """Clear the entire rig and zero all output (new/blank project)."""
    if RENDER_ENGINE is None:
        return jsonify({"error": "engine not running"}), 503
    try:
        if hasattr(RENDER_ENGINE, "reset_rig"):
            RENDER_ENGINE.reset_rig()
        return jsonify({"ok": True})
    except Exception as e:
        app.logger.exception("[API] rig/reset error")
        return jsonify({"error": str(e)}), 500


# ---------- NEW API: LIVE CONTROL ----------

@app.route("/api/live/channels", methods=["POST"])
def api_live_channels():
    """Set channel values directly (live edit from controller)"""
    if RENDER_ENGINE is None:
        return jsonify({"error": "engine not running"}), 503

    payload = request.get_json()
    if not payload:
        return jsonify({"error": "no json"}), 400

    try:
        universe = int(payload.get("universe", 0))
        channels = safe_parse_channels_map(payload.get("channels") or {})
        device_id = payload.get("device_id", "live")

        if LOG_UI_PAYLOADS:
            if LOG_UI_FULL:
                app.logger.debug("[UI] live/channels raw=%s", payload)
            else:
                keys = list(channels.keys())
                sample = {k: channels[k] for k in keys[:8]}
                app.logger.debug(
                    "[UI] live/channels device=%s universe=%s count=%s sample=%s",
                    device_id, universe, len(channels), sample
                )

        for ch, val in channels.items():
            RENDER_ENGINE.set_channel(device_id, universe, ch, val)

        return jsonify({"ok": True})
    except Exception as e:
        app.logger.exception("[API] live/channels error")
        return jsonify({"error": str(e)}), 500


@app.route("/api/live/channels/bulk", methods=["POST"])
def api_live_channels_bulk():
    """Atomic multi-universe live write.

    Payload: {"device_id": "...", "universes": {"0": {"5": 127, ...}, "1": {...}}}

    All channel writes across universes commit under a single engine lock so
    that the next render frame sees one consistent snapshot — required to keep
    fixtures spread across multiple universes in lock-step on live edits."""
    if RENDER_ENGINE is None:
        return jsonify({"error": "engine not running"}), 503

    payload = request.get_json()
    if not payload:
        return jsonify({"error": "no json"}), 400

    try:
        device_id = payload.get("device_id", "live")
        raw_unis = payload.get("universes") or {}
        if not isinstance(raw_unis, dict):
            return jsonify({"error": "universes must be a dict"}), 400

        updates: Dict[int, Dict[int, int]] = {}
        total_channels = 0
        for uni_key, ch_map in raw_unis.items():
            try:
                uni = int(uni_key)
            except (TypeError, ValueError):
                continue
            channels = safe_parse_channels_map(ch_map)
            if not channels:
                continue
            updates[uni] = channels
            total_channels += len(channels)

        if not updates:
            return jsonify({"ok": True, "applied": 0})

        if LOG_UI_PAYLOADS:
            if LOG_UI_FULL:
                app.logger.debug("[UI] live/channels/bulk raw=%s", payload)
            else:
                app.logger.debug(
                    "[UI] live/channels/bulk device=%s universes=%s channels=%s",
                    device_id, list(updates.keys()), total_channels,
                )

        RENDER_ENGINE.set_channels_multi(device_id, updates)
        return jsonify({"ok": True, "applied": total_channels})
    except Exception as e:
        app.logger.exception("[API] live/channels/bulk error")
        return jsonify({"error": str(e)}), 500


# ---------- NEW API: EFFECT GROUPS ----------

@app.route("/api/live/attrs", methods=["POST"])
def api_live_attrs():
    """Hold / release fixture attributes — the UI's only way to drive values.

    Payload: {"updates": [{"device_id": "3", "attr": "main.dimmer", "value": 200}, ...]}
    A null value releases that attribute. Also accepts {"release": ["3", "4"]}
    and {"release_all": true}. The engine resolves attr -> DMX channel from the
    device's attr_map, so the browser never deals in channels.
    """
    if RENDER_ENGINE is None:
        return jsonify({"error": "engine not running"}), 503

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "bad payload"}), 400

    try:
        applied = 0
        released = 0
        if payload.get("release_all"):
            released += RENDER_ENGINE.release_manual_attrs(None)
        release = payload.get("release")
        if isinstance(release, list) and release:
            released += RENDER_ENGINE.release_manual_attrs([str(x) for x in release])
        updates = payload.get("updates")
        if isinstance(updates, list) and updates:
            applied = RENDER_ENGINE.set_manual_attrs(updates)
        return jsonify({"ok": True, "applied": applied, "released": released})
    except Exception as e:
        app.logger.exception("[API] live/attrs error")
        return jsonify({"error": str(e)}), 500


@app.route("/api/live/effects/groups", methods=["POST"])
def api_live_effect_groups():
    if RENDER_ENGINE is None:
        return jsonify({"error": "engine not running"}), 503
    payload = request.get_json() or {}
    action = str(payload.get("action") or "set").lower()
    groups = payload.get("groups") or []
    group_ids = payload.get("group_ids") or payload.get("groupIds") or []
    try:
        if hasattr(RENDER_ENGINE, "set_live_effect_groups"):
            RENDER_ENGINE.set_live_effect_groups(groups, action=action, group_ids=group_ids)
        return jsonify({"ok": True})
    except Exception as e:
        app.logger.exception("[API] live/effects/groups error")
        return jsonify({"error": str(e)}), 500


@app.route("/api/live/effects/groups/purge", methods=["POST"])
def api_live_effect_groups_purge():
    """Purge group id(s) from both live and cue effect pools.
    Used when the UI explicitly deletes an effect while a cue is playing,
    so the deletion takes effect immediately instead of waiting for the cue
    to end. Body: {"group_ids": ["g1", "g2"]} or {"group_id": "g1"}.
    """
    if RENDER_ENGINE is None:
        return jsonify({"error": "engine not running"}), 503
    payload = request.get_json() or {}
    group_ids = payload.get("group_ids") or payload.get("groupIds") or []
    if not group_ids and payload.get("group_id"):
        group_ids = [payload.get("group_id")]
    try:
        removed = 0
        if hasattr(RENDER_ENGINE, "remove_effect_group_everywhere"):
            removed = int(RENDER_ENGINE.remove_effect_group_everywhere(group_ids) or 0)
        return jsonify({"ok": True, "removed": removed})
    except Exception as e:
        app.logger.exception("[API] live/effects/groups/purge error")
        return jsonify({"error": str(e)}), 500


# ---------- NEW API: PLAYBACK ----------

@app.route("/api/playback/run", methods=["POST"])
def api_playback_run():
    """Execute a full cue sequence in the backend scheduler."""
    if RENDER_ENGINE is None:
        return jsonify({"error": "engine not running"}), 503

    payload = request.get_json()
    if not payload:
        return jsonify({"error": "no json"}), 400

    try:
        mode = str(payload.get("mode") or "classic").strip().lower()
        sequence = payload.get("sequence") or []
        timeline = payload.get("timeline") or payload.get("blocks") or []
        start_index = int(payload.get("start_index", 0) or 0)
        start_ms = int(payload.get("start_ms", 0) or 0)
        paused = bool(payload.get("paused", False))
        speed = payload.get("speed", 1.0)
        priority_mode = str(payload.get("priority_mode") or "top").strip().lower()
        # Whole-sequence loop: loop_count 0/absent = forever.
        loop = bool(payload.get("loop", False))
        loop_count = _clamp_int(payload.get("loop_count"), 0, 9999, 0) if payload.get("loop_count") is not None else 0
        virtual_groups = payload.get("virtual_groups") or payload.get("virtualGroups") or {}
        if not isinstance(sequence, list):
            return jsonify({"error": "invalid sequence"}), 400
        if timeline and not isinstance(timeline, list):
            return jsonify({"error": "invalid timeline"}), 400
        if not isinstance(virtual_groups, dict):
            virtual_groups = {}
        if priority_mode not in ("top", "bottom", "merge"):
            priority_mode = "top"
        try:
            RENDER_ENGINE.run_sequence(
                sequence,
                start_index=start_index,
                virtual_groups=virtual_groups,
                speed=speed,
                mode=mode,
                timeline=timeline,
                priority_mode=priority_mode,
                start_ms=start_ms,
                paused=paused,
                loop=loop,
                loop_count=loop_count,
            )
        except TypeError:
            if mode == "timeline":
                return jsonify({"error": "timeline playback is unavailable with the active engine"}), 400
            RENDER_ENGINE.run_sequence(sequence, start_index=start_index, virtual_groups=virtual_groups, speed=speed)
        return jsonify({
            "ok": True,
            "mode": mode,
            "count": len(timeline) if mode == "timeline" else len(sequence),
            "start_index": start_index,
            "start_ms": start_ms,
            "paused": paused,
            "speed": speed,
            "priority_mode": priority_mode,
            "loop": loop,
            "loop_count": loop_count,
        })
    except Exception as e:
        app.logger.exception("[API] playback/run error")
        return jsonify({"error": str(e)}), 500


@app.route("/api/playback/go", methods=["POST"])
def api_playback_go():
    """Execute a cue with fade"""
    if RENDER_ENGINE is None:
        return jsonify({"error": "engine not running"}), 503

    payload = request.get_json()
    if not payload:
        return jsonify({"error": "no json"}), 400

    try:
        cue_data = payload.get("cue", payload)
        device_order = payload.get("device_order")

        RENDER_ENGINE.go_cue(cue_data, device_order)
        return jsonify({"ok": True})
    except Exception as e:
        app.logger.exception("[API] playback/go error")
        return jsonify({"error": str(e)}), 500


@app.route("/api/playback/control", methods=["POST"])
def api_playback_control():
    """Control backend playback state."""
    if RENDER_ENGINE is None:
        return jsonify({"error": "engine not running"}), 503

    payload = request.get_json() or {}
    action = str(payload.get("action") or "").strip().lower()
    delta_ms = int(payload.get("delta_ms", 0) or 0)
    seek_ms = int(payload.get("seek_ms", delta_ms) or 0)
    if not action:
        return jsonify({"error": "missing action"}), 400

    try:
        if hasattr(RENDER_ENGINE, "playback_control"):
            if action == "seek":
                RENDER_ENGINE.playback_control(action, delta_ms=seek_ms)
            else:
                RENDER_ENGINE.playback_control(action, delta_ms=delta_ms)
        return jsonify({"ok": True})
    except Exception as e:
        app.logger.exception("[API] playback/control error")
        return jsonify({"error": str(e)}), 500


@app.route("/api/playback/stop", methods=["POST"])
def api_playback_stop():
    """Stop current playback"""
    if RENDER_ENGINE is None:
        return jsonify({"error": "engine not running"}), 503

    RENDER_ENGINE.stop_playback()
    return jsonify({"ok": True})


# ---------- NEW API: IDENTIFY ----------

@app.route("/api/identify/start", methods=["POST"])
def api_identify_start():
    """Start identify mode for devices"""
    if RENDER_ENGINE is None:
        return jsonify({"error": "engine not running"}), 503

    payload = request.get_json()
    if not payload:
        return jsonify({"error": "no json"}), 400

    try:
        devices = payload.get("devices", [])
        if not isinstance(devices, list):
            devices = [devices]

        # devices format: [{"device_id": "...", "universe": 0, "dimmer_channel": 0}, ...]
        RENDER_ENGINE.start_identify(devices)
        return jsonify({"ok": True})
    except Exception as e:
        app.logger.exception("[API] identify/start error")
        return jsonify({"error": str(e)}), 500


@app.route("/api/identify/stop", methods=["POST"])
def api_identify_stop():
    """Stop identify mode"""
    if RENDER_ENGINE is None:
        return jsonify({"error": "engine not running"}), 503

    RENDER_ENGINE.stop_identify()
    return jsonify({"ok": True})


# ---------- NEW API: MOVEMENT CHANNELS (PAN/TILT) ----------

@app.route("/api/movement_channels", methods=["POST"])
def api_movement_channels():
    """Set movement (pan/tilt) channels by universe for smoothing."""
    if RENDER_ENGINE is None:
        return jsonify({"error": "engine not running"}), 503

    payload = request.get_json() or {}
    universes = payload.get("universes") or {}
    if not isinstance(universes, dict):
        return jsonify({"error": "invalid universes"}), 400

    normalized: Dict[int, List[int]] = {}
    for u_str, ch_list in universes.items():
        try:
            u = int(u_str)
        except Exception:
            continue
        if not isinstance(ch_list, list):
            continue
        out = []
        for ch in ch_list:
            try:
                c = int(ch)
            except Exception:
                continue
            if 0 <= c < 512:
                out.append(c)
        if out:
            normalized[u] = out

    if hasattr(RENDER_ENGINE, "set_movement_channels"):
        RENDER_ENGINE.set_movement_channels(normalized)

    return jsonify({"ok": True, "universes": normalized})


@app.route("/api/dummy_channels", methods=["POST"])
def api_dummy_channels():
    """Set dummy channels used to force updates."""
    if RENDER_ENGINE is None:
        return jsonify({"error": "engine not running"}), 503

    payload = request.get_json() or {}
    universes = payload.get("universes") or {}
    if not isinstance(universes, dict):
        return jsonify({"error": "invalid universes"}), 400

    normalized: Dict[int, List[int]] = {}
    for u_str, ch_list in universes.items():
        try:
            u = int(u_str)
        except Exception:
            continue
        if not isinstance(ch_list, list):
            continue
        out = []
        for ch in ch_list:
            try:
                c = int(ch)
            except Exception:
                continue
            if 0 <= c < 512:
                out.append(c)
        if out:
            normalized[u] = out

    if hasattr(RENDER_ENGINE, "set_dummy_channels"):
        RENDER_ENGINE.set_dummy_channels(normalized)

    return jsonify({"ok": True, "universes": normalized})

# ---------- NEW API: STATS ----------

@app.route("/api/stats", methods=["GET"])
def api_stats():
    server_time = time.time()
    if RENDER_ENGINE is None or not hasattr(RENDER_ENGINE, "get_packet_stats"):
        return jsonify({
            "ok": True,
            "artnet_packets": 0,
            "last_send_ts": 0,
            "server_time": server_time
        })

    try:
        stats = RENDER_ENGINE.get_packet_stats() or {}
        return jsonify({
            "ok": True,
            "artnet_packets": int(stats.get("artnet_packets", 0) or 0),
            "last_send_ts": float(stats.get("last_send_ts", 0) or 0),
            "server_time": server_time
        })
    except Exception as e:
        app.logger.exception("[API] stats error")
        return jsonify({"error": str(e)}), 500

# ---------- NEW API: STATE STREAM (SSE) ----------

@app.route("/api/state/stream")
def api_state_stream():
    """Server-Sent Events stream for DMX state updates"""
    def generate():
        q = Queue()
        _sse_clients.append(q)
        try:
            while True:
                try:
                    state = q.get(timeout=30)
                    yield f"data: {json.dumps(state)}\n\n"
                except:
                    # Send keepalive
                    yield f": keepalive\n\n"
        finally:
            if q in _sse_clients:
                _sse_clients.remove(q)

    return Response(generate(), mimetype="text/event-stream")


def broadcast_state(state: Dict[str, Any]):
    """Broadcast state to all SSE clients"""
    for q in _sse_clients:
        try:
            q.put_nowait(state)
        except:
            pass


# ---------- LEGACY API (for backward compatibility) ----------

@app.route("/api/intelligent_effects", methods=["GET"])
def api_intelligent_effects():
    """List available intelligent effect files."""
    return jsonify({"files": list_intelligent_effect_files()})


@app.route("/api/intelligent_effects/import", methods=["POST"])
def api_intelligent_effects_import():
    """Import one or multiple intelligent effect files (.js/.json)."""
    if "files" not in request.files:
        return jsonify({"error": "no files"}), 400

    saved = []
    for f in request.files.getlist("files"):
        name = _safe_effect_filename(f.filename or "")
        if not name:
            continue
        path = os.path.join(INTELLIGENT_EFFECTS_DIR, name)
        try:
            f.save(path)
            saved.append(name)
        except Exception as e:
            app.logger.exception("[API] intelligent_effects/import failed: %s", e)

    return jsonify({"ok": True, "saved": saved})


@app.route("/api/intelligent_effects/<filename>", methods=["GET"])
def api_intelligent_effects_file(filename: str):
    """Download a single intelligent effect file (.js/.json)."""
    name = _safe_effect_filename(filename)
    if not name:
        return jsonify({"error": "invalid filename"}), 400
    mime = "application/json" if name.lower().endswith(".json") else "application/javascript"
    return send_from_directory(INTELLIGENT_EFFECTS_DIR, name, mimetype=mime)


@app.route("/api/effects", methods=["GET"])
def api_effects():
    """Liste des effets disponibles (frontend)."""
    if Effect is None:
        return jsonify({"effects": []})
    try:
        return jsonify({"effects": Effect.list_effects()})
    except Exception as e:
        app.logger.debug(f"[API] effects list error: {e}")
        return jsonify({"effects": []})


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(os.path.join(RESOURCE_DIR, "static"), filename)


# ---------- STARTUP ----------

def setup_engine_callbacks():
    """Setup SSE broadcasting from engine"""
    if RENDER_ENGINE is not None:
        RENDER_ENGINE.add_state_callback(broadcast_state)


def set_update_callbacks(status_fn=None, check_fn=None, install_fn=None):
    UPDATE_CALLBACKS["status"] = status_fn
    UPDATE_CALLBACKS["check"] = check_fn
    UPDATE_CALLBACKS["install"] = install_fn


if __name__ == "__main__":
    init_engine()
    setup_engine_callbacks()
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
