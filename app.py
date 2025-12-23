#!/usr/bin/env python3
# app.py - DMX/ArtNet controller backend (Flask) + New Render Engine

import os
import json
import time
import logging
import xml.etree.ElementTree as ET
from typing import Dict, Any, List, Optional
from queue import Queue

from flask import Flask, render_template, jsonify, request, send_from_directory, Response

# New render engine
try:
    from dmx_engine import DMXRenderEngine
except ImportError:
    DMXRenderEngine = None

# Effects (for API listing)
try:
    import Effect
except ImportError:
    Effect = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURES_DIR = os.path.join(BASE_DIR, "fixtures")
CUE_DIR = os.path.join(BASE_DIR, "cue")

os.makedirs(FIXTURES_DIR, exist_ok=True)
os.makedirs(CUE_DIR, exist_ok=True)

# ---------- LOGGING ----------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

app = Flask(__name__, static_folder="static", template_folder="templates")
app.logger.setLevel(logging.DEBUG)

# ---------- DMX RENDER ENGINE ----------
RENDER_ENGINE: Optional[DMXRenderEngine] = None

def init_engine():
    global RENDER_ENGINE
    if DMXRenderEngine is not None:
        try:
            RENDER_ENGINE = DMXRenderEngine(artnet_ip="127.0.0.1", bind_ip="0.0.0.0")
            RENDER_ENGINE.start()
            app.logger.info("DMX Render Engine started.")
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
    return render_template("index.html")


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


# ---------- NEW API: PLAYBACK ----------

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


if __name__ == "__main__":
    init_engine()
    setup_engine_callbacks()
    app.run(host="0.0.0.0", port=5000, debug=True)
