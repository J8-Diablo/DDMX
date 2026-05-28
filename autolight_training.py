#!/usr/bin/env python3
"""Training UI backend for AutoLight.

Two responsibilities:

1. **Library management** — the user gives us a folder (or a list of files).
   We scan for audio files, read their tags (title/artist) via ``mutagen``
   when available, and remember the (title, artist) → path mapping. When
   the live media probe later detects a track whose track_id matches an
   entry in our library, we know the user is listening to a "training"
   session and can react accordingly (in the UI, and by writing more
   detailed satisfaction signal into ``TrackMemory``).

2. **Real-time satisfaction signal** — the training modal exposes a slider
   from -1 (rig is wrong) to +1 (rig is on point). The browser streams
   slider values at ~10 Hz; we forward them to the active ``MusicDirector``
   which appends them to the current ``TrackMemory.satisfaction_log``.

The training itself is currently *passive* — we record signal, we don't
yet feed it back into the director's decisions. That adaptation step is a
follow-up; the data we capture here is what will drive it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from runtime_paths import DATA_DIR


log = logging.getLogger(__name__)


_AUDIO_EXTENSIONS: Tuple[str, ...] = (
    ".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".wma",
)


def _library_path() -> str:
    return os.path.join(DATA_DIR, "autolight_training_library.json")


def _camera_positions_path() -> str:
    return os.path.join(DATA_DIR, "autolight_camera_positions.json")


# =============================================================================
# Tag reading
# =============================================================================


def _read_tags(path: str) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """Return (title, artist, duration_ms) for a file, best-effort.

    Falls back to filename parsing when mutagen is unavailable or the file
    has no usable tags. We don't fail the scan over a single broken file —
    we just return None for whatever we couldn't read.
    """
    title: Optional[str] = None
    artist: Optional[str] = None
    duration_ms: Optional[int] = None

    try:
        from mutagen import File as MutagenFile  # type: ignore
        mf = MutagenFile(path, easy=True)
        if mf is not None:
            tags = getattr(mf, "tags", None) or {}
            t = tags.get("title")
            a = tags.get("artist")
            if isinstance(t, list) and t:
                title = str(t[0]).strip() or None
            elif isinstance(t, str):
                title = t.strip() or None
            if isinstance(a, list) and a:
                artist = str(a[0]).strip() or None
            elif isinstance(a, str):
                artist = a.strip() or None
            info = getattr(mf, "info", None)
            length = getattr(info, "length", None)
            if length:
                duration_ms = int(float(length) * 1000.0)
    except Exception as exc:
        log.debug("tag read failed for %s: %s", path, exc)

    if not title:
        # Fallback: parse "Artist - Title.ext" filename pattern.
        stem = os.path.splitext(os.path.basename(path))[0]
        if " - " in stem:
            a_part, _, t_part = stem.partition(" - ")
            artist = artist or a_part.strip() or None
            title = t_part.strip() or stem
        else:
            title = stem

    return title, artist, duration_ms


def _track_id_for(title: Optional[str], artist: Optional[str]) -> str:
    """Match the format used by ``MusicDirector._make_track_id``.

    Re-implemented here rather than imported to keep this module
    independent of the director module load order.
    """
    t = (title or "").strip().lower()
    a = (artist or "").strip().lower()
    return f"{a}::{t}" if a else t


# =============================================================================
# Library entry
# =============================================================================


@dataclass
class LibraryEntry:
    path: str
    title: str = ""
    artist: str = ""
    duration_ms: int = 0
    track_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "title": self.title,
            "artist": self.artist,
            "duration_ms": self.duration_ms,
            "track_id": self.track_id,
        }


# =============================================================================
# TrainingService
# =============================================================================


class TrainingService:
    """Owns the training library + the live satisfaction signal pipeline.

    This service is created once by ``AutoLightService`` and queried by both
    the HTTP layer (``app.py``) and the ``MusicDirector`` (which it pokes
    each time the slider value changes).

    Thread-safety: the library list is mutated from the request thread
    (HTTP handlers) and read from both the HTTP thread (status) and the
    render thread (when checking ``is_track_in_library``). All mutations
    take ``self._lock``.
    """

    def __init__(self, director_provider, identify_callback=None) -> None:
        # ``director_provider`` is a zero-arg callable returning the live
        # MusicDirector. Indirection lets us be created before the renderer
        # is fully wired without holding a stale reference.
        # ``identify_callback`` is a (device_id, duration_s) → bool callable
        # used by the camera-calibration phase to flash a single fixture so
        # the browser can capture its position. Decoupled from
        # AutoLightService so this module stays cycle-free.
        self._director_provider = director_provider
        self._identify_callback = identify_callback
        self._lock = threading.RLock()
        self._library: Dict[str, LibraryEntry] = {}  # track_id → entry
        self._enabled: bool = False
        self._last_satisfaction_value: float = 0.0
        self._last_satisfaction_ts: float = 0.0
        self._last_satisfaction_track_id: Optional[str] = None
        self._satisfaction_sample_count: int = 0  # this-session counter
        # Per-fixture pixel position discovered during the camera
        # calibration phase. Stored normalised to [0, 1] of the video
        # frame so resolution changes don't invalidate the data.
        # Format: {device_id: {"x": float, "y": float, "captured_at_ms": int}}
        self._camera_positions: Dict[str, Dict[str, Any]] = {}
        self._load_library()
        self._load_camera_positions()

    # --------------------------------------------------------------
    # Library persistence
    # --------------------------------------------------------------

    def _load_library(self) -> None:
        path = _library_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            log.warning("training library load failed: %s", exc)
            return
        for entry in (data or []):
            if not isinstance(entry, dict):
                continue
            try:
                e = LibraryEntry(
                    path=str(entry.get("path") or ""),
                    title=str(entry.get("title") or ""),
                    artist=str(entry.get("artist") or ""),
                    duration_ms=int(entry.get("duration_ms") or 0),
                    track_id=str(entry.get("track_id") or ""),
                )
                if e.path and e.track_id:
                    self._library[e.track_id] = e
            except Exception:
                continue

    def _save_library(self) -> None:
        path = _library_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            data = [e.to_dict() for e in self._library.values()]
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, path)
        except Exception as exc:
            log.warning("training library save failed: %s", exc)

    # --------------------------------------------------------------
    # Library management API
    # --------------------------------------------------------------

    def scan_path(self, path: str, recursive: bool = True) -> List[LibraryEntry]:
        """Scan ``path`` (file or directory) for audio files. Returns the
        new ``LibraryEntry`` list discovered. Does NOT mutate the library —
        caller decides whether to commit via ``add_entries``."""
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.exists(path):
            raise FileNotFoundError(path)

        files: List[str] = []
        if os.path.isfile(path):
            files = [path]
        else:
            walker = os.walk(path) if recursive else [(path, [], os.listdir(path))]
            for root, _dirs, names in walker:
                for name in names:
                    if name.lower().endswith(_AUDIO_EXTENSIONS):
                        files.append(os.path.join(root, name))
                if not recursive:
                    break

        out: List[LibraryEntry] = []
        for fp in files:
            try:
                title, artist, duration_ms = _read_tags(fp)
            except Exception as exc:
                log.debug("scan: skipped %s (%s)", fp, exc)
                continue
            tid = _track_id_for(title, artist)
            if not tid:
                continue
            out.append(LibraryEntry(
                path=fp,
                title=title or os.path.basename(fp),
                artist=artist or "",
                duration_ms=duration_ms or 0,
                track_id=tid,
            ))
        return out

    def add_entries(self, entries: List[LibraryEntry]) -> int:
        with self._lock:
            added = 0
            for entry in entries:
                if entry.track_id and entry.track_id not in self._library:
                    self._library[entry.track_id] = entry
                    added += 1
                elif entry.track_id:
                    # Update path/duration in case the file moved.
                    self._library[entry.track_id] = entry
            self._save_library()
            return added

    def remove_track(self, track_id: str) -> bool:
        with self._lock:
            existed = self._library.pop(track_id, None) is not None
            if existed:
                self._save_library()
            return existed

    def clear_library(self) -> None:
        with self._lock:
            self._library.clear()
            self._save_library()

    def list_library(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [e.to_dict() for e in self._library.values()]

    def is_track_in_library(self, track_id: Optional[str]) -> bool:
        if not track_id:
            return False
        with self._lock:
            return track_id in self._library

    def lookup_path(self, track_id: str) -> Optional[str]:
        with self._lock:
            entry = self._library.get(track_id)
            return entry.path if entry else None

    # --------------------------------------------------------------
    # Training mode + satisfaction
    # --------------------------------------------------------------

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def is_enabled(self) -> bool:
        return self._enabled

    def record_satisfaction(self, value: float) -> Dict[str, Any]:
        """Forward a slider value to (a) the global move scheduler and
        (b) the current track's per-track satisfaction log.

        The move scheduler always benefits — its scores adapt globally
        regardless of whether memory_persistence is on or whether a track
        is currently identified. The track log only gets written when both
        of those conditions hold.

        Returns a small status dict the HTTP layer can echo back so the UI
        can render "samples received" without a separate poll.
        """
        try:
            v = float(value)
        except (TypeError, ValueError):
            v = 0.0
        v = max(-1.0, min(1.0, v))

        now = time.monotonic()
        self._last_satisfaction_value = v
        self._last_satisfaction_ts = now

        if not self._enabled:
            return {
                "ok": False,
                "reason": "training_disabled",
                "value": v,
                "samples_this_session": self._satisfaction_sample_count,
            }

        director = self._director_provider() if self._director_provider else None
        if director is None:
            return {
                "ok": False, "reason": "no_director", "value": v,
                "samples_this_session": self._satisfaction_sample_count,
            }

        # ``record_satisfaction`` always feeds the move scheduler; the
        # returned track_id is None when there's no per-track logging
        # (memory off or no track recognised).
        track_id = director.record_satisfaction(v, now)

        if track_id is None:
            return {
                "ok": True,
                "reason": "move_only",
                "track_id": None,
                "value": v,
                "samples_this_session": self._satisfaction_sample_count,
                "in_library": False,
            }

        # Reset counter when the underlying track changes — easier for the
        # UI to display "samples for THIS track" instead of session-total.
        if track_id != self._last_satisfaction_track_id:
            self._satisfaction_sample_count = 0
            self._last_satisfaction_track_id = track_id
        self._satisfaction_sample_count += 1
        return {
            "ok": True,
            "track_id": track_id,
            "value": v,
            "samples_this_session": self._satisfaction_sample_count,
            "in_library": self.is_track_in_library(track_id),
        }

    # --------------------------------------------------------------
    # Status / introspection
    # --------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        director = self._director_provider() if self._director_provider else None
        current_track_id = getattr(director, "_current_track_id", None) if director else None
        current_track = getattr(director, "_current_track", None) if director else None
        memory_enabled = bool(getattr(director, "_memory_enabled", False)) if director else False
        # Pull compositions data into the training status so the modal can
        # render move-active state + score table from a single endpoint.
        compositions: Dict[str, Any] = {}
        if director is not None and hasattr(director, "_move_scheduler"):
            try:
                compositions = director._move_scheduler.diagnostics()
            except Exception:
                compositions = {}
        with self._lock:
            library_size = len(self._library)
            current_in_library = current_track_id in self._library if current_track_id else False
        return {
            "enabled": self._enabled,
            "memory_required_ok": memory_enabled,
            "library_size": library_size,
            "current_track_id": current_track_id,
            "current_track_in_library": current_in_library,
            "current_track_listen_count": int(getattr(current_track, "listen_count", 0) or 0) if current_track else 0,
            "current_track_satisfaction_samples": (
                len(getattr(current_track, "satisfaction_log", [])) if current_track else 0
            ),
            "last_value": round(self._last_satisfaction_value, 3),
            "last_value_age_s": round(time.monotonic() - self._last_satisfaction_ts, 3) if self._last_satisfaction_ts > 0 else None,
            "compositions": compositions,
        }

    def list_moves(self) -> List[Dict[str, Any]]:
        """Static metadata (name, description, eligible intents, …) for the
        scoreboard in the UI. Defers to the compositions module."""
        try:
            from autolight_compositions import all_moves_meta
            return all_moves_meta()
        except Exception:
            return []

    # --------------------------------------------------------------
    # Camera calibration — phase d'identification
    # --------------------------------------------------------------
    #
    # Workflow: the browser owns the webcam stream + frame analysis (no
    # frames hit the server, just the resulting normalised x/y). The
    # server's job is just to (a) flash a chosen fixture on demand so
    # the browser knows what it's looking at, and (b) persist the map
    # the browser builds, so positions survive across sessions.

    def _load_camera_positions(self) -> None:
        path = _camera_positions_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for k, v in (data or {}).items():
                if isinstance(v, dict) and "x" in v and "y" in v:
                    self._camera_positions[str(k)] = {
                        "x": float(v["x"]),
                        "y": float(v["y"]),
                        "captured_at_ms": int(v.get("captured_at_ms", 0) or 0),
                    }
        except Exception as exc:
            log.warning("camera positions load failed: %s", exc)

    def _save_camera_positions(self) -> None:
        path = _camera_positions_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._camera_positions, fh, indent=2)
            os.replace(tmp, path)
        except Exception as exc:
            log.warning("camera positions save failed: %s", exc)

    def set_camera_position(self, device_id: str, x: float, y: float) -> bool:
        """Browser found the bright cluster for ``device_id`` at (x, y),
        normalised to ``[0, 1]`` of the video frame. Persisted immediately."""
        try:
            x = max(0.0, min(1.0, float(x)))
            y = max(0.0, min(1.0, float(y)))
        except (TypeError, ValueError):
            return False
        with self._lock:
            self._camera_positions[str(device_id)] = {
                "x": x,
                "y": y,
                "captured_at_ms": int(time.time() * 1000),
            }
            self._save_camera_positions()
        return True

    def clear_camera_positions(self) -> None:
        with self._lock:
            self._camera_positions.clear()
            self._save_camera_positions()

    def get_camera_positions(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._camera_positions.items()}

    def identify_fixture(self, device_id: str, duration_s: float = 1.5) -> bool:
        """Flash a fixture so the browser can capture its position. Wraps
        ``AutoLightService.identify_device`` (passed in as a callback to
        avoid an import cycle)."""
        if self._identify_callback is None:
            return False
        try:
            return bool(self._identify_callback(device_id, duration_s))
        except Exception as exc:
            log.warning("identify_fixture failed: %s", exc)
            return False

    def open_in_os_player(self, path: str) -> bool:
        """Open ``path`` in the default OS player. Used by the modal's "Play"
        button so the user can audition a library item without leaving the
        app. Returns True on success."""
        try:
            path = os.path.abspath(path)
            if not os.path.exists(path):
                return False
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
                return True
            # POSIX fallbacks — not strictly needed for this Windows-only app
            # but cheap to include.
            import subprocess
            opener = "open" if os.uname().sysname == "Darwin" else "xdg-open"
            subprocess.Popen([opener, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception as exc:
            log.warning("open in OS player failed: %s", exc)
            return False
