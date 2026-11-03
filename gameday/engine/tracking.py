"""The 9:16 window's path through a clip.

There is one window, the full height of the frame, and it only pans left
and right: it never zooms. Its target is the ball from SAM 3.1's boxes,
with a little lead along the ball's motion. The camera has a dead zone and
capped speed and acceleration, and it is never smoothed across a broadcast
cut (at a cut it may jump). Where the ball is missing for under a second
the gap is interpolated; longer gaps follow the players, or hold.

Positions are shares of the frame width (0 = left edge, 1 = right edge), so
the same path drives the small proxy SAM saw and the full-size render.
Pure Python and numpy: no model.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from gameday.types import Box, Segmentation

# What steered each proxy frame.
NONE, BALL, INTERP, PLAYERS, HOLD = 0, 1, 2, 3, 4


@dataclass(frozen=True)
class Camera:
    dead_zone: float = 0.06  # how far the target may drift from the window's centre before it moves
    max_speed: float = 1.0  # frame widths a second
    max_accel: float = 3.0  # frame widths a second, per second
    gain: float = 4.0  # how fast the window closes the gap, per second
    lead_s: float = 0.25  # look this far ahead along the ball's motion
    max_lead: float = 0.08  # frame widths
    smooth_s: float = 0.3  # light smoothing of the target


BALL_CAMERA = Camera()
SPEAKER_CAMERA = Camera(dead_zone=0.08, max_speed=0.5, max_accel=1.5, gain=3.0, lead_s=0.0, max_lead=0.0,
                        smooth_s=0.5)


@dataclass(frozen=True)
class BallFilter:
    min_confidence: float = 0.3
    min_width: float = 0.004  # box width as a share of the frame width
    max_width: float = 0.2
    max_aspect: float = 2.2  # a ball's box is roughly square, even with motion blur
    static_spread: float = 0.012  # an id that moves less than this ...
    static_s: float = 3.0  # ... over this long is a logo or a painted ball
    max_gap_s: float = 1.0  # longer gaps are not interpolated
    max_jump: float = 3.0  # frame widths a second: faster than a ball can move on screen


@dataclass
class Track:
    """The followed thing's centre on each proxy frame (NaN where it wasn't seen)."""

    x: np.ndarray
    y: np.ndarray
    found: np.ndarray  # bool
    static_ids: set[int]


@dataclass
class CropPlan:
    left: np.ndarray  # the window's left edge on each output frame, a share of the frame width
    window: float  # the window's width, a share of the frame width
    fps: float  # output frames a second
    source: str  # Follow.source
    coverage: float  # share of the clip's proxy frames with a usable ball
    frames: int  # proxy frames in the clip
    note: str
    target: np.ndarray | None = None  # what the window aimed at, per output frame (for the debug image)


def window_share(width: int, height: int) -> float:
    """A full-height 9:16 window's width as a share of the frame's width."""
    if width <= 0 or height <= 0:
        return 1.0
    return min(1.0, height * 9.0 / 16.0 / width)


def usable(box: Box, width: int, height: int, f: BallFilter) -> bool:
    w, h = box.right - box.left, box.bottom - box.top
    if w <= 0 or h <= 0 or width <= 0:
        return False
    if box.confidence is not None and box.confidence < f.min_confidence:
        return False
    if not f.min_width <= w / width <= f.max_width:
        return False
    return 1.0 / f.max_aspect <= w / h <= f.max_aspect


def static_ids(boxes: list[Box], width: int, fps: float, f: BallFilter) -> set[int]:
    """Ids that barely move for seconds: logos, a ball painted on the floor."""
    by_id: dict[int, list[Box]] = defaultdict(list)
    for b in boxes:
        by_id[b.object_id].append(b)
    found = set()
    for oid, items in by_id.items():
        frames = [b.frame for b in items]
        if (max(frames) - min(frames)) / max(fps, 1e-6) < f.static_s:
            continue
        cx = [(b.left + b.right) / 2 / width for b in items]
        cy = [(b.top + b.bottom) / 2 / width for b in items]
        if max(cx) - min(cx) < f.static_spread and max(cy) - min(cy) < f.static_spread:
            found.add(oid)
    return found


def _frames(seg: Segmentation) -> int:
    return max(seg.frames, 1 + max((b.frame for b in seg.boxes), default=-1), 0)


def _centre(b: Box, width: int) -> tuple[float, float]:
    return (b.left + b.right) / 2 / width, (b.top + b.bottom) / 2 / width


def _candidate_score(b: Box, width: int, last: tuple[float, float, int, int] | None) -> float:
    """Surer is better; with a recent sighting, nearer it is better, and the same id a little better."""
    conf = 0.5 if b.confidence is None else b.confidence
    if last is None:
        return conf
    cx, cy = _centre(b, width)
    return conf - 2.0 * math.hypot(cx - last[0], cy - last[1]) + (0.15 if b.object_id == last[3] else 0.0)


DEFAULT_FILTER = BallFilter()


def pick_ball(seg: Segmentation, f: BallFilter = DEFAULT_FILTER) -> Track:
    """One ball per frame: with two candidates, the one nearer where the ball was, and the surer."""
    n = _frames(seg)
    fps = seg.fps or 10.0
    good = [b for b in seg.boxes if usable(b, seg.width, seg.height, f) and 0 <= b.frame < n]
    still = static_ids(good, seg.width, fps, f)
    per_frame: dict[int, list[Box]] = defaultdict(list)
    for b in good:
        if b.object_id not in still:
            per_frame[b.frame].append(b)
    x = np.full(n, np.nan)
    y = np.full(n, np.nan)
    last: tuple[float, float, int, int] | None = None  # x, y, frame, id
    for k in range(n):
        candidates = per_frame.get(k)
        if not candidates:
            continue
        recent = last is not None and (k - last[2]) / fps <= 1.0
        best = max(candidates, key=lambda b, near=(last if recent else None): _candidate_score(b, seg.width, near))
        cx, cy = _centre(best, seg.width)
        if recent:
            speed = math.hypot(cx - last[0], cy - last[1]) / ((k - last[2]) / fps)
            if speed > f.max_jump and (best.confidence or 0.5) < 0.6:
                continue  # an unlikely jump from a weak box: wait for the next frame
        x[k], y[k] = cx, cy
        last = (cx, cy, k, best.object_id)
    return Track(x=x, y=y, found=~np.isnan(x), static_ids=still)


def players_x(seg: Segmentation | None, n: int, *, min_height: float = 0.10,
              min_confidence: float = 0.3) -> np.ndarray:
    """The players' middle (median centre of the people big enough to be on the court) per frame."""
    out = np.full(n, np.nan)
    if seg is None or not seg.boxes or seg.width <= 0 or seg.height <= 0:
        return out
    per_frame: dict[int, list[float]] = defaultdict(list)
    for b in seg.boxes:
        if not 0 <= b.frame < n:
            continue
        if (b.bottom - b.top) / seg.height < min_height:
            continue
        if b.confidence is not None and b.confidence < min_confidence:
            continue
        per_frame[b.frame].append((b.left + b.right) / 2 / seg.width)
    for k, xs in per_frame.items():
        out[k] = float(np.median(xs))
    return out


