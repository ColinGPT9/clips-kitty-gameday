"""Rendering clips from synthetic ffmpeg videos (testsrc, colour fields and a sine tone)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from gameday import media
from gameday.engine import render

pytestmark = [pytest.mark.media, pytest.mark.usefixtures("need_ffmpeg")]


def make_video(path, source: str, seconds: float, *, audio: bool = True):
    cmd = [media.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", source]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100"]
    cmd += ["-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac"] if audio else []
    subprocess.run([*cmd, str(path)], check=True)
    return path


def pixel(video, at, xy=(540, 1500)):
    out = video.with_name(f"{video.stem}_{at}.png")
    subprocess.run([media.FFMPEG, "-v", "error", "-y", "-ss", str(at), "-i", str(video), "-frames:v", "1", str(out)],
                   check=True)
    with Image.open(out) as img:
        return np.asarray(img.convert("RGB"))[xy[1], xy[0]]


def test_the_window_pans_on_time_and_the_clip_is_vertical(tmp_path):
    halves = "color=c=red:s=1280x720:r=30,drawbox=x=640:y=0:w=640:h=720:color=blue:t=fill"
    src = make_video(tmp_path / "halves.mp4", halves, 12)
    info = media.probe(src)
    left = np.zeros(180)
    left[90:] = 1.0  # jump to the right edge 3 s into the clip
    out = render.render(src, info, 3.0, 9.0, left, tmp_path / "clip_01.mp4", tmp_path, title="Big three",
                        category="threes")
    got = media.probe(out)
    assert (got.width, got.height, got.video_codec, got.has_audio, got.audio_rate) == (1080, 1920, "h264", True,
                                                                                        48000)
    assert got.duration_s == pytest.approx(6.0, abs=0.15) and got.fps == pytest.approx(30.0)
    red, blue = pixel(out, 2.8), pixel(out, 3.2)
    assert red[0] > 200 and red[2] < 60
    assert blue[2] > 200 and blue[0] < 60
    assert (tmp_path / "clip_01.cmd").read_text().splitlines() == ["0.0000 crop@pan x 0;", "3.0000 crop@pan x 876;"]


def test_the_title_card_shows_first_and_then_fades(tmp_path):
    src = make_video(tmp_path / "plain.mp4", "color=c=gray:s=1280x720:r=30", 6)
    out = render.render(src, media.probe(src), 0.0, 5.0, np.full(150, 0.3), tmp_path / "card.mp4", tmp_path,
                        title="A title that is fairly long to wrap onto two lines", category="dunks")
    band = pixel(out, 0.5, xy=(540, 330))
    later = pixel(out, 4.0, xy=(540, 330))
    assert band.sum() < later.sum() - 100  # the dark band is there at first, gone later


def test_a_relative_work_folder_still_renders_a_titled_panning_clip(tmp_path, monkeypatch):
    """The default DATA_DIR is relative; ffmpeg runs inside tmp, so the card must not be looked up twice."""
    src = make_video(tmp_path / "rel.mp4", "testsrc2=size=1280x720:rate=30", 4).resolve()
    monkeypatch.chdir(tmp_path)
    tmp = Path("data") / "media" / "jobs" / "1" / "tmp"
    tmp.mkdir(parents=True)
    left = np.zeros(90)
    left[45:] = 1.0
    out = render.render(src, media.probe(src), 0.0, 3.0, left, tmp.parent / "clip_01.mp4", tmp, title="Big three",
                        category="threes")
    assert out.is_absolute() and media.probe(out).width == 1080
    assert (tmp / "clip_01_card.png").is_file()


def test_the_default_data_dir_is_absolute(tmp_path, monkeypatch):
    from gameday.config import Config

    monkeypatch.chdir(tmp_path)
    assert Config.from_env({}).data_dir == (tmp_path / "data").resolve()


def test_a_silent_60fps_video_gets_30fps_and_a_silent_track(tmp_path):
    src = make_video(tmp_path / "fast.mp4", "testsrc2=size=1280x720:rate=60", 5, audio=False)
    out = render.render(src, media.probe(src), 1.0, 4.0, np.full(90, 0.5), tmp_path / "quiet.mp4", tmp_path)
    got = media.probe(out)
    assert got.fps == pytest.approx(30.0) and got.has_audio and got.audio_rate == 48000
    assert got.duration_s == pytest.approx(3.0, abs=0.15)


def test_a_portrait_video_is_centred_without_panning(tmp_path):
    src = make_video(tmp_path / "tall.mp4", "testsrc2=size=720x1440:rate=30", 3)
    assert render.window_px(720, 1440) == (720, 1280, 80, False)
    out = render.render(src, media.probe(src), 0.0, 2.0, np.full(60, 0.9), tmp_path / "tall_clip.mp4", tmp_path)
    assert (media.probe(out).width, media.probe(out).height) == (1080, 1920)
    assert not (tmp_path / "tall_clip.cmd").exists()


@pytest.mark.parametrize("turn, size", [(90, (360, 640)), (270, (360, 640)), (180, (640, 360))])
def test_a_phone_video_stored_sideways_is_probed_as_ffmpeg_decodes_it(tmp_path, turn, size):
    # Stored 640x360 and tagged to be shown turned, as phones record upright video.
    wide = make_video(tmp_path / "wide.mp4", "testsrc2=size=640x360:rate=30", 3)
    src = tmp_path / f"phone_{turn}.mp4"
    subprocess.run([media.FFMPEG, "-v", "error", "-y", "-display_rotation", str(turn), "-i", str(wide), "-c", "copy",
                    str(src)], check=True)
    info = media.probe(src)
    assert (info.width, info.height) == size
    left = np.zeros(60)
    left[30:] = 1.0
    out = render.render(src, info, 0.0, 2.0, left, tmp_path / f"phone_{turn}_clip.mp4", tmp_path)
    assert (media.probe(out).width, media.probe(out).height) == (1080, 1920)
    # Upright, the whole width fits the 9:16 frame: nothing to pan.
    assert (tmp_path / f"phone_{turn}_clip.cmd").exists() == (turn == 180)


def test_thumbnail_and_debug_image(tmp_path):
    src = make_video(tmp_path / "game.mp4", "testsrc2=size=1280x720:rate=30", 4)
    out = render.render(src, media.probe(src), 0.0, 3.0, np.full(90, 0.2), tmp_path / "clip_02.mp4", tmp_path)
    thumb = render.thumbnail(out, 1.5, tmp_path / "clip_02.jpg")
    with Image.open(thumb) as img:
        assert img.size == (720, 1280)
    debug = render.debug_image(src, 1.5, tmp_path / "clip_02_debug.jpg", tmp_path, window_left=0.2,
                               window=0.316, ball=(0.4, 0.4, 0.42, 0.44), players=[(0.1, 0.2, 0.15, 0.6)],
                               note="Followed the ball 80% of the time")
    with Image.open(debug) as img:
        rgb = np.asarray(img.convert("RGB"))
    assert img.size == (1280, 720)
    assert rgb[360, int(0.2 * 1280) + 1][1] > 150  # the green window's left edge


def test_window_sizes():
    assert render.window_px(1920, 1080) == (608, 1080, 0, True)
    assert render.window_px(640, 480) == (270, 480, 0, True)
    assert render.window_px(1080, 1920) == (1080, 1920, 0, False)
    assert render.output_fps(59.94) == pytest.approx(29.97) and render.output_fps(25) == 0.0
    assert render.output_fps(120) == 30.0
