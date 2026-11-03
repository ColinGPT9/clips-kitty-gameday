"""How well does SAM 3.1 follow the ball in this video? One request, measured.

    python scripts/sam_check.py <video> [--start S] [--seconds N] [--fps F] [--noun basketball] [--out DIR]

Makes the same small proxy the pipeline sends, asks SAM 3.1 (hosted by Meta)
for every box of the noun, prints the coverage, object ids, confidence, time
and estimated cost, and writes a contact sheet of 12 frames with the boxes.
Needs META_API_KEY in the environment; it is never printed. About $0.20 per
1,000 frames sent (10 fps for 40 s is 400 frames, about $0.08).
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw  # noqa: E402

from gameday import media  # noqa: E402
from gameday.engine import ff, tracking  # noqa: E402
from gameday.meta_api import MetaClient  # noqa: E402
from gameday.meta_api.ledger import ConsoleLedger  # noqa: E402
from gameday.meta_api.video import video_facts  # noqa: E402
from gameday.types import MetaError  # noqa: E402


def contact_sheet(proxy: Path, seg, out: Path, tmp: Path) -> Path:
    n = max(1, seg.frames)
    picks = sorted({int(i * (n - 1) / 11) for i in range(12)})
    per_frame = defaultdict(list)
    for b in seg.boxes:
        per_frame[b.frame].append(b)
    tiles = []
    for k in picks:
        jpg = ff.grab_frame(proxy, k / (seg.fps or 10.0), tmp / f"sheet_{k:05d}.jpg")
        with Image.open(jpg) as opened:
            img = opened.convert("RGB")
        draw = ImageDraw.Draw(img)
        for b in per_frame.get(k, []):
            draw.rectangle([b.left, b.top, b.right, b.bottom], outline=(255, 40, 40), width=3)
            draw.text((b.left, max(0, b.top - 12)), f"{b.object_id} {b.confidence or 0:.2f}", fill=(255, 255, 0))
        draw.text((6, 6), f"frame {k}", fill=(255, 255, 255))
        tiles.append(img)
        jpg.unlink(missing_ok=True)
    tw, th = tiles[0].size
    sheet = Image.new("RGB", (tw * 4, th * ((len(tiles) + 3) // 4)))
    for i, img in enumerate(tiles):
        sheet.paste(img, ((i % 4) * tw, (i // 4) * th))
    sheet.save(out, quality=85)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("video", type=Path)
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--noun", default="basketball")
    parser.add_argument("--out", type=Path, default=Path("sam_check"))
    parser.add_argument("--budget", type=float, default=0.50, help="refuse to spend more than this (USD)")
    args = parser.parse_args()
    key = os.environ.get("META_API_KEY", "").strip()
    if not key:
        print("Set META_API_KEY first.", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    info = media.probe(args.video)
    seconds = min(args.seconds, max(0.1, info.duration_s - args.start))
    proxy = ff.make_proxy(args.video, args.out / f"proxy_{args.noun.replace(' ', '_')}.mp4", start=args.start,
                          seconds=seconds, fps=args.fps)
    facts = video_facts(proxy)
    print(f"proxy: {facts.width}x{facts.height}, {facts.frames} frames at {facts.fps:g} fps, "
          f"{proxy.stat().st_size / 1e6:.2f} MB")
    ledger = ConsoleLedger(args.budget)
    client = MetaClient(key, ledger=ledger)
    started = time.monotonic()
    try:
        seg = client.segment_video(proxy, args.noun)
    except MetaError as exc:
        print(f"SAM 3.1 failed: {exc}", file=sys.stderr)
        return 1
    took = time.monotonic() - started
    framed = {b.frame for b in seg.boxes}
    ids = sorted({b.object_id for b in seg.boxes})
    confs = [b.confidence for b in seg.boxes if b.confidence is not None]
    widths = sorted((b.right - b.left) / seg.width for b in seg.boxes)
    ball = tracking.pick_ball(seg)
    print(f"{len(seg.boxes)} boxes on {len(framed)} of {seg.frames} frames ({len(framed) / max(1, seg.frames):.0%}); "
          f"usable ball on {ball.found.mean():.0%}; ids {ids}")
    if confs:
        print(f"confidence: mean {statistics.mean(confs):.2f}, min {min(confs):.2f}")
    if widths:
        print(f"box width / frame width: median {widths[len(widths) // 2]:.3f}, min {widths[0]:.3f}, "
              f"max {widths[-1]:.3f}")
    if ball.static_ids:
        print(f"ids that never move (ignored): {sorted(ball.static_ids)}")
    print(f"time {took:.1f} s; estimated cost ${ledger.spent_usd:.4f}")
    if seg.frames:
        sheet = contact_sheet(proxy, seg, args.out / f"contact_{args.noun.replace(' ', '_')}.jpg", args.out)
        print(f"contact sheet: {sheet}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
