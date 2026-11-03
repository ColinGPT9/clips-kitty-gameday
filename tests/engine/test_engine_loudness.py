"""Loudness peaks: crowd noise found with numpy alone."""

from __future__ import annotations

import wave

import numpy as np
import pytest

from gameday.engine import loudness

RATE = 16000


def game_audio(seconds=120.0, bursts=((30.0, 0.5), (70.0, 0.25), (74.0, 0.2)), seed=1):
    """Steady commentary-level noise with louder roars of (start, amplitude), 4 s each."""
    rng = np.random.default_rng(seed)
    samples = rng.normal(0, 0.03, int(seconds * RATE)).astype(np.float32)
    for start, amp in bursts:
        a, b = int(start * RATE), int((start + 4.0) * RATE)
        samples[a:b] += rng.normal(0, amp, b - a).astype(np.float32)
    return np.clip(samples, -1, 1)


def test_roars_are_found_loudest_first_and_kept_apart():
    loud = loudness.from_samples(game_audio(), RATE)
    assert len(loud.db) == 240 and loud.hop_s == 0.5
    found = loudness.peaks(loud, min_z=1.5, min_gap_s=15.0)
    assert 30.0 <= found[0].t <= 34.0 and 70.0 <= found[1].t <= 78.0
    assert len(found) == 2  # the roars at 70 s and 74 s are one moment
    assert found[0].z > found[1].z > 1.5


def test_the_onset_is_where_the_roar_began():
    loud = loudness.from_samples(game_audio(), RATE)
    peak = loudness.peaks(loud)[0]
    began = loudness.onset(loud, peak.t)
    assert 28.5 <= began <= 30.5


def test_silence_and_a_flat_level_have_no_peaks():
    assert loudness.peaks(loudness.from_samples(np.zeros(RATE * 30, dtype=np.float32), RATE)) == []
    assert loudness.peaks(loudness.empty()) == []
    flat = loudness.from_samples(np.full(RATE * 30, 0.1, dtype=np.float32), RATE)
    assert loudness.peaks(flat) == []


def test_a_wav_is_read_in_blocks_with_the_same_result(tmp_path):
    samples = game_audio(seconds=150.0)
    path = tmp_path / "game.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((samples * 32767).astype("<i2").tobytes())
    from_file = loudness.measure(path)
    direct = loudness.from_samples(samples, RATE)
    assert len(from_file.db) == len(direct.db) == 300
    assert np.allclose(from_file.db, direct.db, atol=0.05)


def test_nearest_peak_only_counts_a_real_rise():
    loud = loudness.from_samples(game_audio(), RATE)
    assert loudness.nearest_peak(loud, 29.0) == pytest.approx(loud.time(int(np.argmax(loud.z[56:68])) + 56))
    assert loudness.nearest_peak(loud, 100.0) is None
