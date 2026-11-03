"""Picking the plays from canned Muse Spark answers, and the owner's clip-boundary rules."""

from __future__ import annotations

import numpy as np
import pytest

from gameday.engine import loudness, moments
from gameday.engine.moments import Moment, Sentence
from gameday.types import CATEGORIES, PipelineOptions, Turn

OPTS = PipelineOptions()
RATE = 16000


def moment(play=100.0, start=90.0, end=106.0, category="threes", score=80.0) -> Moment:
    return Moment(start_s=start, end_s=end, play_s=play, category=category, label="l", title="t", score=score)


def s(start, end, text="Words.") -> Sentence:
    return Sentence(start, end, text)


# --- Muse Spark's answer -------------------------------------------------------------

KNOWN = moments.known_words([Turn(95.0, 101.0, "Sam Rivers for three, got it! Harbor City ties it.")], "")


def test_the_schema_is_strict():
    schema = moments.moment_schema()
    item = schema["properties"]["moments"]["items"]
    assert schema["additionalProperties"] is False and schema["required"] == ["moments"]
    assert item["additionalProperties"] is False
    assert sorted(item["required"]) == sorted(item["properties"])
    assert item["properties"]["category"]["enum"] == list(CATEGORIES)


def test_moments_are_checked_and_cleaned():
    answer = {"moments": [
        {"start_s": 88, "play_s": 100, "end_s": 106, "category": "threes", "label": "Corner three to tie it",
         "title": "Sam Rivers ties it #hoops", "team": "Harbor City", "player": "Sam Rivers",
         "evidence": "Sam Rivers for three, got it!", "score": 91},
        {"start_s": -5, "play_s": 3, "end_s": 12, "category": "slam", "label": "", "title": "",
         "team": "", "player": "", "evidence": "", "score": 140},
        {"start_s": 50, "play_s": 55, "end_s": 50.5, "category": "dunks", "label": "x", "title": "x", "team": "",
         "player": "", "evidence": "", "score": 50},
        {"start_s": "soon", "play_s": 1, "end_s": 9, "category": "dunks", "label": "x", "title": "x", "team": "",
         "player": "", "evidence": "", "score": 50},
        {"start_s": 200, "play_s": 400, "end_s": 230, "category": "dunks", "label": "Ollie Brant slams it",
         "title": "Ollie Brant with the hammer", "team": "Lakeside", "player": "Ollie Brant", "evidence": "",
         "score": 77},
    ]}
    got = moments.parse_moments(answer, duration_s=300.0, known_text=KNOWN)
    assert len(got) == 3
    first, second, third = got
    assert (first.start_s, first.play_s, first.end_s) == (88, 100, 106)
    assert first.title == "Sam Rivers ties it" and first.team == "Harbor City" and first.player == "Sam Rivers"
    assert (second.start_s, second.category, second.score, second.title) == (0.0, "other", 100.0, "Highlight")
    # Names nobody said are dropped, and the title falls back to a plain one.
    assert third.title == "Huge dunk" and third.label == "Huge dunk" and third.team == "" and third.player == ""
    assert third.play_s == 230


def test_the_messages_carry_the_transcript_peaks_and_hint():
    turns = [Turn(10.0, 12.5, "He drives and scores."), Turn(40.2, 41.0, "Got it!")]
    opts = PipelineOptions(max_clips=3, hint="Harbor City")
    messages = moments.build_messages(turns, [loudness.Peak(41.5, 3.2)], duration_s=600.0, options=opts)
    assert messages[0]["role"] == "system" and "Never invent" in messages[0]["content"]
    user = messages[1]["content"]
    assert "Return up to 7 moments" in user and "Harbor City" in user and "41.5 (z 3.2)" in user
    assert "10.0-12.5 He drives and scores." in user
    talk = moments.build_messages(turns, [], duration_s=600.0, options=PipelineOptions(mode="talk"))
    assert "talk videos" in talk[0]["content"] and "none found" in talk[1]["content"]


def roar(at=30.0, seconds=90.0):
    rng = np.random.default_rng(3)
    samples = rng.normal(0, 0.03, int(seconds * RATE)).astype(np.float32)
    a, b = int(at * RATE), int((at + 4) * RATE)
    samples[a:b] += rng.normal(0, 0.5, b - a).astype(np.float32)
    return loudness.from_samples(np.clip(samples, -1, 1), RATE)


def test_the_play_snaps_to_where_the_roar_began():
    m = moment(play=33.0, start=20.0, end=40.0)
    far = moment(play=70.0, start=60.0, end=80.0)
    moments.snap_to_loudness([m, far], roar(at=30.0))
    assert 29.0 <= m.play_s <= 31.0
    assert far.play_s == 70.0


def test_overlaps_are_dropped_keeping_the_best():
    a = moment(play=100, start=90, end=106, score=60)
    b = moment(play=104, start=95, end=110, score=90)
    c = moment(play=200, start=190, end=206, score=70)
    d = moment(play=300, start=290, end=306, score=10)
    kept = moments.drop_overlaps([a, b, c, d], 2, 400.0, OPTS)
    assert kept == [b, c]


def test_rough_ranges_respect_the_length_limits():
    long = moment(play=100, start=10, end=160)
    start, end = moments.rough_range(long, 400.0, OPTS)
    assert end - start <= OPTS.max_clip_s and start >= 75.0
    short = moment(play=100, start=99, end=101)
    start, end = moments.rough_range(short, 400.0, OPTS)
    assert end - start >= OPTS.min_clip_s and start <= 96.0


