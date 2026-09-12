"""
Background A-weighted level meter on the default Windows output device.

The worker thread only touches audio: it captures the WASAPI loopback stream
and reduces each block to an A-weighted RMS in dBFS. The endpoint volume is
read by the caller on its own thread, which keeps COM out of the audio loop.

    SPL(A) = REF_SPL_AT_FULL_SCALE + dBFS(A) + master attenuation
"""

import math
import re
import threading
import time
from collections import deque

import numpy as np
import pyaudiowpatch as pyaudio

from . import devices

# Fallback when the headphone model cannot be identified. Per-model values
# live in devices.py. This is the weak link in the whole estimate: it is a
# published figure for a family of headphones, not a measurement of the unit
# in your ears, so treat readings as +/- 10 dB unless you calibrate.
REF_SPL_AT_FULL_SCALE = devices.DEFAULT_REFERENCE

BLOCK_SEC = 0.1
SMOOTH_SEC = 1.0        # moving average, so the number does not jitter
SILENCE_DBFS = -90.0
STALE_SEC = 0.35        # no fresh block for this long means playback stopped

DELTA = 0.0015


def a_weighting_db(freqs):
    """IEC 61672 A-weighting curve, in dB, normalised to 0 dB at 1 kHz."""
    f = np.asarray(freqs, dtype=np.float64)
    f2 = f ** 2
    num = (12194.0 ** 2) * (f2 ** 2)
    den = ((f2 + 20.6 ** 2)
           * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2))
           * (f2 + 12194.0 ** 2))
    with np.errstate(divide="ignore", invalid="ignore"):
        db = 20.0 * np.log10(num / den) + 2.00
    db[~np.isfinite(db)] = -200.0
    return db


def pretty_device_name(name):
    """Strip the Windows endpoint wrapper.

    'Headset (My AirPods Pro - Find My)' -> 'My AirPods Pro'
    """
    if not name:
        return None
    inner = re.search(r"\(([^()]+)\)\s*$", name)
    if inner:
        name = inner.group(1)
    name = re.sub(r"\s*[-–]\s*Find My\s*$", "", name, flags=re.I)
    return name.strip() or None


def _same_device(a, b):
    """Tolerant match: PortAudio truncates long device names, so one side can
    legitimately be a prefix of the other."""
    if not a or not b:
        return False
    a, b = a.strip().lower(), b.strip().lower()
    return a == b or a.startswith(b) or b.startswith(a)


class LoopbackMeter:
    """Runs in the background and exposes the latest A-weighted dBFS."""

    def __init__(self):
        self._lock = threading.Lock()
        self._dbfs_a = None
        self._updated = 0.0
        self._device_name = None
        self._error = None
        self._stop = threading.Event()
        self._restart = threading.Event()
        self._thread = None

    # -- public -------------------------------------------------------------

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def request_restart(self):
        """Reopen on the current default output.

        The capture loop only ever left on an error, so when the headphones
        went away Windows moved the default output elsewhere, the loop bound
        itself to that device, and reading it kept succeeding forever. It
        never came back when the headphones returned.
        """
        self._restart.set()

    @property
    def dbfs_a(self):
        """Latest level, or None.

        On pause WASAPI stops handing out blocks rather than handing out
        silent ones, so a stale reading means silence, not a steady level.
        """
        with self._lock:
            if self._dbfs_a is None:
                return None
            if time.time() - self._updated > STALE_SEC:
                return None
            return self._dbfs_a

    @property
    def device_name(self):
        with self._lock:
            return self._device_name

    @property
    def error(self):
        with self._lock:
            return self._error

    # -- worker -------------------------------------------------------------

    def _pick_loopback(self, p):
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        dev = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if dev.get("isLoopbackDevice"):
            return dev
        for lb in p.get_loopback_device_info_generator():
            if dev["name"] in lb["name"]:
                return lb
        raise RuntimeError("no loopback device for " + dev["name"])

    def _run(self):
        while not self._stop.is_set():
            p = None
            stream = None
            try:
                p = pyaudio.PyAudio()
                dev = self._pick_loopback(p)
                rate = int(dev["defaultSampleRate"])
                channels = int(dev["maxInputChannels"])
                chunk = int(rate * BLOCK_SEC)

                window = np.hanning(chunk)
                win_energy = float((window ** 2).sum())
                freqs = np.fft.rfftfreq(chunk, 1.0 / rate)
                a_power = 10.0 ** (a_weighting_db(freqs) / 10.0)

                stream = p.open(format=pyaudio.paFloat32, channels=channels,
                                rate=rate, input=True,
                                input_device_index=dev["index"],
                                frames_per_buffer=chunk)

                name = dev["name"].replace(" [Loopback]", "")
                history = deque(maxlen=max(1, int(SMOOTH_SEC / BLOCK_SEC)))
                self._restart.clear()
                with self._lock:
                    self._device_name = name
                    self._error = None

                while not self._stop.is_set() and not self._restart.is_set():
                    raw = stream.read(chunk, exception_on_overflow=False)
                    data = np.frombuffer(raw, dtype=np.float32)
                    if channels > 1:
                        data = data.reshape(-1, channels).mean(axis=1)
                    if len(data) < chunk:
                        continue

                    spec = np.fft.rfft(data * window)
                    power = np.abs(spec) ** 2
                    power[1:-1] *= 2.0
                    ms = float((power * a_power).sum()) / (chunk * win_energy)

                    if ms <= 0:
                        value = None
                    else:
                        value = 10.0 * math.log10(ms)
                        if value < SILENCE_DBFS:
                            value = None

                    # energy-average the recent blocks
                    history.append(0.0 if value is None else 10.0 ** (value / 10.0))
                    mean = sum(history) / len(history)
                    smoothed = (10.0 * math.log10(mean)) if mean > 0 else None
                    if smoothed is not None and smoothed < SILENCE_DBFS:
                        smoothed = None

                    with self._lock:
                        self._dbfs_a = smoothed
                        self._updated = time.time()

            except Exception as exc:
                with self._lock:
                    self._dbfs_a = None
                    self._error = str(exc)
                # the output device probably changed; back off and retry
                self._stop.wait(0.8)
            finally:
                try:
                    if stream is not None:
                        stream.stop_stream()
                        stream.close()
                except Exception:
                    pass
                try:
                    if p is not None:
                        p.terminate()
                except Exception:
                    pass


