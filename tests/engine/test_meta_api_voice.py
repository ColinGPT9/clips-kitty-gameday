"""Muse Voice on a long WAV: pieces, offsets and the turns that come back."""

from __future__ import annotations

import json
import wave

import pytest

from gameday.meta_api import voice
from gameday.meta_api.client import MetaClient
from gameday.types import MetaError

KEY = "test-key-not-real-0123456789"


def silent_wav(path, seconds: float, rate: int = 16000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return path


def test_pieces_cover_the_file_in_order(tmp_path):
    pieces = voice.plan_pieces(silent_wav(tmp_path / "a.wav", 5.0), piece_s=2.0)
    assert [(p.offset_s, p.seconds) for p in pieces] == [(0.0, 2.0), (2.0, 2.0), (4.0, 1.0)]
    data = voice.read_piece(tmp_path / "a.wav", pieces[2])
    with wave.open(__import__("io").BytesIO(data)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getnframes()) == (16000, 1, 16000)


def test_each_piece_is_sent_and_its_turns_move_to_file_time(fakes, tmp_path):
    audio = silent_wav(tmp_path / "game.wav", 5.0)
    said = {
        "part000.wav": [{"startMs": 500, "endMs": 1500, "transcript": "Brings it up."}],
        "part001.wav": [{"startMs": 100, "endMs": 900, "transcript": "Pull up   for three!"},
                        {"startMs": 1200, "endMs": 1100, "transcript": "backwards"},
                        {"startMs": 1300, "transcript": "no end"}],
        "part002.wav": [{"startMs": 0, "endMs": 800, "text": "Got it."}],
    }

    def answer(url, kwargs):
        name = kwargs["files"]["audio"][0]
        return fakes.Response(200, {"transcript": "...", "turns": said[name]})

    session = fakes.Session([answer, answer, answer])
    ledger = fakes.Ledger()
    client = MetaClient(KEY, ledger=ledger, session=session)
    client.voice_piece_s = 2.0
    turns = client.transcribe(audio, job_id=5)
    assert [(t.start_s, t.end_s, t.text) for t in turns] == [
        (0.5, 1.5, "Brings it up."), (2.1, 2.9, "Pull up for three!"), (4.0, 4.8, "Got it.")]
    assert len(session.calls) == 3
    call = session.calls[0]
    assert call["url"] == "https://api.meta.ai/v1/asr/transcribe"
    _, fields, kind = call["files"]["request"]
    assert kind == "application/json"
    assert json.loads(fields) == {"model": "muse-voice-transcribe-1.0", "audioEncoding": "WAV", "mode": "ENDPOINTING"}
    assert call["files"]["audio"][2] == "audio/wav"
    seconds = sorted(c.units["audio_seconds"] for c in ledger.calls)
    assert seconds == [1.0, 2.0, 2.0]
    assert sum(c.cost_usd for c in ledger.calls) == pytest.approx(5.0 / 3600 * 0.18, abs=1e-6)
    assert all(c.purpose == "transcribe" and c.job_id == 5 for c in ledger.calls)


def test_one_failed_piece_fails_the_transcription(fakes, tmp_path):
    audio = silent_wav(tmp_path / "game.wav", 3.0)

    def answer(url, kwargs):
        if kwargs["files"]["audio"][0] == "part001.wav":
            return fakes.Response(400, {"error": {"message": "unsupported audio"}})
        return fakes.Response(200, {"turns": []})

    client = MetaClient(KEY, session=fakes.Session([answer, answer]))
    client.voice_piece_s = 2.0
    with pytest.raises(MetaError) as err:
        client.transcribe(audio)
    assert "unsupported audio" in str(err.value)


def test_a_file_that_isnt_wav_is_a_meta_error(tmp_path):
    bad = tmp_path / "x.wav"
    bad.write_bytes(b"not a wav")
    with pytest.raises(MetaError):
        MetaClient(KEY).transcribe(bad)
