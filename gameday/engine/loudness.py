"""How loud the game is, every half second.

The crowd and the commentators get loud at big plays, so loudness peaks are
the picks when there is no transcript, and they help place each play in
time. Plain numpy on the WAV made for Muse Voice: no model.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

HOP_S = 0.5
SMOOTH_HOPS = 3  # 1.5 s
FLAT_DB = 1.0  # a track whose smoothed level never moves more than this has nothing to go on


@dataclass
class Loudness:
    hop_s: float
    db: np.ndarray  # level per hop, dB below full scale
    smooth: np.ndarray  # moving average of db
    z: np.ndarray  # robust z-score of smooth: how unusually loud each hop is

    @property
    def duration_s(self) -> float:
        return len(self.db) * self.hop_s

    def index(self, t: float) -> int:
        if not len(self.z):
            return 0
        return int(min(len(self.z) - 1, max(0, int(t / self.hop_s))))

    def time(self, i: int) -> float:
        return (i + 0.5) * self.hop_s

    def z_at(self, t: float) -> float:
        return float(self.z[self.index(t)]) if len(self.z) else 0.0


@dataclass
class Peak:
    t: float
    z: float


def empty() -> Loudness:
    nothing = np.zeros(0, dtype=np.float64)
    return Loudness(HOP_S, nothing, nothing, nothing)


def flat(loud: Loudness) -> bool:
    """No sound to go on: none at all, or a level that never rises or falls
    (a muted or silent track still has an audio stream)."""
    return not len(loud.smooth) or float(np.ptp(loud.smooth)) < FLAT_DB


def _finish(db: np.ndarray, hop_s: float) -> Loudness:
    db = np.asarray(db, dtype=np.float64)
    if not len(db):
        return empty()
    k = min(SMOOTH_HOPS, len(db))
    kernel = np.ones(k)
    smooth = np.convolve(db, kernel, mode="same") / np.convolve(np.ones(len(db)), kernel, mode="same")
    median = float(np.median(smooth))
    scale = 1.4826 * float(np.median(np.abs(smooth - median)))
    if scale < 0.5:  # a nearly constant level: fall back to the standard deviation
        scale = float(np.std(smooth))
    z = (smooth - median) / scale if scale > 1e-6 else np.zeros_like(smooth)
    return Loudness(hop_s, db, smooth, z)


def _levels(samples: np.ndarray, hop: int) -> np.ndarray:
    whole = len(samples) // hop
    out = []
    if whole:
        frames = samples[: whole * hop].reshape(whole, hop)
        out.append(np.sqrt(np.mean(frames * frames, axis=1)))
    rest = samples[whole * hop:]
    if len(rest) >= hop // 4 and len(rest):
        out.append(np.array([np.sqrt(np.mean(rest * rest))]))
    if not out:
        return np.zeros(0)
    rms = np.concatenate(out)
    return 20.0 * np.log10(rms + 1e-6)


def from_samples(samples: np.ndarray, rate: int, hop_s: float = HOP_S) -> Loudness:
    """Loudness of mono samples scaled to [-1, 1]."""
    hop = max(1, int(round(rate * hop_s)))
    return _finish(_levels(np.asarray(samples, dtype=np.float32), hop), hop_s)


def measure(wav_path: Path, hop_s: float = HOP_S) -> Loudness:
    """Loudness of a 16-bit PCM WAV file, read a minute at a time."""
    with wave.open(str(wav_path), "rb") as wav:
        rate, channels, width = wav.getframerate(), wav.getnchannels(), wav.getsampwidth()
        if width != 2 or rate <= 0:
            raise ValueError("expected 16-bit PCM audio")
        hop = max(1, int(round(rate * hop_s)))
        block = hop * int(max(1, round(60 / hop_s)))
        parts = []
        while True:
            data = wav.readframes(block)
            if not data:
                break
            samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
            if channels > 1:
                samples = samples[: len(samples) // channels * channels].reshape(-1, channels).mean(axis=1)
            parts.append(_levels(samples, hop))
    return _finish(np.concatenate(parts) if parts else np.zeros(0), hop_s)


def peaks(loud: Loudness, *, min_z: float = 1.5, min_gap_s: float = 15.0, limit: int = 40) -> list[Peak]:
    """The loudest moments, loudest first, at least `min_gap_s` apart."""
    z = loud.z
    if not len(z):
        return []
    reach = 2  # a peak is the loudest hop within a second either side
    found = []
    for i in range(len(z)):
        if z[i] < min_z:
            continue
        lo, hi = max(0, i - reach), min(len(z), i + reach + 1)
        if z[i] >= z[lo:hi].max():
            found.append(i)
    found.sort(key=lambda i: -z[i])
    kept: list[Peak] = []
    for i in found:
        t = loud.time(i)
        if all(abs(t - p.t) >= min_gap_s for p in kept):
            kept.append(Peak(t=round(t, 2), z=round(float(z[i]), 2)))
            if len(kept) >= limit:
                break
    return kept


def onset(loud: Loudness, t: float, *, back_s: float = 6.0) -> float:
    """Where the rise to the peak near `t` began: about when the play happened."""
    if not len(loud.z):
        return t
    i = loud.index(t)
    top = float(loud.z[i])
    floor = max(0.5, 0.4 * top)
    lowest = max(0, i - int(back_s / loud.hop_s))
    j = i
    while j > lowest and loud.z[j - 1] > floor:
        j -= 1
    return round(j * loud.hop_s, 2)


def nearest_peak(loud: Loudness, t: float, *, before_s: float = 3.0, after_s: float = 5.0,
                 min_z: float = 1.0) -> float | None:
    """The time of the loudest hop in [t - before_s, t + after_s], if it stands out."""
    if not len(loud.z):
        return None
    lo, hi = loud.index(t - before_s), loud.index(t + after_s) + 1
    if hi <= lo:
        return None
    i = lo + int(np.argmax(loud.z[lo:hi]))
    return loud.time(i) if loud.z[i] >= min_z else None
