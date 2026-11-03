"""The worker's one call into remote approval, in the job's "captions" stage.

Ported from this week's event branch (where the desktop worker's announce
step called it). What happens here is database writes, a look at the clip
files, and at most one Muse Spark request for the captions: never a relay
call. The pipeline thread must never wait on WhatsApp, and a relay that is
down must never fail or slow a job. The relay loop (service.tick) sends what
this queues.

Each job is one run of one video, so the clips offered are exactly that
job's clips (clips_of_job).

Duplicate-safe by construction: the outbox key is the job id, so the same
job announced twice queues one message, and an offer is only made once per
job unless the owner asks for a new one.
"""

from datetime import timedelta
from pathlib import Path

from gameday.remote_approval import captions, states, store, summary
from gameday.remote_approval import settings as ra_settings
from gameday.store import loads

WHERE = "In the dashboard's Setup"


def why_not_offered(config: dict) -> str:
    """Why a finished game is not offered on the phone, named as the
    dashboard names its switches, or "" when nothing in the settings stops it.
    An offer reaches the phone on WhatsApp or through the owner's Muse agent."""
    if not (config["whatsapp_enabled"] or ra_settings.muse_connected(config)):
        return f"Nothing was sent to your phone. {WHERE}, make a Muse key or turn on Send me WhatsApp messages."
    if not config["notify_on_finish"]:
        return (f"Nothing was sent to your phone. {WHERE}, turn on When a game's clips are ready "
                "(under Send me WhatsApp messages).")
    return ""


def job_title(db, job) -> str:
    """The game's name: its file's name without the extension."""
    video = db.get_video(job["video_id"]) if job is not None else None
    return Path(str(video["name"])).stem if video is not None else ""


def on_job_finished(db, job, status: str, *, meta=None) -> int | None:
    """Queue what this finished job should tell the owner. Returns the new
    offer's id, or None when nothing was offered (neither WhatsApp nor Muse
    on, a video the settings leave out, nothing found, or a failure notice
    instead). `meta` (a MetaAPI) writes the captions when that is on."""
    config = ra_settings.load(db)
    # Offers reach the owner on WhatsApp, or through their Muse agent with
    # WhatsApp off (the relay keeps them for it). Either one is enough.
    whatsapp = bool(config["whatsapp_enabled"])
    if not (whatsapp or ra_settings.muse_connected(config)):
        return None
    row = db.get_job(job["id"]) or job
    options = loads(row["options"], {})
    kind = summary.kind_of(options)
    # Basketball only, unless the owner opted in to other videos.
    if config["basketball_only"] and kind != "basketball":
        return None

    if status == "failed":
        if config["notify_on_fail"] and whatsapp:
            # Never the error itself: it can name paths, and the phone is not
            # where it is read. The dashboard shows it. A WhatsApp message
            # only: there is no offer for Muse to find.
            store.enqueue(db, f"failed:job:{row['id']}", "failed", body={"job_id": int(row["id"])})
        return None
    if status != "done" or not config["notify_on_finish"]:
        return None
    if any(r["state"] not in states.SETTLED for r in store.reviews_for_job(db, row["id"])):
        return None  # already offered and still open: an announce repeated
    return offer(db, job_id=int(row["id"]), video_id=int(row["video_id"]), title=job_title(db, row),
                 clips=clips_of_job(db, row["id"]), config=config, kind=kind, meta=meta,
                 hint=str(options.get("hint") or ""))


def clips_of_job(db, job_id: int) -> list:
    """The clips this job made whose files are still on disk."""
    return [c for c in db.clips_for_job(int(job_id)) if summary.offerable(c)]


def published_on(db, clips) -> dict[int, list[str]]:
    """Where each clip is already published, in the order the platforms are
    offered: {clip_id: ["instagram", ...]}."""
    out = {}
    for clip in clips:
        done = store.published_platforms(db, int(clip["id"]))
        mine = [p for p in ra_settings.PLATFORMS if p in done]
        if mine:
            out[int(clip["id"])] = mine
    return out


def offer(db, *, job_id: int, video_id: int, title: str, clips, config: dict | None = None,
          round_no: int | None = None, kind: str = "basketball", meta=None, hint: str = "") -> int | None:
    """Make an offer of these clips and queue its message.

    Also what "Ask again" calls after an offer expired: a new offer with a
    new link, never the old link brought back. A clip already posted, or on
    its way, everywhere an offer can send it is left out; one published to
    some of those places is offered with a note of where (summary.snapshot),
    and is never sent there again (commands.queue_publishes).

    With `meta`, Muse Spark writes the captions before the message is queued,
    so the relay loop sends them with the offer."""
    config = config or ra_settings.load(db)
    clips = list(clips)
    posted = published_on(db, clips)
    every = set(ra_settings.PLATFORMS)
    clips = [c for c in clips if not every <= set(store.sent_platforms(db, int(c["id"])))]
    snap = summary.snapshot(clips, limit=int(config["max_clips"]), kind=kind, published=posted)
    if not snap:
        store.audit(db, 0, "desktop", "nothing_to_offer",
                    f"job {job_id}: no clips left to offer (none on disk, or all posted already)")
        return None
    info = summary.build(title, clips, snap, kind=kind)
    expires = store.now_utc() + timedelta(hours=int(config["approval_hours"]))
    review_id = store.create_review(
        db, job_id=job_id, video_id=video_id, title=info["title"], clips=snap,
        fingerprint=summary.fingerprint(snap), summary=info, expires_at=store.stamp(expires),
    )
    store.audit(db, review_id, "desktop", states.READY_FOR_REVIEW.lower(), summary.ready_text(info))
    if meta is not None and (config["muse_captions"] or config["muse_summary"]):
        write_captions(db, review_id, meta, hint=hint)
    nth = round_no if round_no is not None else len(store.reviews_for_job(db, job_id))
    store.enqueue(db, f"ready:job:{job_id}:{nth}", "ready", review_id=review_id)
    store.move(db, review_id, states.NOTIFICATION_PENDING, actor="desktop")
    return review_id


def write_captions(db, review_id: int, meta, *, hint: str = "") -> str:
    """Muse Spark's captions for an offer, kept on it. "" when they were
    written, else why not: the offer then goes with the clips' own text."""
    review = store.get_review(db, review_id)
    rows = {c["clip_id"]: db.get_clip(c["clip_id"]) for c in review["clips"]}
    try:
        captions.generate(db, review, rows, meta=meta, hint=hint)
    except Exception as e:  # the clips' own text is used; an offer never waits on a model
        message = (getattr(e, "message", "") or str(e) or "Muse Spark was not available.")[:200]
        store.audit(db, review_id, "desktop", "muse_spark_skipped", message)
        return message
    return ""
