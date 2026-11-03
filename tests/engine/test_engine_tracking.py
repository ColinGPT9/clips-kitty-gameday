"""The 9:16 window's path: pan only, bounded speed and acceleration, jumps only at cuts."""

from __future__ import annotations

import numpy as np
import pytest

from gameday.engine import tracking
from gameday.types import Box, Segmentation

W, H, FPS = 640, 360, 10.0
WINDOW = tracking.window_share(1920, 1080)  # 0.316 of the frame width


def ball_seg(xs, *, conf=0.8, size=16, frames=None, extra=None, oid=1) -> Segmentation:
    """A ball at centre x = xs[k] (pixels; None: not seen) on proxy frame k."""
    boxes = []
    for k, x in enumerate(xs):
        if x is None:
            continue
        boxes.append(Box(frame=k, object_id=oid, left=x - size / 2, top=150, right=x + size / 2, bottom=150 + size,
                         confidence=conf))
    boxes += extra or []
    boxes.sort(key=lambda b: (b.frame, b.object_id))
    return Segmentation(noun="basketball", width=W, height=H, frames=frames or len(xs), fps=FPS, boxes=boxes)


def make_plan(seg, *, cuts=(), start=0.0, end=None, people=None, mode="basketball"):
    end = end if end is not None else seg.frames / FPS
    return tracking.plan(proxy_frames=seg.frames, proxy_fps=FPS, cuts=list(cuts), clip_start=start, clip_end=end,
                         window=WINDOW, ball=seg, people=people, mode=mode)


def speeds(left, fps=30.0):
    return np.diff(left) * fps


def test_the_window_is_one_size_and_stays_inside_the_frame():
    xs = [10 + 6 * k for k in range(100)]  # sweeps from the far left to the far right
    p = make_plan(ball_seg(xs))
    assert p.window == pytest.approx(WINDOW)
    assert len(p.left) == 300  # 10 s at 30 frames a second
    assert p.left.min() >= 0.0 and p.left.max() <= 1.0 - WINDOW + 1e-9
    assert p.left[0] == pytest.approx(0.0, abs=1e-6)  # the ball starts at the edge: clamped
    assert p.source == "ball" and p.coverage == 1.0 and p.note == "Followed the ball 100% of the time"


def test_speed_and_acceleration_are_capped_even_when_the_ball_jumps():
    xs = [100] * 30 + [560] * 70  # the ball "teleports" across the frame at 3 s
    p = make_plan(ball_seg(xs))
    v = speeds(p.left)
    a = np.diff(v) * 30.0
    cam = tracking.BALL_CAMERA
    assert np.abs(v).max() <= cam.max_speed + 1e-9
    assert np.abs(a).max() <= cam.max_accel + 1e-6
    # ... and it still gets there: the ball ends inside the window.
    centre = 560 / W
    assert p.left[-1] <= centre <= p.left[-1] + WINDOW


def test_the_dead_zone_keeps_a_jiggling_ball_from_moving_the_window():
    xs = [320 + (8 if k % 2 else -8) for k in range(60)]
    p = make_plan(ball_seg(xs))
    assert np.ptp(p.left) < 1e-9


def test_a_cut_is_never_smoothed_across():
    xs = [120] * 40 + [520] * 40  # a cut at 4.0 s to a shot where the ball is far right
    p = make_plan(ball_seg(xs), cuts=[4.0])
    i = 120  # the first output frame of the new shot
    v = speeds(p.left)
    assert abs(p.left[i] - p.left[i - 1]) > 0.3  # it jumps at the cut ...
    assert np.abs(np.delete(v, i - 1)).max() <= tracking.BALL_CAMERA.max_speed + 1e-9  # ... and only there
    assert p.left[i - 1] <= 120 / W <= p.left[i - 1] + WINDOW
    assert p.left[i] <= 520 / W <= p.left[i] + WINDOW
    # Without the cut, the same footage is a capped pan.
    smooth = make_plan(ball_seg(xs))
    assert np.abs(speeds(smooth.left)).max() <= tracking.BALL_CAMERA.max_speed + 1e-9


