"""The ffmpeg jobs the engine runs: the soundtrack, SAM proxies, scene cuts and stills.

gameday.media.run_ffmpeg keeps ffmpeg quiet; scene detection needs its info
output, and the render reads a command file by a relative name, so this
module runs ffmpeg itself (same program, same settings).
"""

from __future__ import annotations

import functools
import re
import subprocess
from pathlib import Path

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


def scene_cuts(video: Path, *, threshold: float = 0.35, timeout: float | None = 300) -> list[float]:
    """Seconds into `video` where the picture changes completely (a broadcast cut)."""
    err = run(["-i", str(video), "-an", "-vf", f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
              level="info", timeout=timeout)
    times = sorted({round(float(m.group(1)), 3) for m in _SHOWINFO_TIME.finditer(err)})
    return [t for t in times if t > 0]


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
