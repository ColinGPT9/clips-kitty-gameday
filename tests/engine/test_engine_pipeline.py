"""The whole pipeline on a 40 s synthetic game, with a fake MetaAPI (no network, no key)."""

from __future__ import annotations

import math
import subprocess
import threading
import wave

import numpy as np
import pytest
from PIL import Image

from gameday import media
from gameday.engine import pipeline, shots
from gameday.meta_api.video import video_facts
from gameday.types import (
    MODEL_SAM,
    STAGES,
    Box,
    JobCancelled,
    MetaError,
    PipelineError,
    PipelineOptions,
    Segmentation,
    Turn,
)

pytestmark = [pytest.mark.media, pytest.mark.usefixtures("need_ffmpeg")]

RATE = 16000
ROARS = ((13.5, 17.5), (29.5, 33.5))


@pytest.fixture(scope="module")
def game(tmp_path_factory):
    """40 s of testsrc2 with commentary-level noise and two crowd roars."""
    folder = tmp_path_factory.mktemp("game")
    rng = np.random.default_rng(7)
    sound = rng.normal(0, 0.02, 40 * RATE)
    for a, b in ROARS:
        sound[int(a * RATE):int(b * RATE)] += rng.normal(0, 0.4, int((b - a) * RATE))
    wav = folder / "crowd.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(sound, -1, 1) * 32767).astype("<i2").tobytes())
    video = folder / "game.mp4"
    subprocess.run([media.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25",
                    "-i", str(wav), "-t", "40", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(video)], check=True)
    return video


TURNS = [Turn(2.0, 4.0, "Here we go, tip-off."), Turn(9.0, 11.0, "He pushes it up the floor."),
         Turn(14.2, 15.4, "And the dunk!"), Turn(26.0, 28.0, "Kicks it out to the corner."),
         Turn(30.3, 31.6, "Three pointer, good!")]

ANSWER = {"moments": [
    {"start_s": 8.0, "play_s": 14.0, "end_s": 17.0, "category": "dunks", "label": "Dunk on the break",
     "title": "Dunk on the break", "team": "", "player": "", "evidence": "And the dunk!", "score": 90},
    {"start_s": 25.0, "play_s": 30.0, "end_s": 33.0, "category": "threes", "label": "Corner three",
     "title": "Corner three is good", "team": "", "player": "", "evidence": "Three pointer, good!", "score": 70},
]}


class FakeMeta:
    def __init__(self, *, sam_fails=False, spark_fails=False, no_ball=False):
        self.sam_fails, self.spark_fails, self.no_ball = sam_fails, spark_fails, no_ball
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def transcribe(self, audio, *, job_id=None):
        with wave.open(str(audio)) as w:
            assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        self.calls.append("voice")
        return list(TURNS)

    def chat_json(self, messages, schema, *, name, purpose, job_id=None, max_tokens=2048, temperature=0.2):
        self.calls.append("spark")
        assert purpose == "moments" and "And the dunk!" in messages[1]["content"]
        if self.spark_fails:
            raise MetaError("Muse Spark answered 503", status=503)
        return ANSWER

    def chat_text(self, messages, *, purpose, job_id=None, max_tokens=600, temperature=0.4):
        raise AssertionError("not used by the engine")

    def segment_video(self, video, noun, *, job_id=None):
        with self.lock:
            self.calls.append(f"sam:{noun}")
        if self.sam_fails:
            raise MetaError("SAM 3.1 answered 500", status=500, model=MODEL_SAM)
        facts = video_facts(video)
        boxes = []
        for k in range(facts.frames):
            if noun == "basketball player":  # three players standing on the right
                boxes += [Box(frame=k, object_id=j, left=430 + 40 * j, top=120, right=460 + 40 * j, bottom=200,
                              confidence=0.8) for j in range(3)]
            elif not self.no_ball:
                x = facts.width * (0.5 + 0.3 * math.sin(k / 15))
                boxes.append(Box(frame=k, object_id=1, left=x - 6, top=150, right=x + 6, bottom=162,
                                 confidence=0.85))
        return Segmentation(noun=noun, width=facts.width, height=facts.height, frames=facts.frames, fps=facts.fps,
                            boxes=boxes)

    def test_key(self):
        return None


def run(game, work, meta, *, stop=lambda: False, **opts):
    seen = []
    options = PipelineOptions(**{"max_clips": 2, "min_clip_s": 8.0, "max_clip_s": 16.0, **opts})
    result = pipeline.run(9, game, work, options, meta, lambda stage, share, msg: seen.append((stage, share, msg)),
                          stop)
    return result, seen


