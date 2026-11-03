"""Finding the broadcast's cuts: on synthetic tiny frames, and in a real file with ffmpeg."""

from __future__ import annotations

import subprocess

import numpy as np
import pytest

from gameday import media
from gameday.engine import shots

FPS = 30.0
H, W = 36, 64


def court(n: int, *, pan: float = 0.5, seed: int = 1) -> np.ndarray:
    """A wide shot: a light floor under dark stands, with players (dots) on it, panning `pan` px a frame."""
    rng = np.random.default_rng(seed)
    wide = np.zeros((H, W * 6, 3), dtype=np.float32)
    wide[:14] = rng.uniform(40, 90, (14, W * 6, 1)) * np.array([1.0, 1.0, 1.2])  # stands
    wide[14:] = np.array([220, 190, 150])  # floor
    for _ in range(60):  # players
        y, x = rng.integers(16, H - 3), rng.integers(0, W * 6 - 3)
        wide[y:y + 3, x:x + 2] = rng.choice([[30, 60, 200], [235, 235, 235]])
    return np.stack([wide[:, int(i * pan):int(i * pan) + W] for i in range(n)]).astype(np.uint8)


def close_up(n: int, *, seed: int = 2) -> np.ndarray:
    """A close-up: a yellow wall behind a big blue jersey that sways."""
    rng = np.random.default_rng(seed)
    out = np.zeros((n, H, W, 3), dtype=np.float32)
    out[:] = np.array([230, 200, 30])
    for i in range(n):
        x = 20 + int(3 * np.sin(i / 7))
        out[i, 6:, x:x + 22] = np.array([30, 60, 200])
        out[i] += rng.normal(0, 4, (H, W, 3))
    return np.clip(out, 0, 255).astype(np.uint8)


def logo(n: int) -> np.ndarray:
    frame = np.zeros((H, W, 3), dtype=np.uint8)
    frame[:] = (240, 240, 245)
    frame[8:28, 16:48] = (200, 30, 40)
    frame[12:24, 24:40] = (30, 50, 140)
    return np.repeat(frame[None], n, axis=0)


def times(n: int, start: float = 100.0) -> np.ndarray:
    return start + np.arange(n) / FPS


def test_a_hard_cut_is_found_on_the_first_frame_of_the_new_shot():
    frames = np.concatenate([court(60), close_up(45), court(45, seed=3)])
    t = times(len(frames))
    assert shots.find(frames, t) == [pytest.approx(t[60]), pytest.approx(t[105])]


def test_a_fast_pan_or_a_flash_is_not_a_cut():
    assert shots.find(court(90, pan=3.0), times(90)) == []  # the camera follows a fast break
    frames = court(90)
    frames[45] = 255  # a photographer's flash
    assert shots.find(frames, times(90)) == []


def test_a_wipe_counts_where_it_begins_and_where_it_ends():
    before, after = court(60), logo(30)
    blend = np.stack([(before[-1] * (1 - a) + after[0] * a).astype(np.uint8) for a in np.linspace(0, 1, 14)])
    frames = np.concatenate([before, blend, after])
    t = times(len(frames))
    cuts = shots.find(frames, t)
    assert len(cuts) >= 2
    assert t[60] - 0.3 <= cuts[0] <= t[60] + 0.05  # the clip ends before the logo shows
    assert cuts[-1] >= t[60 + 14] - 0.2  # and the next one starts after it


@pytest.mark.media
def test_the_cuts_are_found_in_the_game_itself(tmp_path, need_ffmpeg):
    video = tmp_path / "two_shots.mp4"
    subprocess.run([media.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:d=3",
                    "-f", "lavfi", "-i", "smptebars=size=320x180:rate=30:d=3", "-filter_complex",
                    "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(video)], check=True)
    assert shots.scan(video, 0.5, 5.5) == [pytest.approx(3.0, abs=0.01)]
    assert shots.scan(video, 3.5, 5.5) == []