def main_person(seg: Segmentation | None, n: int) -> Track:
    """The person on screen most, and biggest: the one a talk clip follows."""
    x = np.full(n, np.nan)
    y = np.full(n, np.nan)
    if seg is None or not seg.boxes or seg.width <= 0:
        return Track(x=x, y=y, found=np.zeros(n, dtype=bool), static_ids=set())
    weight: dict[int, float] = defaultdict(float)
    for b in seg.boxes:
        if b.confidence is None or b.confidence >= 0.3:
            weight[b.object_id] += max(0.0, (b.right - b.left) * (b.bottom - b.top))
    if not weight:
        return Track(x=x, y=y, found=np.zeros(n, dtype=bool), static_ids=set())
    chosen = max(weight, key=lambda oid: weight[oid])
    area = np.zeros(n)
    for b in seg.boxes:
        if b.object_id != chosen or not 0 <= b.frame < n:
            continue
        a = (b.right - b.left) * (b.bottom - b.top)
        if a > area[b.frame]:
            area[b.frame] = a
            x[b.frame] = (b.left + b.right) / 2 / seg.width
            y[b.frame] = (b.top + b.bottom) / 2 / seg.width
    return Track(x=x, y=y, found=~np.isnan(x), static_ids=set())


def shot_index(times: np.ndarray, cuts: list[float]) -> np.ndarray:
    """Which shot (0, 1, ...) each time falls in, given the cut times."""
    return np.searchsorted(np.asarray(sorted(cuts), dtype=float), times + 1e-6, side="right")


def _spans(labels: np.ndarray) -> list[tuple[int, int]]:
    if not len(labels):
        return []
    edges = np.nonzero(np.diff(labels))[0] + 1
    starts = np.concatenate([[0], edges])
    ends = np.concatenate([edges, [len(labels)]])
    return [(int(a), int(b)) for a, b in zip(starts, ends, strict=True)]


