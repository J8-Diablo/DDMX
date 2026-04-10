#!/usr/bin/env python3
# app.py - DMX/ArtNet controller backend (Flask) + New Render Engine

import os
import json
import time
import logging
import logging.handlers
import re
import socket
import xml.etree.ElementTree as ET
from typing import Dict, Any, List, Optional
from queue import Queue

from flask import Flask, render_template, jsonify, request, send_from_directory, Response
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURES_DIR = os.path.join(BASE_DIR, "fixtures")
CUE_DIR = os.path.join(BASE_DIR, "cue")
CONFIG_DIR = os.path.join(BASE_DIR, "config")
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")
INTELLIGENT_EFFECTS_DIR = os.path.join(BASE_DIR, "intelligent_effects")

os.makedirs(FIXTURES_DIR, exist_ok=True)
os.makedirs(CUE_DIR, exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(INTELLIGENT_EFFECTS_DIR, exist_ok=True)

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
        "dmx_runtime": {
            "render_mode": "ui",
            "playback_clock_mode": os.environ.get("DMX_PLAYBACK_CLOCK_MODE", "timeline").strip().lower(),
            "playback_engine_hz": _env_float("DMX_PLAYBACK_ENGINE_HZ", 120),
            "playback_ui_fps": _env_float("DMX_PLAYBACK_UI_FPS", 12),
            "max_send_hz": _env_float("DMX_MAX_SEND_HZ", 40),
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

    mode_raw = str(payload.get("render_mode") or out.get("render_mode") or "ui").strip().lower()
    out["render_mode"] = "backend" if mode_raw == "backend" else "ui"
    clock_mode_raw = str(payload.get("playback_clock_mode") or out.get("playback_clock_mode") or "timeline").strip().lower()
    out["playback_clock_mode"] = "absolute_clock" if clock_mode_raw == "absolute_clock" else "timeline"
    out["playback_engine_hz"] = _clamp_float(payload.get("playback_engine_hz"), 40.0, 240.0, out.get("playback_engine_hz", 120.0))
    out["playback_ui_fps"] = _clamp_float(payload.get("playback_ui_fps"), 1.0, 30.0, out.get("playback_ui_fps", 12.0))

    out["max_send_hz"] = _clamp_float(payload.get("max_send_hz"), 1.0, 120.0, out.get("max_send_hz", 40))
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
        if hasattr(engine, "set_render_mode"):
            engine.set_render_mode(runtime.get("render_mode", "ui"))
        elif hasattr(engine, "_render_mode"):
            engine._render_mode = str(runtime.get("render_mode", "ui")).strip().lower()
        if hasattr(engine, "set_playback_clock_mode"):
            engine.set_playback_clock_mode(runtime.get("playback_clock_mode", "timeline"))
        elif hasattr(engine, "_playback_clock_mode"):
            engine._playback_clock_mode = str(runtime.get("playback_clock_mode", "timeline")).strip().lower()
        if hasattr(engine, "set_playback_engine_hz"):
            engine.set_playback_engine_hz(runtime.get("playback_engine_hz", 120.0))
        elif hasattr(engine, "_playback_engine_hz"):
            engine._playback_engine_hz = float(runtime.get("playback_engine_hz", 120.0))
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
if not os.path.exists(SETTINGS_PATH):
    save_settings(SETTINGS)

# ---------- LOGGING ----------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

app = Flask(__name__, static_folder="static", template_folder="templates")
app.logger.setLevel(logging.DEBUG)
app.config["APP_NAME"] = APP_NAME
app.config["APP_VERSION"] = APP_VERSION
app.config["APP_LICENSE_CODE"] = APP_LICENSE_CODE

# ---------- DEBUG FLAGS ----------
LOG_UI_PAYLOADS = os.environ.get("DMX_LOG_UI", "0").strip().lower() in ("1", "true", "yes", "on")
LOG_UI_FULL = os.environ.get("DMX_LOG_UI_FULL", "0").strip().lower() in ("1", "true", "yes", "on")

# ---------- FILE LOGGING ----------
LOG_DIR = os.path.join(BASE_DIR, "logs")
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
def parse_fixture_xml(path: str) -> Dict[str, Any]:
    tree = ET.parse(path)
    root = tree.getroot()

    addr_count = int(root.attrib.get("dmxaddresscount", "0") or 0)

    info = {}
    info_node = root.find("information")
    if info_node is not None:
        info["model"] = info_node.findtext("model") or ""
        info["vendor"] = info_node.findtext("vendor") or ""
        info["mode"] = info_node.findtext("mode") or ""

    functions: Dict[str, Any] = {}
    funcs = root.find("functions")
    if funcs is not None:
        dim = funcs.find("dimmer")
        if dim is not None and "dmxchannel" in dim.attrib:
            functions["dimmer"] = {"channel": int(dim.attrib["dmxchannel"])}

        rgb = funcs.find("rgb")
        if rgb is not None:
            rgb_map = {}
            for comp in ("red", "green", "blue"):
                node = rgb.find(comp)
                if node is not None and "dmxchannel" in node.attrib:
                    rgb_map[comp] = int(node.attrib["dmxchannel"])
            if rgb_map:
                functions["rgb"] = rgb_map

        focus = funcs.find("focus")
        if focus is not None and "dmxchannel" in focus.attrib:
            functions["focus"] = {"channel": int(focus.attrib["dmxchannel"])}

        pos = funcs.find("position")
        if pos is not None:
            posmap: Dict[str, Any] = {}
            pan = pos.find("pan")
            tilt = pos.find("tilt")
            if pan is not None and "dmxchannel" in pan.attrib:
                rng_node = pan.find("range")
                rng = int(rng_node.attrib.get("range")) if rng_node is not None and "range" in rng_node.attrib else None
                posmap["pan"] = {"channel": int(pan.attrib["dmxchannel"]), "range": rng}
            if tilt is not None and "dmxchannel" in tilt.attrib:
                rng_node = tilt.find("range")
                rng = int(rng_node.attrib.get("range")) if rng_node is not None and "range" in rng_node.attrib else None
                posmap["tilt"] = {"channel": int(tilt.attrib["dmxchannel"]), "range": rng}
            if posmap:
                functions["position"] = posmap

        for child in funcs:
            tag = child.tag
            if tag in ("dimmer", "rgb", "focus", "position"):
                continue
            if "dmxchannel" in child.attrib:
                functions.setdefault("extra", {})[tag] = {"channel": int(child.attrib["dmxchannel"])}

    return {
        "info": info,
        "addr_count": addr_count,
        "functions": functions,
        "source_file": os.path.basename(path),
    }


def load_all_fixtures() -> Dict[str, Any]:
    fixtures: Dict[str, Any] = {}
    for fname in sorted(os.listdir(FIXTURES_DIR)):
        if not fname.lower().endswith(".xml"):
            continue
        path = os.path.join(FIXTURES_DIR, fname)
        try:
            fixtures[fname] = parse_fixture_xml(path)
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


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    sync = SETTINGS.get("sync_video") or {}
    ctc = SETTINGS.get("ctc") or {}
    whats_new = SETTINGS.get("whats_new") or {}
    auto_update = SETTINGS.get("auto_update") or {}
    ui = SETTINGS.get("ui") or {}
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
        "dmx_runtime": SETTINGS.get("dmx_runtime") or {}
    })


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
    if not isinstance(devices, list):
        return jsonify({"error": "invalid devices"}), 400
    try:
        if hasattr(RENDER_ENGINE, "register_rig_devices"):
            RENDER_ENGINE.register_rig_devices(devices)
        return jsonify({"ok": True, "count": len(devices)})
    except Exception as e:
        app.logger.exception("[API] rig/register error")
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