class EndpointVolume:
    """Master volume of the default render device. Call from one thread."""

    def __init__(self):
        self._vol = None
        self._name = None
        self._checked = 0.0

    def _refresh(self):
        # the default device can change under us; rebind periodically
        now = time.time()
        if self._vol is not None and now - self._checked < 2.0:
            return
        self._checked = now
        try:
            from pycaw.pycaw import AudioUtilities
            speakers = AudioUtilities.GetSpeakers()
            self._vol = speakers.EndpointVolume
            self._name = speakers.FriendlyName
        except Exception:
            self._vol = None
            self._name = None

    def read(self):
        """(attenuation dB, percent, muted, friendly name)."""
        self._refresh()
        if self._vol is None:
            return None, None, False, None
        try:
            return (self._vol.GetMasterVolumeLevel(),
                    self._vol.GetMasterVolumeLevelScalar() * 100.0,
                    bool(self._vol.GetMute()),
                    self._name)
        except Exception:
            self._vol = None
            return None, None, False, None


class SplEstimator:
    """Combines the loopback meter and the endpoint volume into an SPL.

    The reference level comes from the identified headphone model rather than
    one constant for every device; see devices.py.
    """

    def __init__(self, ref_spl=None):
        self.forced_ref = ref_spl
        self.meter = LoopbackMeter()
        self.volume = EndpointVolume()
        self.device = devices.Device("")
        self._known_name = None
        self._last_restart = 0.0

    def start(self):
        self.meter.start()

    def stop(self):
        self.meter.stop()

    def read(self):
        """dict with spl, playing, volume_pct, muted, device."""
        dbfs = self.meter.dbfs_a
        vol_db, vol_pct, muted, name = self.volume.read()
        device = pretty_device_name(name or self.meter.device_name)

        # The endpoint API sees the default output switch immediately; the
        # capture thread cannot. Compare the two and tell it to reopen when
        # they drift apart - this is what makes unplugging and replugging
        # headphones work.
        capturing = pretty_device_name(self.meter.device_name)
        if name and capturing and not _same_device(device, capturing):
            # rate limited: PortAudio truncates device names on some systems,
            # and a permanent mismatch must degrade to an occasional reopen
            # rather than spin
            now = time.time()
            if now - self._last_restart > 3.0:
                self._last_restart = now
                self.meter.request_restart()

        # re-identify only when the output actually changes: the lookup walks
        # the registry and has no business running on every frame
        if device != self._known_name:
            self._known_name = device
            self.device = devices.identify(device)

        ref = self.forced_ref if self.forced_ref else self.device.reference
        playing = dbfs is not None and not muted and vol_db is not None
        spl = (ref + dbfs + vol_db) if playing else None

        return {"spl": spl, "playing": playing, "dbfs_a": dbfs,
                "volume_db": vol_db, "volume_pct": vol_pct,
                "muted": muted, "device": device, "model": self.device.model,
                "reference": ref, "confidence": self.device.confidence,
                "error": self.meter.error}
