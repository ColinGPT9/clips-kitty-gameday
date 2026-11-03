"""gameday.engine.pipeline.run: a game in, vertical clips out.

Muse Voice listens to the commentary, Muse Spark picks the plays, SAM 3.1
follows the ball, and ffmpeg cuts and renders. Every Meta failure has a
fallback (crowd-noise picks, a centre crop), named in the result's warnings,
so a run only stops for a video that can't be read or for a cancel.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path

import numpy as np

from gameday import media
from gameday.engine import ff, follow, moments, render, tracking
from gameday.engine import loudness as loudness_mod
from gameday.media import MediaError
from gameday.types import (
    MODEL_SAM,
    MODEL_SPARK,
    MODEL_VOICE,
    BudgetExceeded,
    ClipResult,
    JobCancelled,
    MetaAPI,
    MetaError,
    PipelineError,
    PipelineOptions,
    PipelineResult,
    ProgressFn,
    StopFn,
    Turn,
)

log = logging.getLogger(__name__)

MIN_VIDEO_S = 20.0
SAM_AT_ONCE = 3
SPARK_MAX_TOKENS = 8000  # the client adds room for Muse Spark's reasoning


def _why(exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text if len(text) <= 160 else text[:157] + "..."


class _Run:
    def __init__(self, job_id: int, source: Path, work_dir: Path, options: PipelineOptions, meta: MetaAPI | None,
                 progress: ProgressFn, should_stop: StopFn):
        self.job_id = job_id
        self.source = Path(source)
        self.work_dir = Path(work_dir)
        self.options = options
        self.meta = meta
        self._progress = progress
        self._should_stop = should_stop
        self.clips_dir = self.work_dir / "clips"
        self.tmp = self.work_dir / "tmp"
        self.seconds: dict[str, float] = {}
        self.fallbacks: list[str] = []
        self.warnings: list[str] = []
        self.voice_seconds = 0.0
        self.sam_frames = 0
        self.spent_before = self._spent()

    # --- small helpers ---------------------------------------------------------

    def say(self, stage: str, share: float, message: str) -> None:
        try:
            self._progress(stage, max(0.0, min(1.0, share)), message)
        except Exception:  # noqa: BLE001 - a progress callback must not stop the run
            log.exception("progress callback failed")

    def stop_point(self) -> None:
        if self._should_stop():
            raise JobCancelled("Stopped")

    def warn(self, text: str, fallback: str = "") -> None:
        if text not in self.warnings:
            self.warnings.append(text)
        if fallback and fallback not in self.fallbacks:
            self.fallbacks.append(fallback)

    def _spent(self) -> dict[str, float] | None:
        reader: Callable[[], dict] | None = getattr(self.meta, "spent_by_model", None)
        if reader is None:
            return None
        try:
            return dict(reader())
        except Exception:  # noqa: BLE001 - a fake without it is fine
            return None

    def timed(self, name: str, started: float) -> None:
        self.seconds[name] = round(self.seconds.get(name, 0.0) + time.monotonic() - started, 2)

    # --- the stages -----------------------------------------------------------------

    def go(self) -> PipelineResult:
        started = time.monotonic()
        try:
            info = media.probe(self.source)
        except MediaError as exc:
            raise PipelineError(f"This video can't be read: {_why(exc)}") from exc
        if info.duration_s < MIN_VIDEO_S:
            raise PipelineError(f"This video is {info.duration_s:.0f} s long; a game needs at least "
                                f"{MIN_VIDEO_S:.0f} s.")
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.timed("probe", started)
        self.stop_point()

        wav, turns = self.voice(info)
        self.stop_point()
        loud = self.loudness(wav)
        self.stop_point()
        picks = self.spark(info, turns, loud)
        self.stop_point()
        works = self.sam(info, turns, picks)
        self.stop_point()
        clips = self.render_all(info, turns, loud, works)
        return PipelineResult(clips=clips, duration_s=info.duration_s, transcript=turns,
                              stats=self.stats(picks, works), warnings=self.warnings)

    def voice(self, info: media.MediaInfo) -> tuple[Path | None, list[Turn]]:
        started = time.monotonic()
        self.say("voice", 0.0, "Listening to the commentary")
        if not info.has_audio:
            self.warn("The video has no sound, so the clips are spread through it.", "no_audio")
            self.say("voice", 1.0, "No sound to listen to")
            self.timed("voice", started)
            return None, []
        wav = self.tmp / "audio.wav"
        try:
            ff.extract_wav(self.source, wav, timeout=3600)
        except MediaError as exc:
            log.warning("audio extraction failed: %s", exc)
            self.warn("The sound couldn't be read, so the clips are spread through the video.", "no_audio")
            self.timed("voice", started)
            return None, []
        turns: list[Turn] = []
        if self.meta is None:
            self.warn("No Meta key is saved, so the clips were picked on crowd noise alone and centre-cropped.",
                      "no_key")
            self.say("voice", 1.0, "Skipped: no Meta key")
        else:
            self.say("voice", 0.1, "Muse Voice is transcribing the commentary")
            try:
                turns = self.meta.transcribe(wav, job_id=self.job_id)
                self.voice_seconds = info.duration_s
                self.say("voice", 1.0, f"{len(turns)} stretches of commentary")
            except BudgetExceeded:
                self.warn("The Meta budget is used up, so the clips were picked on crowd noise alone.", "budget")
            except MetaError as exc:
                log.warning("Muse Voice failed: %s", exc)
                self.warn(f"Muse Voice failed ({_why(exc)}), so the clips were picked on crowd noise alone.", "voice")
            if not turns and "voice" not in self.fallbacks and "budget" not in self.fallbacks:
                self.warn("Muse Voice heard no commentary, so the clips were picked on crowd noise alone.",
                          "no_commentary")
        self.timed("voice", started)
        return wav, turns

    def loudness(self, wav: Path | None) -> loudness_mod.Loudness:
        started = time.monotonic()
        self.say("spark", 0.0, "Measuring the crowd noise")
        loud = loudness_mod.empty()
        if wav is not None:
            try:
                loud = loudness_mod.measure(wav)
            except (OSError, ValueError, EOFError) as exc:
                log.warning("loudness failed: %s", exc)
        self.timed("loudness", started)
        return loud

    def spark(self, info: media.MediaInfo, turns: list[Turn], loud: loudness_mod.Loudness) -> list[moments.Moment]:
        started = time.monotonic()
        options = self.options
        picks: list[moments.Moment] = []
        if self.meta is not None and turns:
            self.say("spark", 0.1, "Muse Spark is picking the plays")
            peaks = loudness_mod.peaks(loud, min_z=1.5, min_gap_s=10.0, limit=60)
            try:
                answer = self.meta.chat_json(
                    moments.build_messages(turns, peaks, duration_s=info.duration_s, options=options),
                    moments.moment_schema(), name="clip_moments", purpose="moments", job_id=self.job_id,
                    max_tokens=SPARK_MAX_TOKENS, temperature=0.2)
                found = moments.parse_moments(answer, duration_s=info.duration_s, mode=options.mode,
                                              known_text=moments.known_words(turns, options.hint))
                said = [m.play_s for m in found]
                if options.mode != "talk":
                    moments.snap_to_loudness(found, loud)
                    moments.note_the_calls(found, turns)
                for m, guess in zip(found, said, strict=True):
                    log.info("moment: play at %.2f (Muse Spark said %.2f), called at %s, lead-up from %.2f: %s",
                             m.play_s, guess, m.called_s, m.start_s, m.evidence[:80])
                picks = moments.drop_overlaps(found, options.max_clips, info.duration_s, options)
                if not picks:
                    self.warn("Muse Spark found no plays, so the clips were picked on crowd noise.", "spark_empty")
            except BudgetExceeded:
                self.warn("The Meta budget is used up, so the clips were picked on crowd noise.", "budget")
            except MetaError as exc:
                log.warning("Muse Spark failed: %s", exc)
                self.warn(f"Muse Spark failed ({_why(exc)}), so the clips were picked on crowd noise.", "spark")
        if not picks:
            picks = moments.fallback_moments(loud, duration_s=info.duration_s, options=options, turns=turns)
        self.say("spark", 1.0, f"Picked {len(picks)} moments")
        self.timed("spark", started)
        return picks

    def sam(self, info: media.MediaInfo, turns: list[Turn], picks: list[moments.Moment]) -> list[follow.ClipWork]:
        started = time.monotonic()
        total = len(picks)
        works = [follow.ClipWork(rank=i, moment=m, window=follow.proxy_window(m, info.duration_s, self.options))
                 for i, m in enumerate(picks, start=1)]
        sents = moments.sentences(turns)
        thing = "the speaker" if self.options.mode == "talk" else "the ball"
        self.say("sam", 0.0, f"SAM 3.1 is following {thing}")
        done = 0
        pool = ThreadPoolExecutor(max_workers=SAM_AT_ONCE, thread_name_prefix="sam")
        try:
            running: set[Future] = set()
            for work in works:
                running.add(pool.submit(follow.track_clip, work, source=self.source,
                                        source_size=(info.width, info.height), duration_s=info.duration_s,
                                        tmp=self.tmp, options=self.options, meta=self.meta, job_id=self.job_id,
                                        sents=sents, should_stop=self._should_stop))
            while running:
                finished, running = wait(running, timeout=1.0, return_when=FIRST_COMPLETED)
                for future in finished:
                    future.result()  # track_clip handles Meta and ffmpeg failures itself
                    done += 1
                    self.say("sam", done / max(1, total), f"Following {thing} in clip {done} of {total}")
                if self._should_stop():
                    raise JobCancelled("Stopped")
        finally:
            # On a cancel, don't wait for SAM calls already on the wire: their answers are dropped.
            pool.shutdown(wait=False, cancel_futures=True)
        for work in works:
            self.sam_frames += work.sam_frames
            for text in work.warnings:
                self.warn(text)
            for name in work.fallbacks:
                if name not in self.fallbacks:
                    self.fallbacks.append(name)
            if work.left_out:
                minutes, seconds = divmod(int(work.moment.play_s), 60)
                self.warn(f"No clip of the play at {minutes}:{seconds:02d}: {work.left_out}.")
        works = self.separate([work for work in works if not work.left_out])
        self.timed("sam", started)
        return works

    def separate(self, works: list[follow.ClipWork]) -> list[follow.ClipWork]:
        """After the rules moved the ends, no two clips may overlap: the better one keeps its span."""
        kept: list[follow.ClipWork] = []
        shortest = self.options.min_clip_s * 0.75
        for work in sorted(works, key=lambda w: w.rank):
            fits = True
            for other in kept:
                if not (work.start_s < other.end_s and other.start_s < work.end_s):
                    continue
                plan = work.plan
                after, before = other.start_s <= work.start_s, other.start_s > work.start_s
                if plan is not None and after and work.end_s - other.end_s - 0.1 >= shortest:
                    skip = int(round((other.end_s + 0.1 - work.start_s) * plan.fps))
                    plan.left = plan.left[skip:]
                    plan.target = None if plan.target is None else plan.target[skip:]
                    work.start_s = round(work.start_s + skip / plan.fps, 2)
                elif plan is not None and before and other.start_s - 0.1 - work.start_s >= shortest:
                    keep = int(round((other.start_s - 0.1 - work.start_s) * plan.fps))
                    plan.left = plan.left[:keep]
                    plan.target = None if plan.target is None else plan.target[:keep]
                    work.end_s = round(work.start_s + keep / plan.fps, 2)
                else:
                    fits = False
                    break
            if fits:
                kept.append(work)
        for rank, work in enumerate(kept, start=1):
            work.rank = rank
        return kept

    def render_all(self, info: media.MediaInfo, turns: list[Turn], loud: loudness_mod.Loudness,
                   works: list[follow.ClipWork]) -> list[ClipResult]:
        started = time.monotonic()
        clips: list[ClipResult] = []
        errors: list[str] = []
        for i, work in enumerate(works, start=1):
            self.stop_point()
            self.say("render", (i - 1) / max(1, len(works)), f"Rendering clip {i} of {len(works)}")
            name = f"clip_{work.rank:02d}"
            out = self.clips_dir / f"{name}.mp4"
            m = work.moment
            window = tracking.window_share(info.width, info.height)
            plan = work.plan or tracking.centre_plan(work.start_s, work.end_s, window, "Centre crop")
            try:
                render.render(self.source, info, work.start_s, work.end_s, plan.left, out, self.tmp,
                              title=m.title if self.options.title_cards else "", category=m.category)
            except MediaError as exc:
                log.warning("render of %s failed: %s", name, exc)
                errors.append(_why(exc))
                self.warn(f"Clip {work.rank} couldn't be rendered ({_why(exc)}).", f"render:{work.rank}")
                continue
            busy = self.busiest(work, loud)
            thumb = self.clips_dir / f"{name}.jpg"
            try:
                render.thumbnail(out, busy - work.start_s, thumb)
            except MediaError:
                try:
                    render.thumbnail(out, min(work.end_s - work.start_s - 0.1, render.TITLE_S + 0.2), thumb)
                except MediaError as exc:
                    log.warning("thumbnail of %s failed: %s", name, exc)
            debug = self.clips_dir / f"{name}_debug.jpg"
            try:
                self.debug_image(work, plan, busy, debug)
            except (MediaError, OSError, ValueError) as exc:
                log.warning("debug image of %s failed: %s", name, exc)
                debug = None
            clips.append(ClipResult(
                index=work.rank, path=out, thumb=thumb, start_s=work.start_s, end_s=work.end_s, title=m.title,
                category=m.category, label=m.label, score=m.score, debug=debug, team=m.team, player=m.player,
                evidence=m.evidence, transcript=moments.transcript_between(turns, work.start_s, work.end_s),
                follow=work.follow))
        self.say("render", 1.0, f"Rendered {len(clips)} clips")
        self.timed("render", started)
        if works and not clips:
            raise PipelineError("No clip could be rendered: " + (errors[0] if errors else "unknown error"))
        return clips

    def busiest(self, work: follow.ClipWork, loud: loudness_mod.Loudness) -> float:
        """The clip's busiest moment: its loudest half second after the title card (else the play)."""
        lo = work.start_s + render.TITLE_S + 0.3
        hi = work.end_s - 0.3
        if hi <= lo:
            return max(work.start_s, min(work.end_s - 0.1, work.start_s + (work.end_s - work.start_s) / 2))
        if len(loud.z):
            a, b = loud.index(lo), loud.index(hi) + 1
            if b > a:
                best = a + int(np.argmax(loud.smooth[a:b]))
                return float(min(hi, max(lo, loud.time(best))))
        return float(min(hi, max(lo, work.moment.play_s)))

    def debug_image(self, work: follow.ClipWork, plan: tracking.CropPlan, at: float, out: Path) -> None:
        i = int(round((at - work.start_s) * plan.fps))
        left = float(plan.left[min(max(0, i), len(plan.left) - 1)]) if len(plan.left) else 0.0
        ball_box = None
        players: list[tuple[float, float, float, float]] = []
        if work.facts is not None:
            k = int(round((at - work.window[0]) * (work.facts.fps or self.options.sam_fps)))
            w, h = work.facts.width, work.facts.height
            if work.ball is not None and w and h:
                track = tracking.pick_ball(work.ball)
                for d in (0, -1, 1, -2, 2, -3, 3):
                    if 0 <= k + d < len(track.x) and track.found[k + d]:
                        cx = track.x[k + d] * w
                        near = [b for b in work.ball.boxes if b.frame == k + d]
                        if near:
                            b = min(near, key=lambda b: abs((b.left + b.right) / 2 - cx))
                            ball_box = (b.left / w, b.top / h, b.right / w, b.bottom / h)
                        break
            if work.people is not None and w and h:
                players = [(b.left / w, b.top / h, b.right / w, b.bottom / h) for b in work.people.boxes
                           if b.frame == k and (b.bottom - b.top) / h >= 0.1]
        render.debug_image(self.source, at, out, self.tmp, window_left=left, window=plan.window, ball=ball_box,
                           players=players, note=plan.note)

    def stats(self, picks: list[moments.Moment], works: list[follow.ClipWork]) -> dict:
        after = self._spent()
        if after is not None and self.spent_before is not None:
            cost = {model: round(after.get(model, 0.0) - self.spent_before.get(model, 0.0), 6)
                    for model in (MODEL_VOICE, MODEL_SPARK, MODEL_SAM)}
        else:  # a MetaAPI that doesn't count: estimate what can be
            from gameday.meta_api.costs import sam_cost, voice_cost

            cost = {MODEL_VOICE: voice_cost(self.voice_seconds), MODEL_SPARK: 0.0, MODEL_SAM: sam_cost(self.sam_frames)}
        return {
            "seconds": self.seconds,
            "cost_usd": cost,
            "fallbacks": self.fallbacks,
            "moments": len(picks),
            "sam_frames": self.sam_frames,
            "follow": {str(w.rank): (w.follow.source if w.follow else "centre") for w in works},
        }


def run(job_id: int, source: Path, work_dir: Path, options: PipelineOptions, meta: MetaAPI | None,
        progress: ProgressFn, should_stop: StopFn) -> PipelineResult:
    """The Pipeline in gameday.types. Only PipelineError and JobCancelled escape."""
    job = _Run(job_id, source, work_dir, options, meta, progress, should_stop)
    try:
        return job.go()
    except (PipelineError, JobCancelled):
        raise
    except Exception as exc:  # noqa: BLE001 - anything else is reported, not raised raw
        log.exception("pipeline failed")
        raise PipelineError(f"The clips couldn't be made: {_why(exc)}") from exc
    finally:
        if os.environ.get("GAMEDAY_KEEP_TMP", "0") != "1":
            shutil.rmtree(job.tmp, ignore_errors=True)
