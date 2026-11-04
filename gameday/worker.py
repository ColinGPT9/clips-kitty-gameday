"""The app's three background threads, and the steps they run.

* The folder watcher, every 10 s (watch/watcher.py): a new video file that
  has stopped changing for a minute gets one job.
* The pipeline, one job at a time: the engine (gameday.engine.pipeline.run)
  in DATA_DIR/media/jobs/<id>, its progress written into the job's stages,
  then the "captions" stage: when phone approval is on and the relay is
  registered, the offer is made (summary, Muse Spark captions) for the relay
  loop to send. A game uploaded with the judge login skips it: it is never
  offered on the owner's phone (access.py).
* The relay and publish loop, every 2 s: one remote approval pass
  (remote_approval/service.py tick), then at most one upload.

At start (recover), a job a restart cut off becomes failed ("The app
restarted during this job") and can be retried; an upload a restart cut off
becomes interrupted and is never sent again by itself.

Each step is a plain method (run_next_job, relay_step, publish_one,
watcher.pass_once), so the tests drive them without threads.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import logging
import threading
import time
from pathlib import Path

from gameday import access
from gameday import secrets as secret_store
from gameday.config import Config
from gameday.store import Store, fresh_stages, loads
from gameday.types import STAGE_LABELS, STAGES, JobCancelled, MetaAPI, PipelineError, PipelineOptions
from gameday.watch.watcher import Watcher

log = logging.getLogger("gameday")

WATCH_SECONDS = 10
RELAY_SECONDS = 2
IDLE_SECONDS = 2
#: Progress is written at most this often, and at every change of stage.
PROGRESS_SECONDS = 0.5


def make_meta_factory(store: Store, config: Config):
    """A MetaFactory: gameday.meta_api.MetaClient with the key in use (the
    environment's, else the saved one) and the store's ledger; None without
    a key."""

    def factory() -> MetaAPI | None:
        key, _from_env = secret_store.meta_key(config, store.secrets)
        if not key:
            return None
        from gameday.meta_api import MetaClient

        params = inspect.signature(MetaClient).parameters
        if "ledger" in params:
            return MetaClient(key, ledger=store.ledger())
        return MetaClient(key)

    return factory


def default_pipeline():
    from gameday.engine.pipeline import run

    return run


class _Progress:
    """The pipeline's progress callback: the job's stages, timed, written to
    the store at most every PROGRESS_SECONDS and at every change of stage."""

    def __init__(self, store: Store, job_id: int, *, clock=time.monotonic):
        self.store = store
        self.job_id = job_id
        self.clock = clock
        self.stages = fresh_stages()
        self.current: str | None = None
        self._since = 0.0
        self._spent: dict[str, float] = {}
        self._written = 0.0

    def __call__(self, stage: str, fraction: float, message: str) -> None:
        if stage not in STAGES:
            return
        now = self.clock()
        changed = stage != self.current
        if changed:
            self.close_current("done")
            self.current = stage
            self._since = now
            self.stages[stage]["state"] = "running"
        self.stages[stage]["seconds"] = round(self._spent.get(stage, 0.0) + now - self._since, 1)
        if changed or now - self._written >= PROGRESS_SECONDS:
            self._written = now
            try:
                fraction = max(0.0, min(1.0, float(fraction)))
            except (TypeError, ValueError):
                fraction = 0.0
            self.store.update_job(self.job_id, stage=stage, stage_progress=fraction,
                                  message=str(message or STAGE_LABELS[stage])[:200], stages=self.stages)

    def close_current(self, state: str, note: str = "") -> None:
        if self.current is None:
            return
        stage = self.current
        spent = self._spent.get(stage, 0.0) + self.clock() - self._since
        self._spent[stage] = spent
        self.stages[stage].update(state=state, seconds=round(spent, 1))
        if note:
            self.stages[stage]["note"] = note[:300]
        self.current = None

    def set(self, stage: str, state: str, note: str = "") -> None:
        self.stages[stage]["state"] = state
        if note:
            self.stages[stage]["note"] = note[:300]

    def skip_waiting(self, upto: tuple[str, ...]) -> None:
        for stage in upto:
            if self.stages[stage]["state"] == "waiting":
                self.stages[stage]["state"] = "skipped"

    def save(self, **fields) -> None:
        self.store.update_job(self.job_id, stages=self.stages, **fields)


class Worker:
    """The background threads over one store. `pipeline` and `meta_factory`
    are the seams tests fill with fakes."""

    def __init__(self, store: Store, config: Config, *, pipeline=None, meta_factory=None, feed=None,
                 probe=None):
        self.store = store
        self.config = config
        self._pipeline = pipeline
        self.meta_factory = meta_factory or make_meta_factory(store, config)
        self.watcher = Watcher(store, feed=feed, probe=probe)
        from gameday.remote_approval.service import TickState

        self.relay_state = TickState()
        #: Publish job ids asked to stop (remote_approval/publishing.py reads it).
        self.cancelled: set[int] = set()
        self.current_job: int | None = None
        self._stop = threading.Event()
        self._wake_jobs = threading.Event()
        self._wake_relay = threading.Event()
        self._threads: list[threading.Thread] = []
        self._relay_error = ""

    # ---- threads ------------------------------------------------------------

    def start(self) -> None:
        self.recover()
        self._stop.clear()
        for name, loop in (("gameday-watcher", self._watch_loop), ("gameday-pipeline", self._pipeline_loop),
                           ("gameday-relay", self._relay_loop)):
            thread = threading.Thread(target=loop, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake_jobs.set()
        self._wake_relay.set()
        for thread in self._threads:
            thread.join(timeout)
        self._threads = []
        self.store.close_all()

    @property
    def running(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    def wake_jobs(self) -> None:
        self._wake_jobs.set()

    def wake_relay(self) -> None:
        """Talk to the relay on the next turn of the loop instead of at its
        own pace: a setting changed, an offer was cancelled from the app."""
        self.relay_state.next_pass = 0.0
        self.relay_state.next_status = 0.0
        self._wake_relay.set()

    def _watch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.watcher.pass_once():
                    self.wake_jobs()
            except Exception:
                log.exception("The folder watcher's pass failed.")
            self._stop.wait(WATCH_SECONDS)
        self.store.close()

    def _pipeline_loop(self) -> None:
        while not self._stop.is_set():
            try:
                ran = self.run_next_job()
            except Exception:
                log.exception("The pipeline loop failed.")
                ran = None
            if ran is None:
                self._wake_jobs.wait(IDLE_SECONDS)
                self._wake_jobs.clear()
        self.store.close()

    def _relay_loop(self) -> None:
        while not self._stop.is_set():
            try:
                busy = self.relay_step()
            except Exception:
                log.exception("The relay loop failed.")
                busy = False
            if not busy:
                self._wake_relay.wait(RELAY_SECONDS)
                self._wake_relay.clear()
        self.store.close()

    # ---- start ----------------------------------------------------------------

    def recover(self) -> None:
        for job_id in self.store.recover_running_jobs():
            self.store.add_event("warn", "job", f"Job {job_id} was running when the app stopped. Retry it to "
                                                "run it again.", job_id=job_id)
        if self.store.recover_running_publish_jobs():
            self.store.add_event("warn", "publish", "An upload was cut off when the app stopped. It is not sent "
                                                    "again by itself: check the account before retrying.")

    # ---- the pipeline -----------------------------------------------------------

    def meta(self) -> MetaAPI | None:
        try:
            return self.meta_factory()
        except Exception as e:
            log.warning("The Meta client couldn't be made (%s).", type(e).__name__)
            return None

    def pipeline(self):
        if self._pipeline is None:
            self._pipeline = default_pipeline()
        return self._pipeline

    def run_next_job(self) -> int | None:
        """Claim and run the oldest queued job. Its id, or None when none was queued."""
        job = self.store.claim_next_job()
        if job is None:
            return None
        self.current_job = int(job["id"])
        try:
            self.run_job(job)
        finally:
            self.current_job = None
        return int(job["id"])

    def run_job(self, job) -> str:
        """One job, start to finish. Returns its final state."""
        store, job_id = self.store, int(job["id"])
        video = store.get_video(job["video_id"])
        name = video["name"] if video is not None else f"video {job['video_id']}"
        options = _options(loads(job["options"], {}))
        work_dir = self.config.job_dir(job_id)
        progress = _Progress(store, job_id)
        store.add_event("info", "job", f"Started job {job_id}: {name}", job_id=job_id)
        should_stop = _StopCheck(store, job_id)
        meta = self.meta()
        try:
            if video is None or not Path(video["path"]).is_file():
                raise PipelineError("The video file is no longer there.")
            work_dir.mkdir(parents=True, exist_ok=True)
            result = self.pipeline()(job_id, Path(video["path"]), work_dir, options, meta, progress, should_stop)
        except JobCancelled:
            return self._end(job_id, progress, "cancelled", "Cancelled", note="Cancelled")
        except PipelineError as e:
            return self._failed(job, progress, str(e) or "The video couldn't be processed.")
        except Exception as e:
            log.exception("Job %s failed in the pipeline.", job_id)
            return self._failed(job, progress, f"Something went wrong while clipping ({type(e).__name__}).")

        progress.close_current("done")
        progress.skip_waiting(tuple(s for s in STAGES if s != "captions"))
        saved = self._save_clips(job, result)
        if video is not None and result.duration_s:
            store.set_video_duration(video["id"], result.duration_s)
        progress.save(clips=saved, warnings=[str(w) for w in result.warnings][:20],
                      stats=_jsonable(result.stats))
        if should_stop():
            return self._end(job_id, progress, "cancelled", "Cancelled after the clips were made")
        self._captions_stage(job_id, meta, progress, saved)
        message = f"{saved} clip{'s' if saved != 1 else ''} ready"
        self._end(job_id, progress, "done", message)
        store.add_event("info", "job", f"Job {job_id} is done: {message}.", job_id=job_id)
        return "done"

    def _end(self, job_id: int, progress: _Progress, state: str, message: str, *, note: str = "",
             error: str = "") -> str:
        stage = progress.current
        progress.close_current("failed" if state == "failed" else ("skipped" if state == "cancelled" else "done"),
                               note=note)
        progress.save(state=state, message=message[:200], error=error[:500], finished_at=time.time(),
                      cancel_requested=0, stage=None if state == "done" else stage)
        if state == "cancelled":
            self.store.add_event("info", "job", f"Job {job_id} was cancelled.", job_id=job_id)
        return state

    def _failed(self, job, progress: _Progress, error: str) -> str:
        job_id = int(job["id"])
        self._end(job_id, progress, "failed", error, error=error)
        self.store.add_event("error", "job", f"Job {job_id} failed: {error}", job_id=job_id)
        try:
            from gameday.remote_approval import notifier

            # A database write only; the relay loop sends it. Never fails the job further.
            notifier.on_job_finished(self.store, job, "failed")
        except Exception as e:
            log.warning("Couldn't queue the failure message (%s).", type(e).__name__)
        return "failed"

    def _save_clips(self, job, result) -> int:
        saved = 0
        for clip in sorted(result.clips, key=lambda c: c.index):
            path = Path(clip.path)
            if not path.is_file():
                log.warning("Job %s: clip %s has no file, so it was left out.", job["id"], clip.index)
                continue
            follow = None
            if clip.follow is not None:
                follow = {"source": clip.follow.source, "ball_coverage": float(clip.follow.ball_coverage or 0),
                          "note": clip.follow.note or "", "frames": int(clip.follow.frames or 0)}
            self.store.add_clip(
                job["id"], job["video_id"], idx=clip.index, path=str(path.resolve()), start_s=clip.start_s,
                end_s=clip.end_s, title=clip.title or "", category=clip.category or "other",
                label=clip.label or "", score=clip.score or 0, team=clip.team or "", player=clip.player or "",
                evidence=clip.evidence or "", transcript=clip.transcript or "",
                # Only files that were made: a thumbnail that failed to render is no poster.
                thumb=str(Path(clip.thumb).resolve()) if clip.thumb and Path(clip.thumb).is_file() else "",
                debug=str(Path(clip.debug).resolve()) if clip.debug and Path(clip.debug).is_file() else "",
                follow=follow)
            saved += 1
        return saved

    # ---- the captions stage: the offer on the phone ----------------------------

    def _captions_stage(self, job_id: int, meta: MetaAPI | None, progress: _Progress, clips: int) -> None:
        if access.made_by_judge(self.store.get_job(job_id) or {}):
            progress.set("captions", "skipped", access.NOT_OFFERED)
            return
        progress("captions", 0.0, "Getting the clips ready for your phone")
        try:
            state, note = self._offer(job_id, meta, progress, clips)
        except Exception as e:
            log.exception("Job %s: the offer couldn't be made.", job_id)
            state, note = "failed", f"The offer couldn't be made ({type(e).__name__})."
        progress.close_current(state, note=note)
        if state == "done":
            self.store.add_event("info", "phone", note, job_id=job_id)
            self.wake_relay()

    def _offer(self, job_id: int, meta: MetaAPI | None, progress: _Progress, clips: int) -> tuple[str, str]:
        from gameday.remote_approval import notifier, service, summary
        from gameday.remote_approval import settings as ra_settings
        from gameday.remote_approval import store as ra_store

        if not clips:
            return "skipped", "No clips to offer."
        config = ra_settings.load(self.store)
        why = notifier.why_not_offered(config)
        if why:
            return "skipped", why
        if service.client(self.store) is None:
            return "skipped", "Connect this app to your relay to get its clips on your phone."
        job = self.store.get_job(job_id)
        if config["basketball_only"] and summary.kind_of(loads(job["options"], {})) != "basketball":
            return "skipped", ("Only basketball games are offered on your phone. "
                               f"{notifier.WHERE}, tick Offer other videos too under Publishing.")
        if config["muse_captions"] or config["muse_summary"]:
            progress("captions", 0.2, "Muse Spark is writing the captions")
        review_id = notifier.on_job_finished(self.store, job, "done", meta=meta)
        if review_id is None:
            return "skipped", "Nothing was offered: the clips are already posted, or already offered."
        review = ra_store.get_review(self.store, review_id)
        offered = len(review["clips"])
        words = f"Offered {offered} clip{'s' if offered != 1 else ''} on your phone."
        if review.get("captions"):
            return "done", f"Muse Spark wrote the captions. {words}"
        if not (config["muse_captions"] or config["muse_summary"]):
            return "done", words
        if meta is None:
            return "done", f"{words} No Meta key is saved, so the clips' own titles are the captions."
        skipped = next((r["detail"] for r in ra_store.audit_log(self.store, review_id, 50)
                        if r["action"] == "muse_spark_skipped"), "")
        return "done", f"{words} Muse Spark couldn't write the captions ({skipped or 'no answer'}), so the " \
                       "clips' own titles are used."

    # ---- the relay loop and uploads ---------------------------------------------

    def relay_step(self) -> bool:
        """One remote approval pass, then at most one upload. True when an
        upload ran (the loop goes again at once)."""
        from gameday.remote_approval import service

        try:
            service.tick(self.store, self.relay_state, emit=self._relay_event)
        except Exception:
            log.exception("The remote approval pass failed.")
        return self.publish_one()

    def _relay_event(self, event: dict) -> None:
        if event.get("type") == "relay_error":
            message = str(event.get("message") or "")
            if message and message != self._relay_error:
                self.store.add_event("warn", "phone", f"Relay: {message}")
            self._relay_error = message

    def publish_one(self) -> bool:
        """Run the oldest queued upload. False when there was none."""
        from gameday.publish.errors import PublishError
        from gameday.remote_approval import publishing

        job = self.store.claim_next_publish_job()
        if job is None:
            return False
        try:
            request = json.loads(job["request"] or "{}")
            if request.get("provider") != "meta":
                raise PublishError("This upload is not one this app knows how to send.")
            publishing.run(self, self.store, job, request)
        except Exception as e:
            if isinstance(e, PublishError):
                message = e.message
            else:
                log.exception("Upload %s failed.", job["id"])
                message = "Something went wrong with this upload. Nothing was confirmed."
            self.store.finish_publish_job(job["id"], "failed", error=message)
            self.store.add_event("warn", "publish", f"Upload of clip {job['clip_id']} failed: {message}")
        return True

    def emit(self, job_id: int, clip_id: int, **info) -> None:
        """Progress from remote_approval/publishing.py: the end of each upload
        goes on the activity list."""
        if not info.get("terminal"):
            return
        ok = info.get("terminal") == "done"
        self.store.add_event("info" if ok else "warn", "publish",
                             f"Clip {clip_id}: {info.get('message') or ('published' if ok else 'not published')}")


class _StopCheck:
    """should_stop for the pipeline: the job's cancel flag, read at most twice a second."""

    def __init__(self, store: Store, job_id: int):
        self.store = store
        self.job_id = job_id
        self._at = 0.0
        self._answer = False

    def __call__(self) -> bool:
        if self._answer:
            return True
        now = time.monotonic()
        if now - self._at >= 0.5:
            self._at = now
            row = self.store.get_job(self.job_id)
            self._answer = bool(row is not None and row["cancel_requested"])
        return self._answer


def _options(saved: dict) -> PipelineOptions:
    names = {f.name for f in dataclasses.fields(PipelineOptions)}
    return PipelineOptions(**{k: v for k, v in saved.items() if k in names})


def _jsonable(value) -> dict:
    try:
        return json.loads(json.dumps(value, default=str)) if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}
