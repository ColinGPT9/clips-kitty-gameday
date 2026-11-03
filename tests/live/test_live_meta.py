"""Meta's real API, a few cents' worth. Run on purpose only:

    META_API_KEY=... python -m pytest -m live tests/live
"""

from __future__ import annotations

import math
import os
import shutil
import struct
import subprocess
import wave

import pytest

from gameday import media
from gameday.meta_api import MetaClient
from gameday.meta_api.ledger import ConsoleLedger

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not os.environ.get("META_API_KEY"), reason="needs META_API_KEY")]


@pytest.fixture(scope="module")
def client():
    ledger = ConsoleLedger(0.25)
    yield MetaClient(os.environ["META_API_KEY"], ledger=ledger)
    print(f"live tests spent about ${ledger.spent_usd:.4f}")


def test_the_key_works(client):
    client.test_key()


def test_muse_spark_answers_in_the_schema(client):
    schema = {"type": "object", "properties": {"sport": {"type": "string"}, "players": {"type": "integer"}},
              "required": ["sport", "players"], "additionalProperties": False}
    got = client.chat_json([{"role": "user", "content": "How many players does a basketball team have on the court?"}],
                           schema, name="check", purpose="test", max_tokens=600)
    assert got["players"] == 5


def test_muse_voice_takes_a_wav(client, tmp_path):
    path = tmp_path / "tone.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(3000 * math.sin(i / 8))) for i in range(16000 * 3)))
    assert isinstance(client.transcribe(path), list)


@pytest.mark.media
@pytest.mark.skipif(shutil.which(media.FFMPEG) is None, reason="needs ffmpeg")
def test_sam_streams_a_complete_answer(client, tmp_path):
    clip = tmp_path / "ball.mp4"
    subprocess.run([media.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=0x1d6b2f:s=320x240:r=5,drawbox=x='40+t*80':y=100:w=24:h=24:color=orange:t=fill",
                    "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)], check=True)
    seg = client.segment_video(clip, "orange square")
    assert seg.frames == 10 and seg.width == 320