def test_a_logo_that_never_moves_is_ignored():
    logo = [Box(frame=k, object_id=9, left=600, top=20, right=616, bottom=36, confidence=0.95) for k in range(80)]
    xs = [200 + 2 * k for k in range(80)]
    track = tracking.pick_ball(ball_seg(xs, extra=logo))
    assert track.static_ids == {9}
    assert np.allclose(track.x * W, xs)


def test_with_two_balls_the_one_near_the_last_position_wins():
    xs = [300 + 3 * k for k in range(20)]
    decoy = [Box(frame=k, object_id=5, left=40 + 20 * k, top=40, right=56 + 20 * k, bottom=56, confidence=0.9)
             for k in range(10, 20)]
    track = tracking.pick_ball(ball_seg(xs, conf=0.7, extra=decoy))
    assert np.allclose(track.x[10:] * W, xs[10:])


def test_boxes_too_big_too_unsure_or_too_long_are_not_balls():
    bad = [
        Box(frame=0, object_id=1, left=0, top=0, right=300, bottom=300, confidence=0.9),  # a person-sized box
        Box(frame=1, object_id=1, left=100, top=100, right=116, bottom=116, confidence=0.1),  # unsure
        Box(frame=2, object_id=1, left=100, top=100, right=160, bottom=110, confidence=0.9),  # a long thin box
    ]
    seg = Segmentation(noun="basketball", width=W, height=H, frames=3, fps=FPS, boxes=bad)
    assert not tracking.pick_ball(seg).found.any()


def test_short_gaps_are_interpolated_and_long_ones_follow_the_players():
    xs = [200 + 2 * k for k in range(100)]
    for k in range(20, 26):  # 0.6 s gap
        xs[k] = None
    for k in range(45, 80):  # 3.5 s gap
        xs[k] = None
    people = Segmentation(noun="person", width=W, height=H, frames=100, fps=FPS, boxes=[
        Box(frame=k, object_id=40 + j, left=430 + 30 * j, top=100, right=470 + 30 * j, bottom=260, confidence=0.9)
        for k in range(100) for j in range(3)])
    track = tracking.pick_ball(ball_seg(xs))
    players = tracking.players_x(people, 100)
    target, kind = tracking.fill_gaps(track, np.zeros(100, dtype=int), FPS, players)
    assert np.all(kind[20:26] == tracking.INTERP)
    assert np.allclose(target[20:26] * W, [240 + 2 * i for i in range(6)])
    assert np.all(kind[45:80] == tracking.PLAYERS)
    assert np.allclose(target[45:80], 480 / W)
    p = make_plan(ball_seg(xs), people=people)
    assert p.source == "blend" and p.coverage == pytest.approx(0.59)
    assert "59%" in p.note


def test_no_ball_at_all_follows_the_players_or_the_centre():
    people = Segmentation(noun="person", width=W, height=H, frames=50, fps=FPS, boxes=[
        Box(frame=k, object_id=1, left=500, top=100, right=540, bottom=260, confidence=0.9) for k in range(50)])
    empty = ball_seg([None] * 50)
    assert make_plan(empty, people=people).source == "players"
    centre = make_plan(empty)
    assert centre.source == "centre" and np.allclose(centre.left, (1 - WINDOW) / 2)


def test_talk_mode_follows_the_main_person():
    people = Segmentation(noun="person", width=W, height=H, frames=60, fps=FPS, boxes=[
        *[Box(frame=k, object_id=1, left=60, top=60, right=200, bottom=360, confidence=0.9) for k in range(60)],
        *[Box(frame=k, object_id=2, left=500, top=200, right=540, bottom=300, confidence=0.9) for k in range(60)]])
    p = make_plan(ball_seg([None] * 60), people=people, mode="talk")
    assert p.source == "speaker" and p.coverage == 1.0
    assert p.left[-1] <= 130 / W <= p.left[-1] + WINDOW


def test_a_clip_inside_the_proxy_uses_its_own_frames():
    xs = [100] * 40 + [None] * 40 + [500] * 20
    p = make_plan(ball_seg(xs), start=2.0, end=6.0)
    assert len(p.left) == 120 and p.coverage == pytest.approx(0.5)


def test_window_share_and_portrait_video():
    assert tracking.window_share(1920, 1080) == pytest.approx(1080 * 9 / 16 / 1920)
    assert tracking.window_share(1080, 1920) == 1.0
