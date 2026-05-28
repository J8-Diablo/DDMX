#!/usr/bin/env python3
"""Real-time system-audio feature extraction for AutoLight.

Captures WASAPI loopback (what the speakers are playing), computes bass / mid
/ treble RMS envelopes and a bass-onset beat flag. The worker thread publishes
the latest features under a lock so the DMX render loop and HTTP endpoints
can sample them without blocking on audio I/O.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

_TARGET_SAMPLE_RATE = 48000
_FRAME_SAMPLES = 1024            # ~21 ms at 48 kHz
_FFT_SIZE = _FRAME_SAMPLES

# Energy smoothing (EMA) — attack faster than release so meters feel responsive
# but don't flicker.
_ATTACK = 0.55
_RELEASE = 0.12

# Song-structure detector parameters. At ~47 Hz capture, we keep 10 s = 470
# frames of history.
_HISTORY_FRAMES = 470
_SHORT_FRAMES = 10               # ~200 ms
_LONG_FRAMES = 235               # ~5 s
_BUILD_FRAMES = 140              # ~3 s
_LEVEL_HYSTERESIS_MS = 500.0

# Real-time visual spectrum: log-spaced bins for the UI spectrum analyzer.
_SPECTRUM_BANDS = 32

_INTENSITY_LABELS = {0: "silent", 1: "verse", 2: "chorus", 3: "high", 4: "drop"}


# Default tunings. All of these can be overridden at runtime via
# AudioAnalyzer.set_tuning() (called from AutoLightService when settings
# change). Values are conservative defaults that match what we shipped
# before this patch — the knobs just make them editable.
DEFAULT_AUDIO_TUNING: Dict[str, float] = {
    # Frequency bands (Hz)
    "bass_band_lo": 30.0, "bass_band_hi": 250.0,
    "mid_band_lo": 250.0, "mid_band_hi": 2000.0,
    "treble_band_lo": 2000.0, "treble_band_hi": 8000.0,
    # Silence gate
    "active_rms_floor": 0.015,
    "long_rms_floor": 0.012,
    # Beat (kick) detector
    "beat_min_bass": 0.004,
    "beat_spike_ratio": 1.45,
    "beat_refractory_ms": 220.0,
    "bass_baseline_tau_s": 0.6,
    # Snare detector — spectral flux in mid band
    "snare_flux_min": 0.012,
    "snare_spike_ratio": 1.7,
    "snare_refractory_ms": 120.0,
    "mid_flux_baseline_tau_s": 0.35,
    # Hat detector — spectral flux in high band
    "hat_flux_min": 0.006,
    "hat_spike_ratio": 1.9,
    "hat_refractory_ms": 60.0,
    "high_flux_baseline_tau_s": 0.20,
    # High-frequency band for hat onsets (Hz)
    "high_band_lo": 4000.0,
    "high_band_hi": 12000.0,
    # Level classifier thresholds (on long_rms)
    "level_chorus_floor": 0.025,
    "level_high_floor": 0.055,
    "drop_score_min": 1.8,
    "drop_rms_min": 0.020,
    # BPM estimator
    "bpm_window_beats": 8.0,      # 3..30 intervals used in median
    "bpm_min": 50.0,
    "bpm_max": 240.0,
    "bpm_autocorr_window_s": 6.0,  # 0 disables autocorrelation, falls back to median
}


def _clamp_tuning(payload: Any) -> Dict[str, float]:
    """Merge payload over defaults, coerce to floats in safe ranges."""
    out: Dict[str, float] = dict(DEFAULT_AUDIO_TUNING)
    if not isinstance(payload, dict):
        return out
    ranges = {
        "bass_band_lo":    (10.0, 400.0),
        "bass_band_hi":    (50.0, 800.0),
        "mid_band_lo":     (80.0, 1500.0),
        "mid_band_hi":     (500.0, 6000.0),
        "treble_band_lo":  (1000.0, 10000.0),
        "treble_band_hi":  (2000.0, 20000.0),
        "high_band_lo":    (2000.0, 10000.0),
        "high_band_hi":    (4000.0, 22000.0),
        "active_rms_floor": (0.002, 0.1),
        "long_rms_floor":   (0.001, 0.1),
        "beat_min_bass":    (0.0005, 0.05),
        "beat_spike_ratio": (1.05, 3.0),
        "beat_refractory_ms": (80.0, 600.0),
        "bass_baseline_tau_s": (0.1, 3.0),
        "snare_flux_min":   (0.0005, 0.2),
        "snare_spike_ratio": (1.1, 4.0),
        "snare_refractory_ms": (40.0, 400.0),
        "mid_flux_baseline_tau_s": (0.05, 2.0),
        "hat_flux_min":     (0.0005, 0.2),
        "hat_spike_ratio":  (1.1, 4.0),
        "hat_refractory_ms": (20.0, 250.0),
        "high_flux_baseline_tau_s": (0.05, 2.0),
        "level_chorus_floor": (0.005, 0.15),
        "level_high_floor":   (0.02, 0.25),
        "drop_score_min":     (1.1, 5.0),
        "drop_rms_min":       (0.005, 0.15),
        "bpm_window_beats":   (3.0, 30.0),
        "bpm_min": (30.0, 120.0),
        "bpm_max": (120.0, 300.0),
        "bpm_autocorr_window_s": (0.0, 12.0),
    }
    for key, (lo, hi) in ranges.items():
        if key in payload:
            try:
                v = float(payload[key])
            except Exception:
                continue
            out[key] = max(lo, min(hi, v))
    return out


def _band_energy(spectrum: "Any", freqs: "Any", lo: float, hi: float) -> float:
    import numpy as np
    mask = (freqs >= lo) & (freqs < hi)
    if not mask.any():
        return 0.0
    bins = spectrum[mask]
    if bins.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(bins * bins)))


def _band_flux(spectrum: "Any", prev_spectrum: "Any", freqs: "Any", lo: float, hi: float) -> float:
    """Half-wave rectified spectral flux in [lo, hi] Hz.

    Computes sum of positive bin-wise differences within the band, normalized
    by bin count so values are comparable across band widths."""
    import numpy as np
    if prev_spectrum is None or prev_spectrum.shape != spectrum.shape:
        return 0.0
    mask = (freqs >= lo) & (freqs < hi)
    if not mask.any():
        return 0.0
    diff = spectrum[mask] - prev_spectrum[mask]
    pos = np.maximum(0.0, diff)
    n = pos.size
    if n == 0:
        return 0.0
    return float(pos.sum() / n)


def _autocorr_bpm(onsets: "Any", frame_rate_hz: float, bpm_min: float, bpm_max: float):
    """Estimate BPM and confidence from a binary onset series via autocorrelation.

    Returns (bpm, confidence) where confidence ∈ [0,1] is the ratio of the
    main peak to the runner-up (1.0 = unambiguous, 0.0 = noise).

    `onsets` is a 1D float array (1.0 on detected onset frames, 0.0 elsewhere)
    of length N representing the last ~N/frame_rate seconds.
    """
    import numpy as np
    if onsets is None or onsets.size < 16 or frame_rate_hz <= 0:
        return 0.0, 0.0
    n = onsets.size
    # Subtract mean so periodic structure stands out vs DC
    x = onsets - float(onsets.mean())
    # Direct autocorrelation; series is short (~280 samples) so fine.
    ac = np.correlate(x, x, mode="full")[n - 1:]
    if ac.size < 4 or ac[0] <= 1e-9:
        return 0.0, 0.0
    ac = ac / ac[0]
    # Lag range corresponding to [bpm_min, bpm_max]
    min_lag = max(1, int(round(60.0 * frame_rate_hz / max(1.0, bpm_max))))
    max_lag = min(ac.size - 1, int(round(60.0 * frame_rate_hz / max(1.0, bpm_min))))
    if max_lag <= min_lag + 1:
        return 0.0, 0.0
    region = ac[min_lag:max_lag + 1]
    peak_idx = int(np.argmax(region))
    peak_val = float(region[peak_idx])
    if peak_val <= 0.0:
        return 0.0, 0.0
    # Find runner-up outside a small neighborhood of the main peak
    masked = region.copy()
    guard = max(1, int(round(min_lag * 0.15)))
    lo = max(0, peak_idx - guard)
    hi = min(masked.size, peak_idx + guard + 1)
    masked[lo:hi] = -1.0
    second_val = float(masked.max()) if masked.size > 0 else 0.0
    second_val = max(0.0, second_val)
    confidence = max(0.0, min(1.0, 1.0 - (second_val / max(1e-6, peak_val))))
    lag = peak_idx + min_lag
    bpm = 60.0 * frame_rate_hz / max(1.0, float(lag))
    return float(bpm), float(confidence)


def _windowed_means(buf: "Any", write_idx: int, filled: int, short_n: int, long_n: int) -> "tuple[float, float]":
    """Mean of the last ``short_n`` and ``long_n`` samples in a ring buffer."""
    import numpy as np
    if filled <= 0:
        return 0.0, 0.0
    capacity = buf.shape[0]
    short_n = min(short_n, filled)
    long_n = min(long_n, filled)

    def _mean_last(n: int) -> float:
        if n <= 0:
            return 0.0
        start = (write_idx - n) % capacity
        if start + n <= capacity:
            view = buf[start:start + n]
        else:
            view = np.concatenate((buf[start:], buf[: (start + n) % capacity]))
        return float(view.mean())

    return _mean_last(short_n), _mean_last(long_n)


def _build_slope(buf: "Any", write_idx: int, filled: int, window: int) -> float:
    """Linear regression slope (unit per frame) over last ``window`` samples."""
    import numpy as np
    if filled <= 1 or window <= 1:
        return 0.0
    capacity = buf.shape[0]
    n = min(window, filled)
    start = (write_idx - n) % capacity
    if start + n <= capacity:
        y = buf[start:start + n]
    else:
        y = np.concatenate((buf[start:], buf[: (start + n) % capacity]))
    x = np.arange(n, dtype=np.float32)
    # Closed-form slope: cov(x, y) / var(x).
    x_mean = x.mean()
    y_mean = y.mean()
    num = float(((x - x_mean) * (y - y_mean)).sum())
    den = float(((x - x_mean) ** 2).sum())
    if den <= 1e-9:
        return 0.0
    return num / den


def _classify_level(
    long_rms: float,
    long_bass: float,
    long_mid: float,
    long_treble: float,
    drop_score: float,
    spectral_flux: float,
    active: bool,
    tuning: Dict[str, float],
) -> int:
    """Intensity bucket 0–4 using RMS thresholds + band-balance hints.

    The band terms let us distinguish a bass-heavy drop from a mid-heavy
    chorus of the same overall loudness.
    """
    if not active or long_rms < tuning["long_rms_floor"]:
        return 0

    # Drop: strong short/long spike, above drop floor, AND bass-dominant
    # (drops are almost always bass-heavy).
    bass_dominant = long_bass > max(long_mid, long_treble) * 1.05
    if (
        drop_score >= tuning["drop_score_min"]
        and long_rms >= tuning["drop_rms_min"]
        and bass_dominant
    ):
        return 4

    if long_rms >= tuning["level_high_floor"]:
        return 3
    if long_rms >= tuning["level_chorus_floor"]:
        return 2
    return 1


def _spectrum_bins(freqs: "Any", sample_rate: int, bands: int = _SPECTRUM_BANDS) -> "Any":
    """Return a list of (start_idx, end_idx) into the FFT bins for log-spaced bands.

    Cheap; called once per stream reopen. Bands cover 30 Hz → 12 kHz.
    """
    import numpy as np
    lo_freq = 30.0
    hi_freq = min(12000.0, sample_rate / 2.0)
    edges = np.logspace(math.log10(lo_freq), math.log10(hi_freq), bands + 1)
    out = []
    for i in range(bands):
        mask = (freqs >= edges[i]) & (freqs < edges[i + 1])
        idxs = np.where(mask)[0]
        if idxs.size == 0:
            out.append((0, 0))
        else:
            out.append((int(idxs[0]), int(idxs[-1]) + 1))
    return out


def _collect_spectrum(spectrum: "Any", bins: list) -> list:
    """Reduce a full FFT magnitude array to ``len(bins)`` band peaks (0–1)."""
    out = []
    max_seen = 0.0
    for lo, hi in bins:
        if hi > lo:
            seg = spectrum[lo:hi]
            if len(seg) > 0:
                v = float(seg.max())
                max_seen = max(max_seen, v)
                out.append(v)
                continue
        out.append(0.0)
    # Normalize: floor at a reasonable magnitude so quiet music still paints
    # visible bars; clip loud music to 1.0.
    ceiling = max(0.05, max_seen * 1.1)
    return [max(0.0, min(1.0, v / ceiling)) for v in out]


class AudioAnalyzer:
    """Background WASAPI-loopback analyzer.

    Feature dict published by :meth:`snapshot`:
        available, active, rms, bass, mid, treble, beat, beat_intensity,
        sample_rate, updated_at, error.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._features: Dict[str, Any] = self._empty_features()
        self._import_error: Optional[str] = None
        self._device_request_index: Optional[int] = None  # pending switch
        self._device_current_index: Optional[int] = None
        self._device_current_name: str = ""
        self._device_restart = threading.Event()
        # Tuning knobs, swappable at runtime. Thread reads ``self._tuning``
        # each frame. Writes are atomic (dict replacement) so no lock needed.
        self._tuning: Dict[str, float] = dict(DEFAULT_AUDIO_TUNING)
        # Tap-tempo override: when not None, replaces the estimated BPM.
        self._tap_tempo_bpm: Optional[float] = None
        # One shared PyAudio instance. Creating/terminating a second instance
        # from another thread (e.g. a Flask handler) while the capture stream
        # is running on this one segfaults PortAudio's WASAPI backend.
        self._pa: Any = None
        self._pa_lock = threading.Lock()

        try:
            import numpy  # noqa: F401
            import pyaudiowpatch as _pa
        except Exception as exc:
            self._import_error = f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._features["error"] = self._import_error
            log.info("audio analyzer disabled: %s", self._import_error)
            return

        try:
            self._pa = _pa.PyAudio()
        except Exception as exc:
            self._import_error = f"PyAudio init failed: {exc}"
            with self._lock:
                self._features["error"] = self._import_error
            return

        self._thread = threading.Thread(target=self._run, name="autolight-audio", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._device_restart.set()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            snap = dict(self._features)
        snap["device_index"] = self._device_current_index
        snap["device_name"] = self._device_current_name
        # Tap-tempo override wins over the on-the-fly estimate.
        if self._tap_tempo_bpm is not None:
            snap["bpm"] = float(self._tap_tempo_bpm)
            snap["bpm_source"] = "tap"
        else:
            snap.setdefault("bpm_source", "auto")
        snap["tuning"] = dict(self._tuning)
        return snap

    def set_tuning(self, payload: Any) -> Dict[str, float]:
        """Merge ``payload`` into the live tuning dict. Returns the new values."""
        if isinstance(payload, dict):
            merged_input = {**self._tuning, **payload}
        else:
            merged_input = self._tuning
        merged = _clamp_tuning(merged_input)
        self._tuning = merged
        # Force a stream restart so the spectrum-band index gets recomputed
        # with new band edges (simple & bug-free).
        self._device_restart.set()
        return dict(merged)

    def set_tap_tempo(self, bpm: Optional[float]) -> None:
        """Override the estimated BPM with a manual tap value (None clears)."""
        if bpm is None or bpm <= 0:
            self._tap_tempo_bpm = None
        else:
            self._tap_tempo_bpm = float(max(30.0, min(300.0, bpm)))

    def list_devices(self) -> List[Dict[str, Any]]:
        """Enumerate available WASAPI loopback devices using the shared PyAudio."""
        if self._import_error or self._pa is None:
            return []
        out: List[Dict[str, Any]] = []
        with self._pa_lock:
            try:
                for info in self._pa.get_loopback_device_info_generator():
                    out.append({
                        "index": int(info.get("index", -1)),
                        "name": str(info.get("name", "")).strip(),
                        "channels": int(info.get("maxInputChannels", 0) or 0),
                        "sample_rate": int(info.get("defaultSampleRate", 0) or 0),
                    })
            except Exception as exc:
                log.debug("list_devices failed: %s", exc)
        return out

    def select_device(self, index: Optional[int]) -> None:
        """Request a switch to a specific WASAPI loopback device index.

        Pass ``None`` to fall back to the system default. The change is
        applied on the next capture frame (the current stream is closed
        and a new one is opened on the requested device).
        """
        if self._import_error:
            return
        self._device_request_index = int(index) if index is not None else None
        self._device_restart.set()

    @staticmethod
    def _empty_features() -> Dict[str, Any]:
        return {
            "available": False,
            "active": False,
            "rms": 0.0,
            "bass": 0.0,
            "mid": 0.0,
            "treble": 0.0,
            # Adaptive 0..1 normalization (rolling P95 over recent history).
            "bass_norm": 0.0,
            "mid_norm": 0.0,
            "treble_norm": 0.0,
            "beat": False,
            "beat_intensity": 0.0,
            "beat_count": 0,
            "kick": False,
            "kick_count": 0,
            "snare": False,
            "snare_count": 0,
            "snare_intensity": 0.0,
            "hat": False,
            "hat_count": 0,
            "hat_intensity": 0.0,
            "flux_mid": 0.0,
            "flux_high": 0.0,
            "bpm": 0.0,
            "bpm_confidence": 0.0,
            "bpm_source": "auto",
            "bpm_method": "median",
            "bar_count": 0,
            "last_beat_ms": 0.0,
            "last_snare_ms": 0.0,
            "last_hat_ms": 0.0,
            "sample_rate": 0,
            "updated_at": 0,
            "error": None,
            "spectrum": [0.0] * _SPECTRUM_BANDS,
            "structure": {
                "level": 0,
                "label": "silent",
                "drop_score": 0.0,
                "build_up_slope": 0.0,
                "spectral_flux": 0.0,
                "long_rms": 0.0,
                "short_rms": 0.0,
                "long_bass": 0.0,
                "long_mid": 0.0,
                "long_treble": 0.0,
            },
        }

    def _publish(self, **kwargs: Any) -> None:
        with self._lock:
            self._features.update(kwargs)
            self._features["updated_at"] = int(time.time() * 1000)

    def _run(self) -> None:
        import numpy as np
        import pyaudiowpatch as pa

        pyaudio = self._pa
        if pyaudio is None:
            self._publish(error="PyAudio not initialized")
            return
        # Persistent per-signal smoothing state survives device switches so the
        # meters don't flicker back to zero when the user changes source.
        smoothed_bass = 0.0
        smoothed_mid = 0.0
        smoothed_treble = 0.0
        smoothed_rms = 0.0
        bass_baseline = 0.0
        last_beat_ms = 0.0
        beat_count = 0
        kick_count = 0  # alias of beat_count (bass-onset trigger)
        # Snare / hat state — flux-based onsets in mid and high bands.
        smoothed_flux_mid = 0.0
        smoothed_flux_high = 0.0
        flux_mid_baseline = 0.0
        flux_high_baseline = 0.0
        last_snare_ms = 0.0
        last_hat_ms = 0.0
        snare_count = 0
        hat_count = 0
        snare_env = 0.0
        hat_env = 0.0
        # BPM estimator: keep last 8 inter-beat intervals, median → BPM.
        beat_intervals_ms: List[float] = []
        estimated_bpm = 0.0
        bpm_confidence_value = 0.0
        bpm_method = "median"
        last_bar_beat_index = 0
        bar_index = 0

        # Structure-detector state (pre-allocated ring buffers, zero-copy).
        history_bass = np.zeros(_HISTORY_FRAMES, dtype=np.float32)
        history_mid = np.zeros(_HISTORY_FRAMES, dtype=np.float32)
        history_treble = np.zeros(_HISTORY_FRAMES, dtype=np.float32)
        history_rms = np.zeros(_HISTORY_FRAMES, dtype=np.float32)
        # Binary onset history (kick OR snare) for autocorrelation BPM.
        # Sized to the longest BPM window we accept (12 s at ~47 Hz ≈ 564).
        onset_history = np.zeros(_HISTORY_FRAMES, dtype=np.float32)
        history_write = 0
        history_filled = 0
        prev_spectrum: Optional["np.ndarray"] = None
        committed_level = 0
        pending_level = 0
        pending_level_since_ms = 0.0
        # Autocorr BPM is recomputed at most every ~250 ms (cheap but not free).
        last_bpm_compute_ms = 0.0

        try:
            while not self._stop.is_set():
                with self._pa_lock:
                    device = self._resolve_device(pyaudio)
                if device is None:
                    self._publish(available=False, error="no WASAPI loopback device found")
                    self._device_current_index = None
                    self._device_current_name = ""
                    time.sleep(0.5)
                    continue

                sr = int(device.get("defaultSampleRate") or _TARGET_SAMPLE_RATE)
                ch = max(1, int(device.get("maxInputChannels") or 2))
                self._device_current_index = int(device.get("index", -1))
                self._device_current_name = str(device.get("name", "")).strip()

                stream = None
                try:
                    with self._pa_lock:
                        stream = pyaudio.open(
                            format=pa.paFloat32,
                            channels=ch,
                            rate=sr,
                            frames_per_buffer=_FRAME_SAMPLES,
                            input=True,
                            input_device_index=device["index"],
                        )
                    self._publish(available=True, sample_rate=sr, error=None)

                    window = np.hanning(_FFT_SIZE).astype(np.float32)
                    freqs = np.fft.rfftfreq(_FFT_SIZE, d=1.0 / sr)
                    spectrum_bins = _spectrum_bins(freqs, sr)

                    self._device_restart.clear()
                    while not self._stop.is_set() and not self._device_restart.is_set():
                        try:
                            raw = stream.read(_FRAME_SAMPLES, exception_on_overflow=False)
                        except Exception as exc:
                            self._publish(error=f"{type(exc).__name__}: {exc}")
                            time.sleep(0.25)
                            continue

                        data = np.frombuffer(raw, dtype=np.float32)
                        if data.size == 0:
                            continue
                        if ch > 1:
                            data = data.reshape(-1, ch).mean(axis=1)
                        if data.size < _FFT_SIZE:
                            data = np.pad(data, (0, _FFT_SIZE - data.size))
                        else:
                            data = data[:_FFT_SIZE]

                        # Snapshot tuning once per frame for stable reads.
                        tun = self._tuning
                        frame_dt = _FRAME_SAMPLES / sr
                        alpha_baseline = 1.0 - math.exp(-frame_dt / max(0.05, tun["bass_baseline_tau_s"]))
                        alpha_mid_flux = 1.0 - math.exp(-frame_dt / max(0.05, tun["mid_flux_baseline_tau_s"]))
                        alpha_high_flux = 1.0 - math.exp(-frame_dt / max(0.05, tun["high_flux_baseline_tau_s"]))

                        rms = float(np.sqrt(np.mean(data * data)))

                        windowed = data * window
                        spectrum = np.abs(np.fft.rfft(windowed)) / (_FFT_SIZE / 2)
                        bass = _band_energy(spectrum, freqs, tun["bass_band_lo"], tun["bass_band_hi"])
                        mid = _band_energy(spectrum, freqs, tun["mid_band_lo"], tun["mid_band_hi"])
                        treble = _band_energy(spectrum, freqs, tun["treble_band_lo"], tun["treble_band_hi"])

                        # Per-band spectral flux (snare / hat onset signal). Half-wave
                        # rectified, normalized by bin count so values match across bands.
                        flux_mid_raw = _band_flux(
                            spectrum, prev_spectrum, freqs,
                            tun["mid_band_lo"], tun["mid_band_hi"],
                        )
                        flux_high_raw = _band_flux(
                            spectrum, prev_spectrum, freqs,
                            tun["high_band_lo"], tun["high_band_hi"],
                        )

                        smoothed_bass = self._ema(smoothed_bass, bass)
                        smoothed_mid = self._ema(smoothed_mid, mid)
                        smoothed_treble = self._ema(smoothed_treble, treble)
                        smoothed_rms = self._ema(smoothed_rms, rms)
                        # Flux EMAs use plain attack/release like the band envelopes.
                        smoothed_flux_mid = self._ema(smoothed_flux_mid, flux_mid_raw)
                        smoothed_flux_high = self._ema(smoothed_flux_high, flux_high_raw)

                        bass_baseline += alpha_baseline * (smoothed_bass - bass_baseline)
                        flux_mid_baseline += alpha_mid_flux * (smoothed_flux_mid - flux_mid_baseline)
                        flux_high_baseline += alpha_high_flux * (smoothed_flux_high - flux_high_baseline)

                        now_ms = time.monotonic() * 1000.0
                        beat = False
                        beat_intensity = 0.0
                        refractory_ok = (now_ms - last_beat_ms) > tun["beat_refractory_ms"]
                        if (
                            refractory_ok
                            and smoothed_bass > tun["beat_min_bass"]
                            and smoothed_bass > bass_baseline * tun["beat_spike_ratio"]
                        ):
                            beat = True
                            beat_intensity = min(1.0, (smoothed_bass - bass_baseline) / max(1e-6, bass_baseline))
                            if last_beat_ms > 0:
                                interval = now_ms - last_beat_ms
                                # Sanity window based on configured bpm min/max.
                                iv_min = 60000.0 / max(30.0, tun["bpm_max"])
                                iv_max = 60000.0 / max(30.0, tun["bpm_min"])
                                if iv_min <= interval <= iv_max:
                                    beat_intervals_ms.append(interval)
                                    window_size = max(3, int(tun["bpm_window_beats"]))
                                    if len(beat_intervals_ms) > window_size:
                                        del beat_intervals_ms[: len(beat_intervals_ms) - window_size]
                            last_beat_ms = now_ms
                            beat_count += 1
                            kick_count = beat_count
                            if (beat_count - last_bar_beat_index) >= 4:
                                last_bar_beat_index = beat_count
                                bar_index += 1

                        # Snare detection: flux peak in mid band, separate refractory.
                        snare_hit = False
                        snare_intensity = 0.0
                        if (
                            (now_ms - last_snare_ms) > tun["snare_refractory_ms"]
                            and smoothed_flux_mid > tun["snare_flux_min"]
                            and smoothed_flux_mid > flux_mid_baseline * tun["snare_spike_ratio"]
                        ):
                            snare_hit = True
                            snare_count += 1
                            last_snare_ms = now_ms
                            snare_intensity = min(1.0, (smoothed_flux_mid - flux_mid_baseline) / max(1e-6, flux_mid_baseline))
                            snare_env = max(snare_env, snare_intensity)
                        # Decay snare/hat envelopes (visual smoothing for UI/director).
                        snare_env *= math.exp(-frame_dt / 0.10)
                        hat_env *= math.exp(-frame_dt / 0.04)

                        # Hat detection: flux peak in high band, short refractory.
                        hat_hit = False
                        hat_intensity = 0.0
                        if (
                            (now_ms - last_hat_ms) > tun["hat_refractory_ms"]
                            and smoothed_flux_high > tun["hat_flux_min"]
                            and smoothed_flux_high > flux_high_baseline * tun["hat_spike_ratio"]
                        ):
                            hat_hit = True
                            hat_count += 1
                            last_hat_ms = now_ms
                            hat_intensity = min(1.0, (smoothed_flux_high - flux_high_baseline) / max(1e-6, flux_high_baseline))
                            hat_env = max(hat_env, hat_intensity)

                        active = smoothed_rms > tun["active_rms_floor"]

                        # Update ring buffers (bass / mid / treble / rms).
                        history_bass[history_write] = smoothed_bass
                        history_mid[history_write] = smoothed_mid
                        history_treble[history_write] = smoothed_treble
                        history_rms[history_write] = smoothed_rms
                        # Binary onset series: 1 on kick-or-snare frames. Snares
                        # alone aren't reliable on bass-heavy tracks, kicks alone
                        # miss tracks where snare carries the groove — combining
                        # both makes autocorr lock faster.
                        onset_history[history_write] = 1.0 if (beat or snare_hit) else 0.0
                        history_write = (history_write + 1) % _HISTORY_FRAMES
                        if history_filled < _HISTORY_FRAMES:
                            history_filled += 1

                        # Wide-band spectral flux retained for structure detection.
                        if prev_spectrum is not None and prev_spectrum.shape == spectrum.shape:
                            spectral_flux = float(np.sum(np.maximum(0.0, spectrum - prev_spectrum)))
                        else:
                            spectral_flux = 0.0
                        prev_spectrum = spectrum.copy()

                        # BPM estimation. Try autocorrelation on the onset train
                        # first (more robust), fall back to median of intervals.
                        bpm_method = "median"
                        bpm_confidence_value = 0.0
                        autocorr_window_s = float(tun.get("bpm_autocorr_window_s", 0.0) or 0.0)
                        if autocorr_window_s > 0 and history_filled > int(autocorr_window_s * (sr / _FRAME_SAMPLES)):
                            # Recompute every ~250 ms to keep CPU low.
                            if (now_ms - last_bpm_compute_ms) > 250.0:
                                last_bpm_compute_ms = now_ms
                                window_frames = min(history_filled, int(autocorr_window_s * (sr / _FRAME_SAMPLES)))
                                # Read the last `window_frames` from the ring.
                                start = (history_write - window_frames) % _HISTORY_FRAMES
                                if start + window_frames <= _HISTORY_FRAMES:
                                    onset_window = onset_history[start:start + window_frames]
                                else:
                                    onset_window = np.concatenate(
                                        (onset_history[start:], onset_history[:(start + window_frames) % _HISTORY_FRAMES])
                                    )
                                if onset_window.sum() >= 4:  # need at least a few onsets
                                    ac_bpm, ac_conf = _autocorr_bpm(
                                        onset_window,
                                        frame_rate_hz=sr / _FRAME_SAMPLES,
                                        bpm_min=tun["bpm_min"],
                                        bpm_max=tun["bpm_max"],
                                    )
                                    if ac_bpm > 0 and ac_conf > 0.15:
                                        estimated_bpm = ac_bpm
                                        bpm_confidence_value = ac_conf
                                        bpm_method = "autocorr"
                        # Median fallback (always recomputed; cheap)
                        if bpm_method == "median" and len(beat_intervals_ms) >= 3:
                            sorted_ivs = sorted(beat_intervals_ms)
                            mid_iv = sorted_ivs[len(sorted_ivs) // 2]
                            if mid_iv > 0:
                                estimated_bpm = 60000.0 / mid_iv
                            mean_iv = sum(beat_intervals_ms) / len(beat_intervals_ms)
                            if mean_iv > 0:
                                var = sum((x - mean_iv) ** 2 for x in beat_intervals_ms) / len(beat_intervals_ms)
                                stddev = var ** 0.5
                                bpm_confidence_value = max(0.0, min(1.0, 1.0 - (stddev / mean_iv) * 3.0))

                        short_rms, long_rms = _windowed_means(
                            history_rms, history_write, history_filled,
                            _SHORT_FRAMES, _LONG_FRAMES,
                        )
                        _, long_bass = _windowed_means(history_bass, history_write, history_filled, 1, _LONG_FRAMES)
                        _, long_mid = _windowed_means(history_mid, history_write, history_filled, 1, _LONG_FRAMES)
                        _, long_treble = _windowed_means(history_treble, history_write, history_filled, 1, _LONG_FRAMES)
                        drop_score = short_rms / max(long_rms, 1e-6) if long_rms > 1e-5 else 0.0

                        build_up_slope = _build_slope(history_bass, history_write, history_filled, _BUILD_FRAMES)

                        # Adaptive band normalization: rolling P95 over the
                        # available history. Cheap (recomputed every ~5 frames),
                        # makes the meters look right regardless of source level.
                        if (history_filled & 0x07) == 0 and history_filled >= 16:
                            n = min(history_filled, _HISTORY_FRAMES)
                            # numpy.quantile is O(n) average. ~470 floats is fast.
                            p95_bass = float(np.quantile(history_bass[:n], 0.95))
                            p95_mid = float(np.quantile(history_mid[:n], 0.95))
                            p95_treble = float(np.quantile(history_treble[:n], 0.95))
                        else:
                            p95_bass = locals().get("p95_bass", 0.0)
                            p95_mid = locals().get("p95_mid", 0.0)
                            p95_treble = locals().get("p95_treble", 0.0)
                        bass_norm = min(1.0, smoothed_bass / max(p95_bass, 0.005))
                        mid_norm = min(1.0, smoothed_mid / max(p95_mid, 0.004))
                        treble_norm = min(1.0, smoothed_treble / max(p95_treble, 0.002))

                        raw_level = _classify_level(
                            long_rms, long_bass, long_mid, long_treble,
                            drop_score, spectral_flux, active, tun,
                        )
                        if raw_level != pending_level:
                            pending_level = raw_level
                            pending_level_since_ms = now_ms
                        if pending_level != committed_level and (now_ms - pending_level_since_ms) > _LEVEL_HYSTERESIS_MS:
                            committed_level = pending_level

                        spectrum_bands = _collect_spectrum(spectrum, spectrum_bins)

                        self._publish(
                            active=active,
                            rms=smoothed_rms,
                            bass=smoothed_bass,
                            mid=smoothed_mid,
                            treble=smoothed_treble,
                            bass_norm=float(bass_norm),
                            mid_norm=float(mid_norm),
                            treble_norm=float(treble_norm),
                            beat=beat,
                            beat_intensity=beat_intensity,
                            beat_count=beat_count,
                            kick=beat,
                            kick_count=kick_count,
                            snare=snare_hit,
                            snare_count=snare_count,
                            snare_intensity=float(snare_env),
                            hat=hat_hit,
                            hat_count=hat_count,
                            hat_intensity=float(hat_env),
                            flux_mid=float(smoothed_flux_mid),
                            flux_high=float(smoothed_flux_high),
                            bpm=estimated_bpm,
                            bpm_confidence=bpm_confidence_value,
                            bpm_method=bpm_method,
                            bar_count=bar_index,
                            last_beat_ms=last_beat_ms,
                            last_snare_ms=last_snare_ms,
                            last_hat_ms=last_hat_ms,
                            spectrum=spectrum_bands,
                            structure={
                                "level": committed_level,
                                "label": _INTENSITY_LABELS.get(committed_level, "silent"),
                                "drop_score": float(drop_score),
                                "build_up_slope": float(build_up_slope),
                                "spectral_flux": float(spectral_flux),
                                "long_rms": float(long_rms),
                                "short_rms": float(short_rms),
                                "long_bass": float(long_bass),
                                "long_mid": float(long_mid),
                                "long_treble": float(long_treble),
                            },
                        )
                except Exception as exc:
                    log.warning("audio analyzer stream error: %s", exc)
                    self._publish(available=False, error=f"{type(exc).__name__}: {exc}")
                    time.sleep(0.5)
                finally:
                    if stream is not None:
                        try:
                            with self._pa_lock:
                                stream.stop_stream()
                                stream.close()
                        except Exception:
                            pass
        finally:
            try:
                with self._pa_lock:
                    pyaudio.terminate()
                    self._pa = None
            except Exception:
                pass

    @staticmethod
    def _ema(prev: float, new: float) -> float:
        coeff = _ATTACK if new > prev else _RELEASE
        return prev + coeff * (new - prev)

    def _resolve_device(self, pyaudio: "Any") -> Optional[Dict[str, Any]]:
        """Pick the capture device: honor an explicit request, else system default."""
        import pyaudiowpatch as pa

        requested = self._device_request_index
        if requested is not None:
            try:
                for info in pyaudio.get_loopback_device_info_generator():
                    if int(info.get("index", -1)) == int(requested):
                        return info
            except Exception as exc:
                log.debug("requested device lookup failed: %s", exc)
            log.info("requested audio device %s not found, falling back to default", requested)

        try:
            default = pyaudio.get_default_wasapi_loopback()
            if default:
                return default
        except Exception:
            pass
        try:
            wasapi_info = pyaudio.get_host_api_info_by_type(pa.paWASAPI)
            default_out = pyaudio.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
            target_name = default_out.get("name", "")
            for loopback in pyaudio.get_loopback_device_info_generator():
                if target_name and target_name in loopback.get("name", ""):
                    return loopback
            for loopback in pyaudio.get_loopback_device_info_generator():
                return loopback
        except Exception as exc:
            log.debug("loopback discovery failed: %s", exc)
        return None
