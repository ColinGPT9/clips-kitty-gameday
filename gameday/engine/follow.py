"""Per clip: the small proxy SAM 3.1 sees, the broadcast cuts, SAM's boxes, the
clip's final start and end, and the 9:16 window's path."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from gameday.engine import ff, moments, shots, tracking
from gameday.media import MediaError
from gameday.meta_api.video import VideoFacts, video_facts
from gameday.types import Follow, MetaAPI, MetaError, PipelineOptions, Segmentation

log = logging.getLogger(__name__)

PART_BYTES = 5_500_000  # a proxy bigger than this goes to SAM in parts
PART_S = 20.0
NEEDS_PLAYERS = 0.6  # below this ball coverage, SAM is also asked for the players
CUTAWAY_SHARE = 0.15  # a shot with the ball in fewer of its frames is a cutaway
# So is one where the ball is usually this wide (a share of the frame's width): a close-up of the players. In the
# HD demo game the ball was 1 to 3% of the width from the wide camera, 4.5% from the one under the basket and in
# replays, and 6 to 13% in close-ups of the players.
CLOSE_UP_BALL = 0.06
# Where the ball is missing, the window follows the players. "basketball player" finds the players on the
# court and not the crowd ("person" found both, and took six times as long); 3 frames a second is plenty.
PLAYERS_NOUN = "basketball player"
PLAYERS_FPS = 3.0


@dataclass
class ClipWork:
    rank: int  # 1 = best
    moment: moments.Moment
    window: tuple[float, float]  # the proxy's span in the source, seconds
    start_s: float = 0.0  # the clip, after the rules (source seconds)
    end_s: float = 0.0
    proxy: Path | None = None
    facts: VideoFacts | None = None
    cuts: list[float] = field(default_factory=list)  # source seconds
    ball: Segmentation | None = None
    people: Segmentation | None = None
    plan: tracking.CropPlan | None = None
    follow: Follow | None = None
    warnings: list[str] = field(default_factory=list)
    fallbacks: list[str] = field(default_factory=list)
    sam_frames: int = 0  # frames sent to SAM 3.1 for this clip
    left_out: str = ""  # why the rules made no clip of this moment (moments.NoClip), if they didn't

    def to_proxy(self, t: float) -> float:
        return t - self.window[0]


def proxy_window(m: moments.Moment, duration_s: float, options: PipelineOptions) -> tuple[float, float]:
    """The stretch of video worth showing SAM: the rough clip with room for the rules to move its ends."""
    start, end = moments.rough_range(m, duration_s, options)
    return round(max(0.0, start - 6.0), 3), round(min(duration_s, end + 7.0), 3)


def cutaways(track: tracking.Track, fps: float, window_start: float) -> moments.Cutaway:
    """What kind of cutaway a shot (source seconds) is, by SAM's ball: missing from most of its frames
    (the crowd, the bench), or big in them (a close-up of the players, even with the ball in hand)."""
    def cutaway(a: float, b: float) -> str:
        share = tracking.ball_share(track, fps, a - window_start, b - window_start)
        if share is not None and share < CUTAWAY_SHARE:
            return moments.NO_BALL
        size = tracking.ball_size(track, fps, a - window_start, b - window_start)
        return moments.CLOSE_UP if size is not None and size >= CLOSE_UP_BALL else ""

    return cutaway


def _shots(work: ClipWork, track: tracking.Track | None, fps: float) -> str:
    """Every shot of the window by its first frame (the window's start, then each cut), with SAM's ball
    share and the ball's usual width in it, for the log: enough to replay the rules offline."""
    ws, we = work.window
    edges = [ws, *(c for c in work.cuts if ws < c < we), we]
    parts = []
    for a, b in zip(edges, edges[1:], strict=False):
        about = []
        if track is not None:
            share = tracking.ball_share(track, fps, a - ws, b - ws)
            size = tracking.ball_size(track, fps, a - ws, b - ws)
            about = ([] if share is None else [f"ball {share:.0%}"]) + ([] if size is None else [f"{size:.1%} wide"])
        parts.append(f"{a:.2f}" + (f" ({', '.join(about)})" if about else ""))
    return ", ".join(parts)


def segment(meta: MetaAPI, proxy: Path, facts: VideoFacts, noun: str, *, job_id: int | None,
            tmp: Path) -> Segmentation:
    """SAM 3.1 on the proxy: in one request, or in parts of PART_S seconds when it is big."""
    if proxy.stat().st_size <= PART_BYTES:
        return meta.segment_video(proxy, noun, job_id=job_id)
    seconds = facts.frames / (facts.fps or 10.0)
    count = max(1, math.ceil(seconds / PART_S - 1e-6))
    boxes = []
    for p in range(count):
        start = p * PART_S
        part = ff.cut_part(proxy, tmp / f"{proxy.stem}_{noun.replace(' ', '_')}_{p}.mp4", start=start,
                           seconds=min(PART_S, seconds - start))
        seg = meta.segment_video(part, noun, job_id=job_id)
        offset = int(round(start * (facts.fps or 10.0)))
        boxes += [replace(b, frame=b.frame + offset, object_id=b.object_id + 100_000 * p) for b in seg.boxes]
    boxes.sort(key=lambda b: (b.frame, b.object_id))
    return Segmentation(noun=noun, width=facts.width, height=facts.height, frames=facts.frames, fps=facts.fps,
                        boxes=boxes)


def players_pass(meta: MetaAPI, work: ClipWork, *, source: Path, tmp: Path, job_id: int | None) -> Segmentation:
    """SAM 3.1 on a 3 fps proxy of the same span, its boxes moved onto the ball proxy's frames."""
    assert work.facts is not None
    ws, we = work.window
    proxy = ff.make_proxy(source, tmp / f"clip_{work.rank:02d}_players.mp4", start=ws, seconds=we - ws,
                          fps=PLAYERS_FPS)
    facts = video_facts(proxy)
    seg = segment(meta, proxy, facts, PLAYERS_NOUN, job_id=job_id, tmp=tmp)
    work.sam_frames += seg.frames
    step = (work.facts.fps or 10.0) / (facts.fps or PLAYERS_FPS)
    scale = work.facts.width / facts.width if facts.width else 1.0
    last = max(0, work.facts.frames - 1)
    boxes = [replace(b, frame=min(last, int(round(b.frame * step))), left=b.left * scale, top=b.top * scale,
                     right=b.right * scale, bottom=b.bottom * scale) for b in seg.boxes]
    return Segmentation(noun=PLAYERS_NOUN, width=work.facts.width, height=work.facts.height,
                        frames=work.facts.frames, fps=work.facts.fps, boxes=boxes)


def track_clip(work: ClipWork, *, source: Path, source_size: tuple[int, int], duration_s: float, tmp: Path,
               options: PipelineOptions, meta: MetaAPI | None, job_id: int | None,
               sents: list[moments.Sentence], should_stop: Callable[[], bool]) -> ClipWork:
    """Everything for one clip short of rendering it. Never raises for a Meta or ffmpeg failure:
    the clip falls back to a centre crop and says so."""
    ws, we = work.window
    window = tracking.window_share(*source_size)
    name = f"clip {work.rank}"
    try:
        work.proxy = ff.make_proxy(source, tmp / f"clip_{work.rank:02d}_proxy.mp4", start=ws, seconds=we - ws,
                                   fps=options.sam_fps)
        work.facts = video_facts(work.proxy)
    except MediaError as exc:
        log.warning("proxy for %s failed: %s", name, exc)
        work.proxy, work.facts = None, None
        work.warnings.append(f"Couldn't prepare {name} for SAM 3.1, so it is centre-cropped.")
        work.fallbacks.append(f"centre_crop:{work.rank}")
    # The broadcast's cuts come from the game itself, every frame of the span (not SAM's proxy).
    try:
        work.cuts = shots.scan(source, ws, we)
    except MediaError as exc:
        log.warning("finding the cuts around %s failed: %s", name, exc)
        work.warnings.append(f"Couldn't find the camera cuts around {name}, so it ends on the commentary alone.")

    talk = options.mode == "talk"
    if work.proxy is not None and work.facts is not None and meta is not None and not should_stop():
        noun = "person" if talk else (options.ball_noun or "basketball")
        try:
            seg = segment(meta, work.proxy, work.facts, noun, job_id=job_id, tmp=tmp)
            work.sam_frames += seg.frames
            if talk:
                work.people = seg
            else:
                work.ball = seg
        except (MetaError, MediaError) as exc:
            log.warning("SAM 3.1 on %s failed: %s", name, exc)
            work.warnings.append(f"SAM 3.1 failed on {name} ({exc}), so it is centre-cropped.")
            work.fallbacks.append(f"centre_crop:{work.rank}")
        if work.ball is not None and not should_stop():
            rough = moments.rough_range(work.moment, duration_s, options)
            ball = tracking.pick_ball(work.ball)
            share = tracking.ball_share(ball, work.facts.fps, work.to_proxy(rough[0]), work.to_proxy(rough[1]))
            if share is not None and share < NEEDS_PLAYERS:
                try:
                    work.people = players_pass(meta, work, source=source, tmp=tmp, job_id=job_id)
                except (MetaError, MediaError) as exc:
                    log.warning("SAM 3.1 players on %s failed: %s", name, exc)
                    work.warnings.append(f"SAM 3.1 couldn't find the players in {name} ({exc}).")

    # Where the clip starts and ends.
    cutaway = None
    track = tracking.pick_ball(work.ball) if work.ball is not None and work.facts is not None else None
    fps = (work.facts.fps if work.facts is not None else 0.0) or options.sam_fps
    if track is not None and len(track.found) and float(track.found.mean()) >= 0.2:
        # SAM sees the ball in this footage, so a shot without it, or with it close up, means something.
        cutaway = cutaways(track, fps, ws)
    try:
        work.start_s, work.end_s = moments.refine(work.moment, cuts=work.cuts, sents=sents, duration_s=duration_s,
                                                  options=options, window=(ws, we), cutaway=cutaway)
    except moments.NoClip as exc:
        work.left_out = str(exc)
        log.info("%s: play at %.2f, no clip: %s; shots from %s", name, work.moment.play_s, exc,
                 _shots(work, track, fps))
        return work
    at_cut = any(abs(c - moments.CUT_GUARD_S - work.end_s) < 0.011 for c in work.cuts)
    log.info("%s: play at %.2f, clip %.2f-%.2f %s; shots from %s", name, work.moment.play_s, work.start_s,
             work.end_s, "ending at a cut" if at_cut else "ending on the talk", _shots(work, track, fps))

    # The window's path.
    if work.facts is None:
        work.plan = tracking.centre_plan(work.start_s, work.end_s, window, "Centre crop")
    elif work.ball is None and work.people is None:
        why = "no Meta key" if meta is None else "SAM 3.1 wasn't available"
        work.plan = tracking.centre_plan(work.start_s, work.end_s, window, f"Centre crop: {why}")
    else:
        work.plan = tracking.plan(proxy_frames=work.facts.frames, proxy_fps=work.facts.fps or options.sam_fps,
                                  cuts=[c - ws for c in work.cuts], clip_start=work.to_proxy(work.start_s),
                                  clip_end=work.to_proxy(work.end_s), window=window, ball=work.ball,
                                  people=work.people, mode=options.mode)
    work.follow = Follow(source=work.plan.source, ball_coverage=work.plan.coverage, frames=work.plan.frames,
                         note=work.plan.note)
    return work