def test_without_spark_the_loudest_moments_get_plain_titles():
    picks = moments.fallback_moments(roar(at=30.0), duration_s=90.0, options=PipelineOptions(max_clips=3),
                                     turns=[Turn(29.0, 31.0, "What a play!")])
    assert picks and picks[0].title == "Big moment 1" and picks[0].category == "big_moment"
    assert picks[0].source == "loudness" and 25.0 <= picks[0].play_s <= 31.0
    assert picks[0].evidence == "What a play!"
    assert [p.title for p in picks] == [f"Big moment {i}" for i in range(1, len(picks) + 1)]
    silent = moments.fallback_moments(loudness.empty(), duration_s=100.0, options=PipelineOptions(max_clips=2))
    assert len(silent) == 2


# --- the owner's clip rules ------------------------------------------------------------

def test_the_clip_ends_at_the_broadcast_cut_after_the_call():
    sents = [s(97.0, 98.5, "He pulls up."), s(100.5, 102.5, "Bang, he hits the three!"), s(103.0, 104.0),
             s(108.0, 110.0, "Timeout on the floor.")]
    start, end = moments.refine(moment(), cuts=[104.6, 112.0], sents=sents, duration_s=400.0, options=OPTS)
    assert (start, end) == (90.0, 104.6)


def test_it_stops_before_a_new_sentence_that_starts_too_long_after_the_play():
    sents = [s(100.3, 101.0, "Dunk!"), s(104.0, 107.0, "That is his third tonight."),
             s(107.2, 108.5, "Timeout called.")]
    _, end = moments.refine(moment(category="dunks"), cuts=[], sents=sents, duration_s=400.0, options=OPTS)
    assert end == pytest.approx(107.05)


def test_after_a_block_or_steal_the_talk_allowance_is_shorter():
    sents = [s(100.2, 101.0, "Rejected!"), s(103.0, 105.0, "What a block that was.")]
    _, block_end = moments.refine(moment(category="blocks"), cuts=[], sents=sents, duration_s=400.0, options=OPTS)
    _, dunk_end = moments.refine(moment(category="dunks"), cuts=[], sents=sents, duration_s=400.0, options=OPTS)
    assert block_end == pytest.approx(102.0)
    assert dunk_end == pytest.approx(105.4)


def test_there_is_no_end_pad_past_a_cut():
    sents = [s(100.5, 101.5, "Three!")]
    _, end = moments.refine(moment(), cuts=[101.8], sents=sents, duration_s=400.0, options=OPTS)
    assert end == pytest.approx(101.8)


def test_the_next_scene_never_hangs_over_the_end():
    sents = [s(100.5, 101.5, "Three!"), s(102.0, 104.0, "And he has been hot all night.")]
    _, end = moments.refine(moment(), cuts=[103.5], sents=sents, duration_s=400.0, options=OPTS)
    assert end == pytest.approx(103.5)


def test_it_never_opens_on_a_cutaway_or_a_flash_of_the_shot_before():
    def cutaway(a, b):
        return (a, b) == (86.0, 89.0)

    start, _ = moments.refine(moment(start=85.0), cuts=[86.0, 89.0], sents=[s(100.5, 101.5, "Yes!")],
                              duration_s=400.0, options=OPTS, cutaway=cutaway)
    assert start == 89.0
    start, _ = moments.refine(moment(start=85.0), cuts=[86.0, 89.0], sents=[s(100.5, 101.5, "Yes!")],
                              duration_s=400.0, options=OPTS)
    assert start == 86.0  # without SAM it only skips the flash


def test_it_starts_a_few_seconds_before_the_call():
    sents = [s(99.0, 101.0, "Here he comes, and the finish!")]
    start, end = moments.refine(moment(start=99.5), cuts=[], sents=sents, duration_s=400.0, options=OPTS)
    assert start <= 99.0 - moments.LEAD_BEFORE_CALL_S
    assert end - start >= OPTS.min_clip_s


def test_lengths_stay_between_the_limits():
    sents = [s(100.5, 101.5, "Three!")]
    opts = PipelineOptions(max_clip_s=20.0, min_clip_s=12.0)
    start, end = moments.refine(moment(start=60.0), cuts=[], sents=sents, duration_s=400.0, options=opts)
    assert end - start == pytest.approx(20.0)
    start, end = moments.refine(moment(start=99.0), cuts=[], sents=sents, duration_s=400.0, options=opts)
    assert end - start == pytest.approx(12.0) and end == pytest.approx(102.0)
    start, end = moments.refine(moment(play=3.0, start=0.0), cuts=[], sents=[], duration_s=400.0, options=opts)
    assert start == 0.0 and end - start == pytest.approx(12.0)


def test_a_talk_clip_starts_and_ends_on_sentences():
    sents = [s(10.0, 14.0, "First."), s(14.2, 20.0, "Second point."), s(20.5, 26.0, "Third."),
             s(26.4, 40.0, "Fourth.")]
    m = moment(play=16.0, start=14.5, end=24.0, category="highlight")
    start, end = moments.refine(m, cuts=[], sents=sents, duration_s=100.0, options=PipelineOptions(mode="talk"))
    assert start == pytest.approx(14.0) and end == pytest.approx(26.3)


def test_sentences_share_a_turns_time_by_length():
    parts = moments.sentences([Turn(10.0, 20.0, "Short one. A much longer second sentence here!")])
    assert [p.text for p in parts] == ["Short one.", "A much longer second sentence here!"]
    assert parts[0].start_s == 10.0 and parts[1].end_s == 20.0
    assert parts[0].end_s == parts[1].start_s and 12.0 < parts[0].end_s < 13.5
