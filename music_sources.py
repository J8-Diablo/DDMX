#!/usr/bin/env python3
"""External music-analysis sources for AutoLight.

Goal: look up the currently playing track on public services and pull a
reference amplitude envelope (waveform). Combined with the media probe's
``position_ms`` it gives the scene engine a *lookahead* — "in 4 seconds
there's a 4× energy spike" — so AutoLight can prepare a build-up / drop
effect before the event instead of reacting after.

This module is intentionally conservative:

* **All sources are opt-in.** If no credentials are configured, nothing
  hits the network; the existing realtime heuristics still run.
* Network calls use ``urllib`` (stdlib) with tight timeouts.
* Results are cached in memory keyed by ``(title, artist, duration_ms)``.
* Failures are swallowed and logged at DEBUG; the service never errors.

Sources:
  * **SoundCloud** — downloads a track's public ``waveform.json`` (array of
    amplitude samples, 0–100). Requires a ``client_id`` (SoundCloud's web
    player exposes one; the user must paste it into settings, since
    SoundCloud's official API has been closed to new registrations for
    years).
  * **Songle** — stub. Their chorus API is keyed on a YouTube URL. Without
    a YouTube lookup we can't auto-resolve from title+artist, so this
    source is exposed as a stub the user can extend manually later.
  * **Local file** — stub. Would require madmom/librosa and the actual
    audio file path; neither is easy to obtain from WASAPI loopback.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


@dataclass
class TrackAnalysis:
    """What the scene engine consumes for lookahead-aware behavior."""
    source: str                                 # "soundcloud" | "songle" | "local" | "none"
    title: str = ""
    artist: str = ""
    duration_ms: Optional[int] = None
    bpm: Optional[float] = None
    # Amplitude envelope normalized to [0, 1] at fixed time resolution.
    waveform: List[float] = field(default_factory=list)
    waveform_ms_per_sample: float = 0.0         # how many ms each sample covers
    # Structural markers (seconds into the track). Empty when source doesn't supply.
    sections: List[Dict[str, Any]] = field(default_factory=list)
    fetched_at: float = 0.0
    permalink: str = ""

    def envelope_at(self, position_ms: float) -> Optional[float]:
        if not self.waveform or self.waveform_ms_per_sample <= 0:
            return None
        idx = int(position_ms / self.waveform_ms_per_sample)
        if 0 <= idx < len(self.waveform):
            return float(self.waveform[idx])
        return None

    def lookahead_peak(self, position_ms: float, horizon_ms: float = 4000.0) -> Optional[float]:
        """Max envelope value in the next ``horizon_ms``. Useful for anticipating drops."""
        if not self.waveform or self.waveform_ms_per_sample <= 0:
            return None
        start = int(position_ms / self.waveform_ms_per_sample)
        end = int((position_ms + horizon_ms) / self.waveform_ms_per_sample)
        start = max(0, start)
        end = max(start + 1, min(len(self.waveform), end + 1))
        if start >= len(self.waveform):
            return None
        window = self.waveform[start:end]
        return max(window) if window else None


def _http_get_json(url: str, timeout: float = 4.0, headers: Optional[Dict[str, str]] = None) -> Optional[Any]:
    req_headers = {"User-Agent": "DDMX-AutoLight/0.3"}
    if headers:
        req_headers.update(headers)
    try:
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as exc:
        log.debug("GET %s failed: %s", url, exc)
        return None


class SoundCloudClient:
    """Minimal SoundCloud client: search → track metadata → waveform samples.

    SoundCloud's public API has been closed to new registrations for years,
    but their web player exposes a rotating ``client_id``. Users can sniff
    one from Chrome DevTools (any request to ``api-v2.soundcloud.com``
    includes ``client_id=<hex>``) and paste it in AutoLight settings.
    """

    SEARCH_URL = "https://api-v2.soundcloud.com/search/tracks"

    def __init__(self) -> None:
        self._client_id: Optional[str] = None
        self._cache: Dict[Tuple[str, str, int], TrackAnalysis] = {}
        self._cache_lock = threading.Lock()

    def set_client_id(self, client_id: Optional[str]) -> None:
        self._client_id = (client_id or "").strip() or None

    def available(self) -> bool:
        return self._client_id is not None

    def lookup(self, title: str, artist: str, duration_ms: Optional[int]) -> Optional[TrackAnalysis]:
        if not self.available():
            return None
        key = ((title or "").strip().lower(), (artist or "").strip().lower(), int(duration_ms or 0))
        with self._cache_lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached

        query = " ".join(part for part in (title, artist) if part).strip()
        if not query:
            return None
        params = {"q": query, "limit": "5", "client_id": self._client_id}
        url = self.SEARCH_URL + "?" + urllib.parse.urlencode(params)
        data = _http_get_json(url)
        if not isinstance(data, dict):
            return None
        hits = data.get("collection") or []
        match = self._pick_best_match(hits, title, artist, duration_ms)
        if not match:
            return None
        waveform_url = str(match.get("waveform_url") or "").strip()
        track_duration = int(match.get("duration") or 0) or duration_ms
        samples: List[float] = []
        ms_per_sample = 0.0
        if waveform_url:
            wf = _http_get_json(waveform_url)
            if isinstance(wf, dict):
                raw_samples = wf.get("samples")
                if isinstance(raw_samples, list) and raw_samples:
                    peak = max(max(raw_samples), 1)
                    samples = [float(s) / float(peak) for s in raw_samples]
                    if track_duration and len(samples) > 0:
                        ms_per_sample = float(track_duration) / float(len(samples))
        analysis = TrackAnalysis(
            source="soundcloud",
            title=str(match.get("title") or title),
            artist=str((match.get("user") or {}).get("username") or artist),
            duration_ms=track_duration,
            bpm=None,
            waveform=samples,
            waveform_ms_per_sample=ms_per_sample,
            sections=[],
            fetched_at=time.time(),
            permalink=str(match.get("permalink_url") or ""),
        )
        with self._cache_lock:
            self._cache[key] = analysis
        log.info(
            "soundcloud: matched '%s - %s' → %s (%d samples, %.1fs)",
            artist, title, analysis.permalink, len(samples), (track_duration or 0) / 1000.0,
        )
        return analysis

    @staticmethod
    def _pick_best_match(
        hits: List[Dict[str, Any]], title: str, artist: str, duration_ms: Optional[int]
    ) -> Optional[Dict[str, Any]]:
        if not hits:
            return None
        title_norm = (title or "").strip().lower()
        artist_norm = (artist or "").strip().lower()
        expected_dur = int(duration_ms or 0)

        def score(h: Dict[str, Any]) -> float:
            t = str(h.get("title") or "").lower()
            user = (h.get("user") or {}).get("username") or ""
            a = str(user).lower()
            dur = int(h.get("duration") or 0)
            s = 0.0
            if title_norm and title_norm in t:
                s += 2.0
            if artist_norm and artist_norm in (t + " " + a):
                s += 2.0
            if expected_dur and dur:
                # closer duration wins
                diff = abs(dur - expected_dur) / max(dur, expected_dur)
                s += max(0.0, 1.5 - diff * 3.0)
            return s

        ranked = sorted(hits, key=score, reverse=True)
        best = ranked[0]
        if score(best) < 1.5:
            return None
        return best


# ============================================================================
# METADATA SOURCES (genre / BPM / musical key)
# ============================================================================
# These feed the BeatGrid (authoritative BPM reference) and the show layer
# (mood-driven colour). Every client takes an injectable ``fetch`` callable so
# the merge logic is unit-testable offline. Network sources are conservative:
# tight timeouts, failures swallowed, results cached.


@dataclass
class TrackMetadata:
    """Aggregated descriptive metadata for the playing track."""
    title: str = ""
    artist: str = ""
    duration_ms: Optional[int] = None
    bpm: Optional[float] = None
    bpm_source: str = ""          # which provider supplied the bpm
    genre: str = ""
    musical_key: str = ""         # e.g. "A minor" / "Am" when available
    sources: List[str] = field(default_factory=list)  # providers that contributed
    fetched_at: float = 0.0

    def merge(self, other: "TrackMetadata") -> None:
        """Fill empty fields from ``other`` (first non-empty wins)."""
        if (not self.bpm) and other.bpm:
            self.bpm = other.bpm
            self.bpm_source = other.bpm_source or self.bpm_source
        if (not self.genre) and other.genre:
            self.genre = other.genre
        if (not self.musical_key) and other.musical_key:
            self.musical_key = other.musical_key
        for s in other.sources:
            if s not in self.sources:
                self.sources.append(s)


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


def _dur_close(a: Optional[int], b: Optional[int], tol: float = 0.06) -> bool:
    if not a or not b:
        return True  # unknown duration → don't penalise
    return abs(a - b) / max(a, b) <= tol


class DeezerClient:
    """Deezer public JSON API — no key required. Best free source for BPM.

    Flow: search → pick best hit → /track/<id> (bpm) → /album/<id> (genre).
    """

    SEARCH = "https://api.deezer.com/search"
    TRACK = "https://api.deezer.com/track/"
    ALBUM = "https://api.deezer.com/album/"

    def __init__(self, fetch=None) -> None:
        self._fetch = fetch or (lambda url: _http_get_json(url))

    def available(self) -> bool:
        return True  # keyless

    def lookup(self, title: str, artist: str, duration_ms: Optional[int]) -> Optional[TrackMetadata]:
        if not title:
            return None
        q = f'track:"{title}"' + (f' artist:"{artist}"' if artist else "")
        url = self.SEARCH + "?" + urllib.parse.urlencode({"q": q, "limit": "8"})
        data = self._fetch(url)
        hits = data.get("data") if isinstance(data, dict) else None
        if not hits:
            return None
        best = self._pick(hits, title, artist, duration_ms)
        if not best:
            return None
        meta = TrackMetadata(
            title=str(best.get("title") or title),
            artist=str((best.get("artist") or {}).get("name") or artist),
            duration_ms=(int(best.get("duration")) * 1000 if best.get("duration") else duration_ms),
            sources=["deezer"],
            fetched_at=time.time(),
        )
        tid = best.get("id")
        if tid is not None:
            detail = self._fetch(self.TRACK + str(tid))
            if isinstance(detail, dict):
                bpm = detail.get("bpm")
                if bpm and float(bpm) > 0:
                    meta.bpm = float(bpm)
                    meta.bpm_source = "deezer"
                album = detail.get("album") or {}
                alb_id = album.get("id")
                if alb_id is not None:
                    alb = self._fetch(self.ALBUM + str(alb_id))
                    if isinstance(alb, dict):
                        genres = ((alb.get("genres") or {}).get("data") or [])
                        if genres:
                            meta.genre = str(genres[0].get("name") or "").strip()
        return meta

    @staticmethod
    def _pick(hits, title, artist, duration_ms):
        tnorm, anorm = _norm(title), _norm(artist)
        exp = int(duration_ms or 0)

        def score(h):
            t = _norm(h.get("title"))
            a = _norm((h.get("artist") or {}).get("name"))
            dur = int(h.get("duration") or 0) * 1000
            s = 0.0
            if tnorm and tnorm in t:
                s += 2.0
            if anorm and anorm in a:
                s += 2.0
            if exp and dur and _dur_close(exp, dur):
                s += 1.5
            return s

        best = max(hits, key=score)
        return best if score(best) >= 2.0 else None


class MusicBrainzClient:
    """MusicBrainz recording search — no key, just a descriptive User-Agent.

    Supplies genre/tags. Does not provide BPM. Rate-limited to ~1 req/s by MB;
    callers run it off the render thread so the limit is harmless.
    """

    SEARCH = "https://musicbrainz.org/ws/2/recording"

    def __init__(self, fetch=None) -> None:
        self._fetch = fetch or (
            lambda url: _http_get_json(
                url, timeout=5.0,
                headers={"User-Agent": "DDMX-AutoLight/0.4 (lighting controller)"},
            )
        )

    def available(self) -> bool:
        return True  # keyless

    def lookup(self, title: str, artist: str, duration_ms: Optional[int]) -> Optional[TrackMetadata]:
        if not title:
            return None
        query = f'recording:"{title}"' + (f' AND artist:"{artist}"' if artist else "")
        url = self.SEARCH + "?" + urllib.parse.urlencode({"query": query, "fmt": "json", "limit": "5"})
        data = self._fetch(url)
        recs = data.get("recordings") if isinstance(data, dict) else None
        if not recs:
            return None
        tnorm, anorm = _norm(title), _norm(artist)

        def score(r):
            s = float(r.get("score") or 0) / 100.0
            if tnorm and tnorm in _norm(r.get("title")):
                s += 1.0
            credits = " ".join(_norm((c.get("artist") or {}).get("name"))
                               for c in (r.get("artist-credit") or []))
            if anorm and anorm in credits:
                s += 1.0
            return s

        best = max(recs, key=score)
        if score(best) < 1.0:
            return None
        # Genre from tags (most-voted) or the 'genres' array when present.
        genre = ""
        tags = best.get("tags") or best.get("genres") or []
        if tags:
            tags = sorted(tags, key=lambda t: int(t.get("count") or 0), reverse=True)
            genre = str(tags[0].get("name") or "").strip()
        return TrackMetadata(
            title=str(best.get("title") or title),
            artist=artist,
            duration_ms=duration_ms,
            genre=genre,
            sources=["musicbrainz"],
            fetched_at=time.time(),
        )


class GetSongBpmClient:
    """getsongbpm.com — free but key-gated (and requires a backlink credit).

    Opt-in: the user pastes an API key in settings. Supplies BPM + key.
    """

    SEARCH = "https://api.getsongbpm.com/search/"

    def __init__(self, fetch=None) -> None:
        self._api_key: Optional[str] = None
        self._fetch = fetch or (lambda url: _http_get_json(url))

    def set_api_key(self, key: Optional[str]) -> None:
        self._api_key = (key or "").strip() or None

    def available(self) -> bool:
        return self._api_key is not None

    def lookup(self, title: str, artist: str, duration_ms: Optional[int]) -> Optional[TrackMetadata]:
        if not self.available() or not title:
            return None
        lookup = f"song:{title}" + (f" artist:{artist}" if artist else "")
        url = self.SEARCH + "?" + urllib.parse.urlencode(
            {"api_key": self._api_key, "type": "both", "lookup": lookup}
        )
        data = self._fetch(url)
        if not isinstance(data, dict):
            return None
        search = data.get("search")
        if isinstance(search, list):
            search = search[0] if search else None
        if not isinstance(search, dict):
            return None
        bpm = search.get("tempo")
        try:
            bpm = float(bpm) if bpm else None
        except Exception:
            bpm = None
        return TrackMetadata(
            title=str(search.get("title") or title),
            artist=str((search.get("artist") or {}).get("name") or artist),
            duration_ms=duration_ms,
            bpm=bpm,
            bpm_source="getsongbpm" if bpm else "",
            musical_key=str(search.get("key_of") or "").strip(),
            sources=["getsongbpm"],
            fetched_at=time.time(),
        )


class MetadataResolver:
    """Queries metadata sources in a cascade and merges the first hits.

    Order favours authoritative BPM first (GetSongBPM when configured, then
    Deezer), then genre (MusicBrainz, then Deezer). ``resolve`` stops early
    once BPM + genre are both known.
    """

    def __init__(self, deezer=None, musicbrainz=None, getsongbpm=None) -> None:
        self.deezer = deezer if deezer is not None else DeezerClient()
        self.musicbrainz = musicbrainz if musicbrainz is not None else MusicBrainzClient()
        self.getsongbpm = getsongbpm if getsongbpm is not None else GetSongBpmClient()
        self._cache: Dict[Tuple[str, str, int], Optional[TrackMetadata]] = {}
        self._cache_lock = threading.Lock()
        self.enabled = True

    def set_getsongbpm_key(self, key: Optional[str]) -> None:
        self.getsongbpm.set_api_key(key)

    def _ordered_sources(self):
        # GetSongBPM first (BPM+key) only when configured; Deezer (BPM+genre);
        # MusicBrainz (genre) last as a genre fallback.
        srcs = []
        if self.getsongbpm.available():
            srcs.append(self.getsongbpm)
        srcs.append(self.deezer)
        srcs.append(self.musicbrainz)
        return srcs

    def resolve(self, title: str, artist: str, duration_ms: Optional[int]) -> Optional[TrackMetadata]:
        if not self.enabled or not title:
            return None
        key = (_norm(title), _norm(artist), int(duration_ms or 0))
        with self._cache_lock:
            if key in self._cache:
                return self._cache[key]

        merged: Optional[TrackMetadata] = None
        for src in self._ordered_sources():
            try:
                part = src.lookup(title, artist, duration_ms)
            except Exception as exc:
                log.debug("metadata source %s failed: %s", type(src).__name__, exc)
                part = None
            if not part:
                continue
            if merged is None:
                merged = part
            else:
                merged.merge(part)
            if merged.bpm and merged.genre:
                break  # enough information

        with self._cache_lock:
            self._cache[key] = merged
        if merged:
            log.info("metadata: '%s - %s' → bpm=%s genre=%s key=%s via %s",
                     artist, title, merged.bpm, merged.genre, merged.musical_key,
                     ",".join(merged.sources))
        return merged


class MusicContext:
    """Per-AutoLightService orchestrator.

    Watches the media probe for track changes and asks every enabled source
    for analysis. First source returning data wins; result is cached by
    (title, artist, duration_ms). Sources are queried off the render thread
    so even a slow HTTP call never blocks DMX output.
    """

    def __init__(self) -> None:
        self.soundcloud = SoundCloudClient()
        self.metadata = MetadataResolver()
        self._current: Optional[TrackAnalysis] = None
        self._current_meta: Optional[TrackMetadata] = None
        self._current_key: Optional[Tuple[str, str, int]] = None
        self._pending_key: Optional[Tuple[str, str, int]] = None
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._last_error: Optional[str] = None

    def set_soundcloud_client_id(self, client_id: Optional[str]) -> None:
        self.soundcloud.set_client_id(client_id)

    def set_getsongbpm_key(self, key: Optional[str]) -> None:
        self.metadata.set_getsongbpm_key(key)

    def set_metadata_enabled(self, enabled: bool) -> None:
        self.metadata.enabled = bool(enabled)

    def metadata_for_current(self) -> Optional[TrackMetadata]:
        with self._lock:
            return self._current_meta

    def current(self) -> Optional[TrackAnalysis]:
        with self._lock:
            return self._current

    def observe_track(self, title: Optional[str], artist: Optional[str], duration_ms: Optional[int]) -> None:
        """Called periodically with the latest media-probe track info.

        On change, kick off a background lookup. Subsequent calls for the
        same track are cheap no-ops.
        """
        title = (title or "").strip()
        artist = (artist or "").strip()
        if not title:
            return
        key = (title.lower(), artist.lower(), int(duration_ms or 0))
        with self._lock:
            if key == self._current_key:
                return
            if key == self._pending_key:
                return
            if self._worker is not None and self._worker.is_alive():
                return
            self._pending_key = key
            self._current_key = key
            self._current = None
            self._current_meta = None

        def _work():
            try:
                result: Optional[TrackAnalysis] = None
                if self.soundcloud.available():
                    result = self.soundcloud.lookup(title, artist, duration_ms)
                meta: Optional[TrackMetadata] = None
                try:
                    meta = self.metadata.resolve(title, artist, duration_ms)
                except Exception as exc:
                    log.debug("metadata resolve failed: %s", exc)
                with self._lock:
                    self._current = result
                    self._current_meta = meta
                    self._pending_key = None
                    self._last_error = None if (result is not None or meta is not None) else "no match"
            except Exception as exc:
                log.debug("music context worker failed: %s", exc)
                with self._lock:
                    self._pending_key = None
                    self._last_error = f"{type(exc).__name__}: {exc}"

        self._worker = threading.Thread(target=_work, name="music-context", daemon=True)
        self._worker.start()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            cur = self._current
            meta = self._current_meta
            pending = self._pending_key
            err = self._last_error
        return {
            "has_analysis": cur is not None,
            "source": cur.source if cur else "none",
            "title": cur.title if cur else (meta.title if meta else ""),
            "artist": cur.artist if cur else (meta.artist if meta else ""),
            "duration_ms": cur.duration_ms if cur else (meta.duration_ms if meta else None),
            "sample_count": len(cur.waveform) if cur else 0,
            "permalink": cur.permalink if cur else "",
            "pending": pending,
            "last_error": err,
            "soundcloud_configured": self.soundcloud.available(),
            # Metadata layer (genre/BPM/key) — feeds BeatGrid + colour.
            "has_metadata": meta is not None,
            "bpm": meta.bpm if meta else None,
            "bpm_source": meta.bpm_source if meta else "",
            "genre": meta.genre if meta else "",
            "musical_key": meta.musical_key if meta else "",
            "metadata_sources": meta.sources if meta else [],
            "metadata_enabled": self.metadata.enabled,
            "getsongbpm_configured": self.metadata.getsongbpm.available(),
        }
