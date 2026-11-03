"""The broadcast's cuts, found in the game itself around each clip.

A game stream switches cameras with hard cuts (to a close-up, the bench, the
crowd) and with wipes (a logo sweeping across before a replay). Both are
found on tiny frames at the source's own frame rate (up to 30 a second), so
a clip can end on the frame before a cut:

* A hard cut is a frame far more different from the one before than its
  neighbours are from theirs, whose colours changed too (a fast pan moves
  the picture but keeps its colours), and that isn't a flash: the frames
  after it don't go back to the ones before.
* A wipe, or a dissolve into another scene, changes the colours a lot over
  half a second without a hard cut. It counts as two cuts, where it begins
  and where it ends, so a clip ends before it and starts after it.

A dissolve between two cameras on the same court changes too little to be
found, and it doesn't end a play.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from gameday.engine import ff

PAD_S = 1.5  # also looked at either side of the span, so a cut at its very edge is found
NEIGHBOURS = 15  # frames either side whose changes are the baseline a cut must stand out from
MIN_JUMP = 0.10  # a hard cut changes the picture by at least this much (mean change, 0..1)
JUMP_OVER_BASELINE = 3.0  # and by this many times the baseline
MIN_COLOUR_CHANGE = 0.12  # and its colours by at least this much (histogram distance, 0..1)
WIPE_FRAMES = 8  # a wipe is judged between frames this far either side of its middle
MIN_WIPE_CHANGE = 0.6
MERGE_S = 0.2  # cuts closer together than this are one


def _histograms(frames: np.ndarray) -> np.ndarray:
    """A 64-bin colour histogram of each frame (four levels a channel), summing to 1."""
    q = (frames >> 6).astype(np.int32)
    bins = q[..., 0] * 16 + q[..., 1] * 4 + q[..., 2]
    n = len(frames)
    counts = np.bincount((bins.reshape(n, -1) + 64 * np.arange(n)[:, None]).ravel(), minlength=64 * n)
    hist = counts.reshape(n, 64).astype(np.float64)
    return hist / np.maximum(1.0, hist.sum(axis=1, keepdims=True))


def find(frames: np.ndarray, times: np.ndarray) -> list[float]:
    """The cuts among `frames` (n, height, width, 3 uint8, shot at `times`): the
    time of each new shot's first frame, and both ends of each wipe."""
    n = len(frames)
    if n < 3:
        return []
    gray = frames.astype(np.float32).mean(axis=3) / 255.0
    jump = np.zeros(n)
    jump[1:] = np.abs(np.diff(gray, axis=0)).mean(axis=(1, 2))
    hist = _histograms(frames)
    colour = np.zeros(n)
    colour[1:] = 0.5 * np.abs(np.diff(hist, axis=0)).sum(axis=1)

    hard = []
    for i in np.nonzero((jump >= MIN_JUMP) & (colour >= MIN_COLOUR_CHANGE))[0]:
        around = np.r_[jump[max(1, i - NEIGHBOURS):max(1, i - 1)], jump[i + 2:i + 2 + NEIGHBOURS]]
        base = float(np.median(around)) if len(around) else 0.0
        if jump[i] < JUMP_OVER_BASELINE * base + 0.02:
            continue  # the camera is moving fast: everything around it changes as much
        if float(np.abs(gray[min(n - 1, i + 3)] - gray[max(0, i - 4)]).mean()) < 0.5 * jump[i]:
            continue  # a flash, or the end of one: the picture goes back to what it was
        hard.append(int(i))
    found = [float(times[i]) for i in hard]

    k = WIPE_FRAMES
    if n > 2 * k:
        # Each frame's colours averaged with two either side, so a flash is no wipe.
        total = np.vstack([np.zeros((1, hist.shape[1])), np.cumsum(hist, axis=0)])
        lo, hi = np.clip(np.arange(n) - 2, 0, n), np.clip(np.arange(n) + 3, 0, n)
        steady = (total[hi] - total[lo]) / (hi - lo)[:, None]
        change = np.zeros(n)
        change[k:n - k] = 0.5 * np.abs(steady[2 * k:] - steady[:n - 2 * k]).sum(axis=1)
        near_cut = np.zeros(n, dtype=bool)
        for i in hard:
            near_cut[max(0, i - k - 1):i + k + 2] = True
        for i in range(k, n - k):
            if change[i] >= MIN_WIPE_CHANGE and not near_cut[i] and change[i] >= change[i - k:i + k + 1].max():
                # Judged between i - k and i + k; a slow one may start a little before.
                found += [float(times[max(0, i - k - 3)]), float(times[i + k])]

    cuts: list[float] = []
    for t in sorted(found):
        if not cuts or t - cuts[-1] >= MERGE_S:
            cuts.append(t)
    return cuts


def scan(source: Path, start: float, end: float, *, timeout: float | None = 600) -> list[float]:
    """The cuts in [start, end] of `source`, in source seconds."""
    a = max(0.0, start - PAD_S)
    frames, times = ff.tiny_frames(source, start=a, seconds=end + PAD_S - a, timeout=timeout)
    return [round(t, 3) for t in find(frames, times) if start <= t <= end]
