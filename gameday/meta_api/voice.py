"""Muse Voice: a long WAV file in pieces, and the timed turns that come back.

Muse Voice takes at most about ten minutes of audio a request, so a game's
soundtrack goes in pieces of PIECE_S seconds. Each piece is read from the
WAV file only when its request is made, so a long game is never held in
memory at once.
"""

from __future__ import annotations

import io
import re
import wave
from dataclasses import dataclass
from pathlib import Path

from gameday.types import MODEL_VOICE, MetaError, Turn

PIECE_S = 544.0  # 16 kHz mono 16-bit: about 17 MB, inside the request limits


@dataclass(frozen=True)
class WavPiece:
    index: int
    offset_s: float  # where the piece starts in the whole file
    seconds: float
    start_frame: int
    frames: int


def plan_pieces(path: Path, piece_s: float = PIECE_S) -> list[WavPiece]:
    """The pieces a WAV file is sent in, in order."""
    try:
        with wave.open(str(path), "rb") as wav:
            rate = wav.getframerate()
            total = wav.getnframes()
    except (OSError, EOFError, wave.Error) as exc:
        raise MetaError(f"The audio for Muse Voice isn't a WAV file it can read ({exc})",
                        model=MODEL_VOICE) from None
    if rate <= 0 or total <= 0:
        return []
    step = max(1, int(round(piece_s * rate)))
    pieces = []
    for index, start in enumerate(range(0, total, step)):
        frames = min(step, total - start)
        pieces.append(WavPiece(index=index, offset_s=start / rate, seconds=frames / rate,
                               start_frame=start, frames=frames))
    return pieces


def read_piece(path: Path, piece: WavPiece) -> bytes:
    """One piece as a WAV file of its own (same rate, channels and sample size)."""
    with wave.open(str(path), "rb") as source:
        params = source.getparams()
        source.setpos(piece.start_frame)
        data = source.readframes(piece.frames)
    out = io.BytesIO()
    with wave.open(out, "wb") as target:
        target.setnchannels(params.nchannels)
        target.setsampwidth(params.sampwidth)
        target.setframerate(params.framerate)
        target.writeframes(data)
    return out.getvalue()


def request_fields(language: str = "") -> dict:
    """The JSON part of a transcription request. ENDPOINTING is the mode that
    splits the audio into timed turns."""
    fields: dict = {"model": MODEL_VOICE, "audioEncoding": "WAV", "mode": "ENDPOINTING"}
    if language:
        fields["languageBias"] = [language]
    return fields


def _seconds(value) -> float | None:
    try:
        number = float(value) / 1000.0
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN check


def parse_turns(answer: dict, offset_s: float) -> list[Turn]:
    """The timed turns in one piece's answer, moved to the whole file's time.

    Text that comes without a time is left out: a made-up time would put
    words where they weren't said.
    """
    turns: list[Turn] = []
    items = answer.get("turns") if isinstance(answer, dict) else None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        text = re.sub(r"\s+", " ", str(item.get("transcript") or item.get("text") or "")).strip()
        start = _seconds(item.get("startMs"))
        end = _seconds(item.get("endMs"))
        if not text or start is None or end is None or end <= start:
            continue
        speaker = item.get("speaker") or item.get("speakerId") or ""
        turns.append(Turn(start_s=round(start + offset_s, 3), end_s=round(end + offset_s, 3), text=text,
                          speaker=str(speaker)))
    return turns
