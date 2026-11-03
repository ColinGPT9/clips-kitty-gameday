"""Run the whole engine on one video, outside the app.

    python scripts/run_pipeline.py <video> <out_dir> [--max-clips N] [--mode basketball|talk] [--hint TEXT]

Uses META_API_KEY from the environment (never printed); without it the run
uses the fallbacks (crowd-noise picks, centre crops). Every Meta call is
printed with its estimated cost, calls past --budget are refused, and the
result is written to <out_dir>/result.json beside the clips.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameday.engine import pipeline  # noqa: E402
from gameday.meta_api import MetaClient  # noqa: E402
from gameday.meta_api.ledger import ConsoleLedger  # noqa: E402
from gameday.types import JobCancelled, PipelineError, PipelineOptions  # noqa: E402


def plain(value):
    """Dataclasses and paths as JSON-friendly values."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [plain(v) for v in value]
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("video", type=Path)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--max-clips", type=int, default=8)
    parser.add_argument("--mode", choices=("basketball", "talk"), default="basketball")
    parser.add_argument("--hint", default="")
    parser.add_argument("--min-clip", type=float, default=12.0)
    parser.add_argument("--max-clip", type=float, default=40.0)
    parser.add_argument("--no-title-cards", action="store_true")
    parser.add_argument("--budget", type=float, default=3.0, help="refuse Meta calls past this many USD")
    parser.add_argument("--job-id", type=int, default=1)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    key = os.environ.get("META_API_KEY", "").strip()
    ledger = ConsoleLedger(args.budget)
    meta = MetaClient(key, ledger=ledger) if key else None
    if meta is None:
        print("No META_API_KEY: running on the fallbacks only.")
    options = PipelineOptions(mode=args.mode, max_clips=args.max_clips, min_clip_s=args.min_clip,
                              max_clip_s=args.max_clip, hint=args.hint, title_cards=not args.no_title_cards)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    def progress(stage: str, share: float, message: str) -> None:
        print(f"[{stage:>7} {share:4.0%}] {message}", flush=True)

    started = time.monotonic()
    try:
        result = pipeline.run(args.job_id, args.video, args.out_dir, options, meta, progress, lambda: False)
    except (PipelineError, JobCancelled) as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 1
    took = time.monotonic() - started
    report = plain(result)
    report["ledger"] = {"by_model": ledger.by_model(), "total_usd": ledger.spent_usd}
    report["wall_seconds"] = round(took, 1)
    (args.out_dir / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"\n{len(result.clips)} clips from {result.duration_s:.0f} s in {took:.0f} s")
    for clip in result.clips:
        follow = clip.follow.note if clip.follow else ""
        print(f"  {clip.index}. {clip.start_s:7.1f}-{clip.end_s:7.1f} s  [{clip.category}] {clip.title}  "
              f"(score {clip.score:.0f}; {follow})")
    print(f"seconds per stage: {result.stats.get('seconds')}")
    print(f"cost by model (estimate): {result.stats.get('cost_usd')}  total ~${ledger.spent_usd:.4f}")
    for line in result.warnings:
        print(f"warning: {line}")
    print(f"result: {args.out_dir / 'result.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