@app.route("/api/live/effect/start", methods=["POST"])
def api_live_effect_start():
    """Start a live effect on a channel"""
    if RENDER_ENGINE is None:
        return jsonify({"error": "engine not running"}), 503

    payload = request.get_json()
    if not payload:
        return jsonify({"error": "no json"}), 400

    try:
        device_id = payload.get("device_id", "live")
        channel = int(payload.get("channel", 0))
        effect_type = payload.get("type", "Sinus")
        amplitude = float(payload.get("amplitude", 100))
        frequency = float(payload.get("frequency", 1))
        phase = payload.get("phase", 0)
        params = {k: v for k, v in payload.items()
                  if k not in ("device_id", "channel", "type", "amplitude", "frequency", "phase")}

        RENDER_ENGINE.start_live_effect(device_id, channel, effect_type, amplitude, frequency, phase, params)
        return jsonify({"ok": True})
    except Exception as e:
        app.logger.exception("[API] live/effect/start error")
        return jsonify({"error": str(e)}), 500


@app.route("/api/live/effect/stop", methods=["POST"])
def api_live_effect_stop():
    """Stop live effects on a device"""
    if RENDER_ENGINE is None:
        return jsonify({"error": "engine not running"}), 503

    payload = request.get_json() or {}
    device_id = payload.get("device_id", "live")
    channel = payload.get("channel")  # None = all channels

    RENDER_ENGINE.stop_live_effects(device_id, channel)
    return jsonify({"ok": True})


# ---------- NEW API: EFFECT GROUPS ----------

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
        sequence = payload.get("sequence") or []
        start_index = int(payload.get("start_index", 0) or 0)
        speed = payload.get("speed", 1.0)
        virtual_groups = payload.get("virtual_groups") or payload.get("virtualGroups") or {}
        if not isinstance(sequence, list):
            return jsonify({"error": "invalid sequence"}), 400
        if not isinstance(virtual_groups, dict):
            virtual_groups = {}
        RENDER_ENGINE.run_sequence(sequence, start_index=start_index, virtual_groups=virtual_groups, speed=speed)
        return jsonify({"ok": True, "count": len(sequence), "start_index": start_index, "speed": speed})
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
    if not action:
        return jsonify({"error": "missing action"}), 400

    try:
        if hasattr(RENDER_ENGINE, "playback_control"):
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

@app.route("/api/apply_state", methods=["POST"])
def api_apply_state():
    """Legacy API - redirect to new live/channels"""
    if RENDER_ENGINE is None:
        return jsonify({"error": "engine not running"}), 503

    payload = request.get_json()
    if not payload:
        return jsonify({"error": "no json"}), 400

    try:
        universe = int(payload.get("universe", 0))
        channels = safe_parse_channels_map(payload.get("channels") or {})

        for ch, val in channels.items():
            RENDER_ENGINE.set_channel("legacy", universe, ch, val)

        return jsonify({"ok": True})
    except Exception as e:
        app.logger.exception("[API] apply_state error")
        return jsonify({"error": str(e)}), 500


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


@app.route("/api/intelligent_effects/definitions", methods=["GET"])
def api_intelligent_effects_definitions():
    if IntelligentFX is None:
        return jsonify({"effects": []})
    try:
        return jsonify({"effects": IntelligentFX.list_effects()})
    except Exception as e:
        app.logger.debug(f"[API] intelligent effects list error: {e}")
        return jsonify({"effects": []})


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
    return send_from_directory(os.path.join(BASE_DIR, "static"), filename)


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
