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


def probe(path: Path, *, timeout: float = 60) -> MediaInfo:
    """What a video file holds. Raises MediaError if it has no video stream."""
    cmd = [FFPROBE, "-v", "error", "-show_entries",
           "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,sample_rate",
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
    return MediaInfo(
        duration_s=duration,
        width=int(video["width"]),
        height=int(video.get("height") or 0),
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