def test_a_full_run_makes_vertical_clips_that_follow_the_ball(game, tmp_path):
    meta = FakeMeta()
    result, seen = run(game, tmp_path, meta)
    assert result.duration_s == pytest.approx(40.0, abs=0.2) and result.transcript == TURNS
    assert [c.index for c in result.clips] == [1, 2]
    first, second = result.clips
    assert first.title == "Dunk on the break" and first.category == "dunks" and first.score == 90
    assert first.start_s < 14.0 < first.end_s and second.start_s < 30.0 < second.end_s
    assert first.end_s <= second.start_s or second.end_s <= first.start_s
    assert 8.0 <= first.end_s - first.start_s <= 16.0
    assert "And the dunk!" in first.transcript and first.evidence == "And the dunk!"
    for clip in result.clips:
        info = media.probe(clip.path)
        assert (info.width, info.height, info.has_audio) == (1080, 1920, True)
        assert info.duration_s == pytest.approx(clip.end_s - clip.start_s, abs=0.2)
        assert clip.path.parent == tmp_path / "clips" and clip.path.name == f"clip_{clip.index:02d}.mp4"
        with Image.open(clip.thumb) as img:
            assert img.size == (720, 1280)
        assert clip.debug is not None and clip.debug.exists()
        assert clip.follow.source == "ball" and clip.follow.ball_coverage > 0.95
        assert clip.follow.note.startswith("Followed the ball")
    stages = [stage for stage, _, _ in seen]
    assert set(stages) <= set(STAGES) and "captions" not in stages
    order = list(dict.fromkeys(stages))
    assert order == ["voice", "spark", "sam", "render"]
    assert all(0.0 <= share <= 1.0 for _, share, _ in seen)
    assert result.warnings == [] and result.stats["fallbacks"] == []
    assert set(result.stats["seconds"]) >= {"voice", "spark", "sam", "render"}
    assert result.stats["cost_usd"]["sam-3.1"] > 0
    assert meta.calls[:2] == ["voice", "spark"] and meta.calls.count("sam:basketball") == 2
    assert not (tmp_path / "tmp").exists()


def test_without_a_key_it_falls_back_to_crowd_noise_and_a_centre_crop(game, tmp_path):
    result, _ = run(game, tmp_path, None)
    assert result.clips and result.clips[0].title == "Big moment 1"
    for roar_start, _ in ROARS:  # each roar is in a clip
        assert any(c.start_s < roar_start + 0.5 < c.end_s for c in result.clips)
    assert all(c.follow.source == "centre" for c in result.clips)
    assert "no_key" in result.stats["fallbacks"]
    assert any("No Meta key" in w for w in result.warnings)


def test_a_play_in_a_replay_makes_no_clip_and_the_run_says_why(game, tmp_path, monkeypatch):
    # A wipe at 25.0-25.6 into a replay and one at 33.0-33.6 out of it: the corner three's time falls in it.
    wipes = [25.0, 25.6, 33.0, 33.6]
    monkeypatch.setattr(shots, "scan", lambda source, start, end, **kw: [c for c in wipes if start <= c <= end])
    result, _ = run(game, tmp_path, FakeMeta(), title_cards=False)
    [clip] = result.clips
    assert clip.title == "Dunk on the break" and clip.index == 1 and clip.path.name == "clip_01.mp4"
    assert any(w.startswith("No clip of the play at 0:") and "replay" in w for w in result.warnings)


def test_meta_failures_become_warnings_not_errors(game, tmp_path):
    result, _ = run(game, tmp_path, FakeMeta(sam_fails=True, spark_fails=True), title_cards=False)
    assert result.clips and all(c.follow.source == "centre" for c in result.clips)
    assert any("Muse Spark failed" in w for w in result.warnings)
    assert any("SAM 3.1 failed" in w for w in result.warnings)
    assert "spark" in result.stats["fallbacks"]


def test_talk_mode_follows_the_speaker_with_one_sam_pass(game, tmp_path):
    meta = FakeMeta()
    result, _ = run(game, tmp_path, meta, mode="talk", max_clips=1, title_cards=False)
    [clip] = result.clips
    assert clip.follow.source == "speaker" and clip.follow.note.startswith("Followed the speaker")
    assert meta.calls.count("sam:person") == 1 and "sam:basketball" not in meta.calls


def test_without_the_ball_the_window_follows_the_players(game, tmp_path):
    meta = FakeMeta(no_ball=True)
    result, _ = run(game, tmp_path, meta, max_clips=1, title_cards=False)
    [clip] = result.clips
    assert clip.follow.source == "players" and clip.follow.ball_coverage == 0.0
    assert meta.calls.count("sam:basketball player") == 1
    assert result.stats["sam_frames"] > 0


def test_a_stop_request_cancels_and_cleans_up(game, tmp_path):
    asked = []

    def stop():
        asked.append(1)
        return len(asked) > 2

    with pytest.raises(JobCancelled):
        run(game, tmp_path, FakeMeta(), stop=stop)
    assert not (tmp_path / "tmp").exists()


def test_videos_that_cant_be_used_are_pipeline_errors(tmp_path):
    short = tmp_path / "short.mp4"
    subprocess.run([media.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
                    "-t", "5", "-c:v", "libx264", "-preset", "ultrafast", str(short)], check=True)
    with pytest.raises(PipelineError, match="at least 20 s"):
        run(short, tmp_path / "a", FakeMeta())
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video")
    with pytest.raises(PipelineError, match="can't be read"):
        run(junk, tmp_path / "b", FakeMeta())