def fill_gaps(track: Track, shots: np.ndarray, fps: float, players: np.ndarray | None = None,
              max_gap_s: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """A target for every proxy frame: the ball, short gaps interpolated, long
    ones from the players (or held), never across a cut. NaN where a whole
    shot has nothing to follow."""
    n = len(track.x)
    target = np.full(n, np.nan)
    kind = np.zeros(n, dtype=np.int8)
    have_players = players is not None and bool(np.any(~np.isnan(players)))
    max_gap = int(round(max_gap_s * fps))
    for a, b in _spans(shots):
        seen = np.nonzero(track.found[a:b])[0] + a
        if not len(seen):
            if have_players:
                _fill_from(players, target, kind, a, b, PLAYERS)
            continue
        target[seen] = track.x[seen]
        kind[seen] = BALL
        for i0, i1 in zip(seen[:-1], seen[1:], strict=True):
            if i1 - i0 <= 1:
                continue
            if i1 - i0 - 1 <= max_gap:
                steps = np.arange(1, i1 - i0) / (i1 - i0)
                target[i0 + 1:i1] = track.x[i0] + (track.x[i1] - track.x[i0]) * steps
                kind[i0 + 1:i1] = INTERP
            else:
                _fill_long(target, kind, players if have_players else None, i0 + 1, i1, track.x[i0], track.x[i1])
        _fill_long(target, kind, players if have_players else None, a, seen[0], track.x[seen[0]],
                   track.x[seen[0]])
        _fill_long(target, kind, players if have_players else None, seen[-1] + 1, b, track.x[seen[-1]],
                   track.x[seen[-1]])
    return target, kind


def _fill_from(values: np.ndarray, target: np.ndarray, kind: np.ndarray, a: int, b: int, code: int) -> None:
    """Copy values[a:b] into target, holding the nearest known value across its holes."""
    part = values[a:b]
    known = np.nonzero(~np.isnan(part))[0]
    if not len(known):
        return
    idx = np.arange(b - a)
    target[a:b] = np.interp(idx, known, part[known])
    kind[a:b] = code


def _fill_long(target, kind, players, a: int, b: int, before: float, after: float) -> None:
    if b <= a:
        return
    if players is not None and np.any(~np.isnan(players[a:b])):
        _fill_from(players, target, kind, a, b, PLAYERS)
        return
    # Nothing to follow: hold where the ball was last seen (or will be first seen).
    target[a:b] = before if not math.isnan(before) else after
    kind[a:b] = HOLD


def _moving_average(values: np.ndarray, k: int) -> np.ndarray:
    if k <= 1 or len(values) < 2:
        return values.copy()
    k = min(k, len(values))
    kernel = np.ones(k)
    return np.convolve(values, kernel, mode="same") / np.convolve(np.ones(len(values)), kernel, mode="same")


def steer(target: np.ndarray, shots: np.ndarray, window: float, fps: float, cam: Camera = BALL_CAMERA) -> np.ndarray:
    """The window's centre on each output frame, chasing `target` (centres, shares of the frame
    width) with a dead zone and capped speed and acceleration. Each shot starts fresh."""
    n = len(target)
    half = window / 2
    lo, hi = half, 1.0 - half
    centre = np.full(n, 0.5)
    if n == 0 or window >= 1.0:
        return centre
    dt = 1.0 / fps
    a_step = cam.max_accel * dt
    for a, b in _spans(shots):
        goal = target[a:b].copy()
        if np.all(np.isnan(goal)):
            centre[a:b] = 0.5
            continue
        idx = np.arange(b - a)
        known = ~np.isnan(goal)
        goal = np.interp(idx, idx[known], goal[known])
        goal = _moving_average(goal, int(round(cam.smooth_s * fps)) | 1)
        if cam.lead_s > 0 and len(goal) > 1:
            lead = np.clip(np.gradient(goal) * fps * cam.lead_s, -cam.max_lead, cam.max_lead)
            goal = goal + lead
        goal = np.clip(goal, lo, hi)
        c = float(np.clip(np.median(goal[: max(1, int(fps * 0.5))]), lo, hi))
        v = 0.0
        for i in range(b - a):
            if i:
                err = goal[i] - c
                excess = abs(err) - cam.dead_zone
                if excess <= 0:
                    want = 0.0
                else:
                    brake = math.sqrt(2 * cam.max_accel * excess)  # still able to stop in time
                    want = math.copysign(min(cam.gain * excess, cam.max_speed, brake), err)
                v_new = v + max(-a_step, min(a_step, want - v))
                c_new = min(hi, max(lo, c + v_new * dt))
                v = (c_new - c) / dt
                c = c_new
            centre[a + i] = c
    return centre


def plan(*, proxy_frames: int, proxy_fps: float, cuts: list[float], clip_start: float, clip_end: float,
         window: float, ball: Segmentation | None = None, people: Segmentation | None = None,
         mode: str = "basketball", out_fps: float = 30.0, f: BallFilter = DEFAULT_FILTER) -> CropPlan:
    """The crop path for [clip_start, clip_end] (seconds into the proxy), one value per output frame.

    `cuts` are seconds into the proxy. `ball` is SAM's answer for the ball
    noun, `people` for "person" (the players, or the speaker in talk mode).
    """
    n_out = max(1, int(round((clip_end - clip_start) * out_fps)))
    n = max(1, proxy_frames)
    fps = proxy_fps or 10.0
    if mode == "talk":
        track = main_person(people, n)
        players = None
        cam = SPEAKER_CAMERA
    else:
        track = pick_ball(ball, f) if ball is not None else Track(np.full(n, np.nan), np.full(n, np.nan),
                                                                    np.zeros(n, dtype=bool), set())
        players = players_x(people, len(track.x)) if people is not None else None
        cam = BALL_CAMERA
    m = len(track.x)
    proxy_shots = shot_index(np.arange(m) / fps, cuts)
    target, kind = fill_gaps(track, proxy_shots, fps, players, f.max_gap_s)

    out_t = clip_start + np.arange(n_out) / out_fps
    out_shots = shot_index(out_t, cuts)
    pos = np.clip(out_t * fps, 0, m - 1)
    k0 = np.floor(pos).astype(int)
    k1 = np.minimum(k0 + 1, m - 1)
    w = pos - k0
    same = (proxy_shots[k0] == proxy_shots[k1]) & ~np.isnan(target[k0]) & ~np.isnan(target[k1])
    pick = np.where(proxy_shots[k1] == out_shots, k1, k0)
    out_target = np.where(same, target[k0] * (1 - w) + target[k1] * w, target[pick])
    centre = steer(out_target, out_shots, window, out_fps, cam)
    left = np.clip(centre - window / 2, 0.0, max(0.0, 1.0 - window))

    first, last = int(math.floor(clip_start * fps)), int(math.ceil(clip_end * fps))
    first, last = max(0, min(first, m)), max(0, min(last, m))
    span = max(1, last - first)
    coverage = float(np.count_nonzero(track.found[first:last])) / span if last > first else 0.0
    used_players = bool(np.any(kind[first:last] == PLAYERS))
    pct = int(round(coverage * 100))
    if mode == "talk":
        if coverage > 0:
            source, note = "speaker", f"Followed the speaker {pct}% of the time"
        else:
            source, note = "centre", "Centre crop: SAM 3.1 found no one to follow"
    elif coverage >= 0.6 or (coverage > 0 and not used_players):
        source, note = "ball", f"Followed the ball {pct}% of the time"
    elif coverage > 0:
        source, note = "blend", f"Followed the ball {pct}% of the time and the players the rest"
    elif used_players:
        source, note = "players", "Followed the players: SAM 3.1 didn't find the ball"
    else:
        source, note = "centre", "Centre crop: SAM 3.1 didn't find the ball"
    return CropPlan(left=left, window=window, fps=out_fps, source=source, coverage=round(coverage, 3),
                    frames=span, note=note, target=out_target)


def centre_plan(clip_start: float, clip_end: float, window: float, note: str, out_fps: float = 30.0) -> CropPlan:
    n_out = max(1, int(round((clip_end - clip_start) * out_fps)))
    left = np.full(n_out, max(0.0, (1.0 - window) / 2))
    return CropPlan(left=left, window=window, fps=out_fps, source="centre", coverage=0.0, frames=0, note=note)


def ball_share(track: Track, fps: float, start_s: float, end_s: float) -> float | None:
    """Share of the frames in [start_s, end_s) (seconds into the proxy) with the ball; None if too few frames."""
    a, b = max(0, int(math.ceil(start_s * fps))), min(len(track.found), int(math.floor(end_s * fps)))
    if b - a < 5:
        return None
    return float(np.count_nonzero(track.found[a:b])) / (b - a)
