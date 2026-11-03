"""ffmpeg and ffprobe, the only programs the app runs."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")


class MediaError(Exception):
    """ffmpeg or ffprobe failed, or the file isn't a video they can read."""


@dataclass
class MediaInfo:
    duration_s: float
    width: int
    height: int
    fps: float
    video_codec: str
    has_audio: bool
    audio_rate: int = 0


def _rate(text: str) -> float:
    num, _, den = (text or "0/1").partition("/")
    try:
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _quarter_turned(stream: dict) -> bool:
    """Whether the stream is shown turned by 90 or 270 degrees (a phone held
    upright). ffmpeg turns such frames as it decodes them, so every frame the
    engine sees has the stored width and height swapped."""
    turns = [d.get("rotation") for d in stream.get("side_data_list") or [] if isinstance(d, dict)]
    turns.append((stream.get("tags") or {}).get("rotate"))
    for turn in turns:
        try:
            if int(round(float(turn))) % 180 == 90:
                return True
        except (TypeError, ValueError):
            continue
    return False


def probe(path: Path, *, timeout: float = 60) -> MediaInfo:
    """What a video file holds, as ffmpeg decodes it: width and height are
    those of the frames after any display rotation. Raises MediaError if it
    has no video stream."""
    cmd = [FFPROBE, "-v", "error", "-show_entries",
           "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,sample_rate"
           ":stream_side_data=rotation:stream_tags=rotate",
           "-of", "json", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True).stdout
        data = json.loads(out or "{}")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise MediaError(f"ffprobe couldn't read {Path(path).name}") from exc
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video or not video.get("width"):
        raise MediaError(f"{Path(path).name} has no video stream")
    try:
        duration = float((data.get("format") or {}).get("duration") or 0)
    except ValueError:
        duration = 0.0
    width, height = int(video["width"]), int(video.get("height") or 0)
    if _quarter_turned(video):
        width, height = height, width
    return MediaInfo(
        duration_s=duration,
        width=width,
        height=height,
        fps=_rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate")),
        video_codec=str(video.get("codec_name") or ""),
        has_audio=audio is not None,
        audio_rate=int((audio or {}).get("sample_rate") or 0),
    )


def run_ffmpeg(args: list[str], *, timeout: float | None = None) -> None:
    """Run ffmpeg with `args` (without the program name). Raises MediaError with
    the end of its error output when it fails."""
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-y", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaError(f"ffmpeg didn't run: {exc}") from exc
    if proc.returncode != 0:
        raise MediaError("ffmpeg failed: " + (proc.stderr or "").strip()[-600:])
