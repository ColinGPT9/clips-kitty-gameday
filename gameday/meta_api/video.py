"""What SAM 3.1 is sent: the size, rate and frame count of a short MP4."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from gameday import media


@dataclass(frozen=True)
class VideoFacts:
    width: int
    height: int
    fps: float
    frames: int


def video_facts(path: Path, *, timeout: float = 60) -> VideoFacts:
    """Width, height, frame rate and the number of frames, counted from the
    container's packets (quick: nothing is decoded)."""
    cmd = [media.FFPROBE, "-v", "error", "-select_streams", "v:0", "-count_packets",
           "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate,nb_read_packets",
           "-of", "json", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True).stdout
        stream = (json.loads(out or "{}").get("streams") or [{}])[0]
        width, height = int(stream["width"]), int(stream["height"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise media.MediaError(f"ffprobe couldn't read {Path(path).name}") from exc
    fps = media._rate(stream.get("avg_frame_rate")) or media._rate(stream.get("r_frame_rate"))
    try:
        frames = int(stream.get("nb_read_packets") or 0)
    except (TypeError, ValueError):
        frames = 0
    return VideoFacts(width=width, height=height, fps=fps, frames=frames)
