"""Rendering one clip: cut it from the game, pan the 9:16 window along its path,
scale to 1080x1920, lay a title card over the first seconds, and make the
thumbnail and the debug image."""

from __future__ import annotations

import os
import textwrap
import threading
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from gameday.engine import ff
from gameday.media import MediaError, MediaInfo

OUT_W, OUT_H = 1080, 1920
TITLE_S = 2.5
MAX_FPS = 30.0
PATH_FPS = 30.0  # the crop path has one value per 1/30 s
_ONE_AT_A_TIME = threading.Lock()  # the server has two CPUs: one render at a time

FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/local/share/fonts/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
)

CATEGORY_WORDS = {
    "clutch": "CLUTCH", "dunks": "DUNK", "threes": "THREE", "blocks": "BLOCK", "steals": "STEAL",
    "assists": "ASSIST", "fan_reactions": "CROWD", "big_moment": "BIG MOMENT", "other": "HIGHLIGHT",
    "highlight": "HIGHLIGHT",
}


def font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """GAMEDAY_FONT if set, else DejaVu Sans Bold, else Arial or Segoe, else Pillow's own."""
    chosen = os.environ.get("GAMEDAY_FONT", "").strip()
    for path in ([chosen] if chosen else []) + list(FONT_PATHS):
        if path and Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def window_px(width: int, height: int) -> tuple[int, int, int, bool]:
    """(width, height, top, pans) of the 9:16 window in source pixels. A frame
    wider than 9:16 gets a full-height window that pans; a taller one gets a
    full-width window, centred, that doesn't."""
    if width * 16 >= height * 9:
        ww = max(2, min(width - width % 2, 2 * int(round(height * 9 / 16 / 2))))
        return ww, height - height % 2, 0, ww < width
    wh = max(2, min(height - height % 2, 2 * int(round(width * 16 / 9 / 2))))
    return width - width % 2, wh, ((height - wh) // 2) // 2 * 2, False


def output_fps(fps: float) -> float:
    """At most 30 frames a second: 50 and 60 are halved, other high rates become 30."""
    if not fps or fps <= MAX_FPS + 0.5:
        return 0.0  # keep the source's rate
    half = fps / 2
    return round(half, 3) if half <= MAX_FPS + 0.5 else MAX_FPS


def left_pixels(left_share: np.ndarray, width: int, window_w: int) -> np.ndarray:
    """The window's left edge in even source pixels, inside the frame."""
    x = np.round(np.asarray(left_share, dtype=float) * width / 2) * 2
    return np.clip(x, 0, max(0, width - window_w)).astype(int)


def write_commands(path: Path, xs: np.ndarray, fps: float = PATH_FPS, target: str = "crop@pan") -> int:
    """An ffmpeg sendcmd file that moves the crop to xs[i] at i/fps seconds. Returns the line count."""
    lines = []
    last = None
    for i, x in enumerate(xs):
        if x != last:
            lines.append(f"{i / fps:.4f} {target} x {int(x)};")
            last = x
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return len(lines)


def _wrap(draw: ImageDraw.ImageDraw, text: str, face, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=face) <= width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def title_card(title: str, category: str, out: Path) -> Path:
    """A transparent 1080x1920 PNG: the title on a dark rounded band in the upper third."""
    img = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    text = " ".join(title.split()) or "Highlight"
    inner = 900
    size, lines, face = 88, [], font(88)
    while size >= 44:
        face = font(size)
        lines = _wrap(draw, text, face, inner)
        if len(lines) <= 3 and all(draw.textlength(line, font=face) <= inner for line in lines):
            break
        size -= 6
    if len(lines) > 3:
        lines = textwrap.wrap(text, 28)[:3]
    chip_face = font(40)
    chip = CATEGORY_WORDS.get(category, "")
    line_h = int(size * 1.18)
    pad = 44
    chip_h = 64 if chip else 0
    top = 300
    band_h = pad * 2 + chip_h + line_h * len(lines)
    draw.rounded_rectangle([60, top, OUT_W - 60, top + band_h], radius=40, fill=(12, 12, 18, 205))
    y = top + pad
    if chip:
        cw = int(draw.textlength(chip, font=chip_face)) + 40
        cx = (OUT_W - cw) // 2
        draw.rounded_rectangle([cx, y, cx + cw, y + 52], radius=26, fill=(255, 196, 0, 255))
        draw.text((OUT_W // 2, y + 26), chip, font=chip_face, fill=(20, 20, 20, 255), anchor="mm")
        y += chip_h
    for line in lines:
        draw.text((OUT_W // 2, y + line_h // 2), line, font=face, fill=(255, 255, 255, 255), anchor="mm",
                  stroke_width=2, stroke_fill=(0, 0, 0, 255))
        y += line_h
    img.save(out)
    return out


def render(source: Path, info: MediaInfo, start_s: float, end_s: float, left_share: np.ndarray, out: Path,
           tmp: Path, *, title: str = "", category: str = "", timeout: float | None = 1800) -> Path:
    """The clip [start_s, end_s] of `source` as a 1080x1920 H.264 + AAC MP4 at `out`."""
    seconds = max(0.1, end_s - start_s)
    ww, wh, top, pans = window_px(info.width, info.height)
    xs = left_pixels(left_share, info.width, ww) if pans else np.zeros(1, dtype=int)
    stem = out.stem
    chain = []
    rate = output_fps(info.fps)
    if rate:
        chain.append(f"fps={rate:g}")
    if pans and len(np.unique(xs)) > 1:
        write_commands(tmp / f"{stem}.cmd", xs)
        chain.append(f"sendcmd=f={stem}.cmd")
    chain += [f"crop@pan=w={ww}:h={wh}:x={int(xs[0])}:y={top}", f"scale={OUT_W}:{OUT_H}:flags=lanczos", "setsar=1"]
    args = ["-ss", f"{start_s:.3f}", "-t", f"{seconds:.3f}", "-i", str(source)]
    graph = [f"[0:v]{','.join(chain)},format=yuv420p[base]"]
    card_rate = rate or (info.fps if 1 <= info.fps <= MAX_FPS + 0.5 else MAX_FPS)
    next_input = 1
    if title:
        card = title_card(title, category, tmp / f"{stem}_card.png")
        args += ["-loop", "1", "-framerate", f"{card_rate:g}", "-t", f"{min(TITLE_S, seconds):.3f}", "-i", str(card)]
        fade_at = max(0.0, min(TITLE_S, seconds) - 0.4)
        graph.append(f"[{next_input}:v]format=rgba,fade=t=out:st={fade_at:.2f}:d=0.4:alpha=1[card]")
        graph.append("[base][card]overlay=0:0:eof_action=pass:format=auto,format=yuv420p[v]")
        next_input += 1
        video = "[v]"
    else:
        video = "[base]"
    if info.has_audio:
        audio = "0:a:0"
    else:
        args += ["-f", "lavfi", "-t", f"{seconds:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        audio = f"{next_input}:a:0"
    args += ["-filter_complex", ";".join(graph), "-map", video, "-map", audio, "-t", f"{seconds:.3f}",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-profile:v", "high", "-pix_fmt", "yuv420p",
             "-g", str(int(round(2 * (card_rate or 30)))), "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
             "-movflags", "+faststart", str(out.resolve())]
    with _ONE_AT_A_TIME:
        ff.run(args, cwd=tmp, timeout=timeout)
    if not out.exists() or out.stat().st_size == 0:
        raise MediaError(f"ffmpeg made no {out.name}")
    return out


def thumbnail(clip: Path, at_s: float, out: Path) -> Path:
    """A 720x1280 JPEG of the rendered clip at `at_s` seconds."""
    return ff.grab_frame(clip, at_s, out, width=720)


def debug_image(source: Path, at_s: float, out: Path, tmp: Path, *, window_left: float, window: float,
                ball: tuple[float, float, float, float] | None, players: list[tuple[float, float, float, float]]
                | None = None, note: str = "") -> Path:
    """The wide frame at `at_s` with SAM's ball box in red, the players in yellow and the
    9:16 window in green. Boxes are (left, top, right, bottom) as shares of the frame."""
    frame = ff.grab_frame(source, at_s, tmp / f"{out.stem}_wide.jpg", width=1280)
    with Image.open(frame) as opened:
        img = opened.convert("RGB")
    w, h = img.size
    draw = ImageDraw.Draw(img)
    for p in players or []:
        draw.rectangle([p[0] * w, p[1] * h, p[2] * w, p[3] * h], outline=(255, 210, 0), width=2)
    x0 = window_left * w
    draw.rectangle([x0, 1, x0 + window * w, h - 2], outline=(40, 220, 60), width=5)
    if ball is not None:
        draw.rectangle([ball[0] * w - 3, ball[1] * h - 3, ball[2] * w + 3, ball[3] * h + 3], outline=(255, 30, 30),
                       width=4)
    caption = f"{at_s:.1f} s   red: SAM 3.1 ball   yellow: players   green: 9:16 window"
    if note:
        caption += "\n" + note
    face = font(max(12, h // 32))
    box = draw.multiline_textbbox((10, 8), caption, font=face)
    draw.rectangle([box[0] - 6, box[1] - 4, box[2] + 6, box[3] + 4], fill=(0, 0, 0))
    draw.multiline_text((10, 8), caption, font=face, fill=(255, 255, 255))
    img.save(out, quality=88)
    return out
