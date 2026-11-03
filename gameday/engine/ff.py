"""The ffmpeg jobs the engine runs: the soundtrack, SAM proxies, tiny frames for
finding the cuts, and stills.

gameday.media.run_ffmpeg keeps ffmpeg quiet; the tiny frames need its info
output for their times, and the render reads a command file by a relative
name, so this module runs ffmpeg itself (same program, same settings).
"""

from __future__ import annotations

import functools
import re
import subprocess
from pathlib import Path

import numpy as np

from gameday import media
from gameday.media import MediaError

PROXY_WIDTH = 640

#: In an MPEG transport or program stream (.ts, .mts, .m2ts, .mpg), an input
#: seek can land past the keyframe it needs, and ffmpeg then drops every frame
#: up to the next keyframe: the span starts up to a GOP late. For those, the
#: input is sought this much earlier and trimmed to the exact time in the
#: filter graph.
SEEK_PAD_S = 10.0
LOOSE_SEEK_FORMATS = frozenset({"mpegts", "mpeg"})


@functools.lru_cache(maxsize=64)
def _format_names(path: str, mtime_ns: int) -> frozenset[str]:
    try:
        out = subprocess.run([media.FFPROBE, "-v", "error", "-show_entries", "format=format_name", "-of", "csv=p=0",
                              path], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    return frozenset(part.strip() for part in (out or "").split(",") if part.strip())


def seek_plan(source: Path, start: float) -> tuple[float, float]:
    """For a span of `source` from `start`: where to seek the input, and how
    many seconds to trim off after that seek, so the span starts on its frame."""
    if start <= 0:
        return 0.0, 0.0
    try:
        resolved = Path(source).resolve()
        names = _format_names(str(resolved), resolved.stat().st_mtime_ns)
    except OSError:
        return start, 0.0
    if not names & LOOSE_SEEK_FORMATS:
        return start, 0.0
    seek_at = max(0.0, start - SEEK_PAD_S)
    return seek_at, start - seek_at


def run(args: list[str], *, cwd: Path | None = None, timeout: float | None = None, level: str = "error") -> str:
    """Run ffmpeg with `args`; returns what it wrote to stderr. Raises MediaError when it fails."""
    cmd = [media.FFMPEG, "-hide_banner", "-nostdin", "-v", level, "-y", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              cwd=str(cwd) if cwd else None, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaError(f"ffmpeg didn't run: {exc}") from exc
    if proc.returncode != 0:
        raise MediaError("ffmpeg failed: " + (proc.stderr or "").strip()[-600:])
    return proc.stderr or ""


def extract_wav(source: Path, out: Path, *, timeout: float | None = None) -> Path:
    """The first audio stream as 16 kHz mono 16-bit WAV (what Muse Voice takes)."""
    run(["-i", str(source), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
         "-map_metadata", "-1", "-fflags", "+bitexact", str(out)], timeout=timeout)
    return out


def make_proxy(source: Path, out: Path, *, start: float, seconds: float, fps: float,
               width: int = PROXY_WIDTH, timeout: float | None = 600) -> Path:
    """A small H.264 copy of [start, start + seconds] for SAM 3.1: `fps` frames a
    second, at most `width` pixels wide, no sound."""
    seek_at, skip = seek_plan(source, start)
    args: list[str] = []
    if seek_at > 0:
        args += ["-ss", f"{seek_at:.3f}"]
    chain = f"fps={fps:g},scale='min({width},iw)':-2"
    if skip:
        chain = f"trim=start={skip:.3f},setpts=PTS-STARTPTS," + chain
    args += ["-i", str(source), "-t", f"{seconds:.3f}", "-map", "0:v:0", "-an", "-sn", "-dn",
             "-vf", chain, "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    run(args, timeout=timeout)
    return out


def cut_part(proxy: Path, out: Path, *, start: float, seconds: float, timeout: float | None = 300) -> Path:
    """[start, start + seconds] of a proxy, re-encoded alike (for SAM in parts)."""
    run(["-ss", f"{start:.3f}", "-i", str(proxy), "-t", f"{seconds:.3f}", "-an", "-c:v", "libx264", "-preset",
         "veryfast", "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)], timeout=timeout)
    return out


_SHOWINFO_TIME = re.compile(r"Parsed_showinfo.*?\bpts_time:\s*(-?[0-9.]+)")


def tiny_frames(source: Path, *, start: float, seconds: float, width: int = 64, height: int = 36,
                timeout: float | None = 600) -> tuple[np.ndarray, np.ndarray]:
    """Every frame of [start, start + seconds] of `source` as a tiny RGB image, with
    its time in the source: (frames shaped (n, height, width, 3), times). A source
    faster than 30 frames a second is looked at every other frame or so."""
    seek_at, skip = seek_plan(source, start)
    chain = [f"trim=start={skip:.3f},setpts=PTS-STARTPTS"] if skip else []
    chain += ["select='isnan(prev_selected_t)+gte(t-prev_selected_t,0.03)'",
              f"scale={width}:{height}:flags=area", "showinfo"]
    cmd = [media.FFMPEG, "-hide_banner", "-nostdin", "-v", "info"]
    if seek_at > 0:
        cmd += ["-ss", f"{seek_at:.3f}"]
    cmd += ["-i", str(source), "-t", f"{max(0.0, seconds):.3f}", "-map", "0:v:0", "-an", "-sn", "-dn",
            "-vf", ",".join(chain)]
    # Raw video out is constant-rate by default: a seek between two frames would repeat the first one,
    # and every frame after it would sit one frame behind its time. Passed through, they match one to one
    # (-fps_mode since ffmpeg 5.1, -vsync before).
    for rate in (["-fps_mode", "passthrough"], ["-vsync", "passthrough"]):
        try:
            proc = subprocess.run([*cmd, *rate, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True,
                                  timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise MediaError(f"ffmpeg didn't run: {exc}") from exc
        err = proc.stderr.decode("utf-8", "replace")
        if proc.returncode == 0 or rate[0].lstrip("-") not in err:
            break
    if proc.returncode != 0:
        raise MediaError("ffmpeg failed: " + err.strip()[-600:])
    size = width * height * 3
    times = [float(m.group(1)) for m in _SHOWINFO_TIME.finditer(err)]
    count = min(len(proc.stdout) // size, len(times))
    frames = np.frombuffer(proc.stdout, dtype=np.uint8, count=count * size).reshape(count, height, width, 3)
    return frames, np.asarray(times[:count], dtype=float) + start


def grab_frame(video: Path, at_s: float, out: Path, *, width: int | None = None,
               timeout: float | None = 120) -> Path:
    """One frame of `video` at `at_s` seconds as a JPEG."""
    args = ["-ss", f"{max(0.0, at_s):.3f}", "-i", str(video), "-frames:v", "1"]
    if width:
        args += ["-vf", f"scale='min({width},iw)':-2"]
    args += ["-q:v", "3", str(out)]
    run(args, timeout=timeout)
    if not out.exists():
        raise MediaError(f"ffmpeg made no frame at {at_s:.1f} s")
    return out
