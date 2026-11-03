"""One approved clip, published to the platforms the phone chose.

Ported from this week's event branch. Called by the relay loop
(gameday/worker.py) for a publish job, one upload at a time. A restart in the
middle of an upload leaves the job 'interrupted', and it is never sent again
by itself.

Before anything leaves this app the job is checked against its offer once
more: the offer is still in a publishing state, the job's command is the one
that approved it, and the file is byte-for-byte the size and mtime that was
offered. Then each platform in order, Instagram then Facebook. Each
platform's outcome is written to clip_publishes (provider "meta",
request_id = the approval id) as it happens, so a platform that already
succeeded is never posted again by a retry.
"""

import logging
from pathlib import Path

from gameday.publish.errors import PublishCancelled, PublishError
from gameday.remote_approval import captions, states, store, summary, usage
from gameday.remote_approval import settings as ra_settings

log = logging.getLogger("gameday")


def make_graph(config: dict, device: dict):
    """The Graph client for a publish: through this app's relay. Tests and the
    demo replace it."""
    from gameday.publish.meta_graph import MetaGraph
    from gameday.remote_approval.relay import Relay

    return MetaGraph(Relay(config["relay_url"], **device))


def run(worker, db, job, request_data: dict) -> None:
    """`worker` gives emit(job_id, clip_id, **info) for progress and
    `cancelled`, the set of publish job ids asked to stop."""
    job_id = job["id"]
    review = store.get_review(db, int(request_data.get("review_id") or 0))
    if review is None or review["approval_id"] != request_data.get("approval_id"):
        raise PublishError("This upload's approval could not be found. Nothing was posted.")
    if review["decision"].get("command_id") != request_data.get("command_id"):
        raise PublishError("This upload does not match the approval on record. Nothing was posted.")
    if review["state"] not in (states.PUBLISH_QUEUED, states.PUBLISHING):
        raise PublishError("This offer is no longer approved for publishing. Nothing was posted.")
    entry = next((c for c in review["clips"] if c["clip_id"] == job["clip_id"]), None)
    clip = db.get_clip(job["clip_id"])
    if entry is None or clip is None:
        raise PublishError("That clip no longer exists. Nothing was posted.")
    if not summary.file_matches(entry):
        raise PublishError("This clip changed after it was approved, so it was not posted. "
                           "Ask for a new offer from the app.")
    if review["state"] == states.PUBLISH_QUEUED:
        try:
            store.move(db, review["id"], states.PUBLISHING, actor="publisher")
        except states.IllegalMove:
            # Another clip of the same offer started uploading first.
            pass

    config = ra_settings.load(db)
    device = ra_settings.device(db)
    if not device or not config["relay_url"]:
        raise PublishError("This app is no longer connected to your relay. Nothing was posted.")
    graph = make_graph(config, device)
    ctx = {"command_id": review["decision"]["command_id"], "approval_id": review["approval_id"],
           "clip_id": int(job["clip_id"])}
    text = captions.texts_for(review, clip, entry)
    path = Path(entry["path"])
    platforms = [p for p in ("instagram", "facebook") if p in request_data.get("platforms", [])]

    def progress(platform: str, fraction: float, message: str) -> None:
        worker.emit(job_id, job["clip_id"], phase="upload", fraction=fraction, message=message,
                    provider="meta", platform=platform)

    def cancelled() -> bool:
        return job_id in worker.cancelled

    outcomes: dict[str, dict] = {}
    for i, platform in enumerate(platforms):
        share = 0.05 + 0.9 * i / max(1, len(platforms))
        previous = _published(db, job["clip_id"], platform)
        if previous:
            outcomes[platform] = previous
            continue
        _record(db, review, clip, platform, "processing")
        progress(platform, share, f"Publishing to {ra_settings.PLATFORM_LABELS[platform]}")

        def on_progress(label, done, total, platform=platform, share=share):
            progress(platform, share, label)

        try:
            if platform == "instagram":
                posted = graph.instagram(ctx, path, text["instagram"], should_cancel=cancelled,
                                         on_progress=on_progress)
            else:
                posted = graph.facebook(ctx, path, text["title"], text["facebook"], should_cancel=cancelled,
                                        on_progress=on_progress)
        except PublishCancelled:
            worker.cancelled.discard(job_id)
            outcomes[platform] = _record(db, review, clip, platform, "failed",
                                         error="Cancelled before it was posted.")
            break
        except PublishError as e:
            outcomes[platform] = _record(db, review, clip, platform, "failed", error=e.message)
            continue
        except Exception as e:  # never a raw exception on the dashboard or the phone
            log.warning("A Meta publish step failed (%s).", type(e).__name__)
            outcomes[platform] = _record(db, review, clip, platform, "failed",
                                         error=f"Something went wrong publishing to "
                                               f"{ra_settings.PLATFORM_LABELS[platform]}. Nothing was "
                                               "confirmed there.")
            continue
        usage.record_graph(db, uploads=1, size=entry["size"])
        outcomes[platform] = _record(db, review, clip, platform, posted.state, post_id=posted.post_id,
                                     url=posted.url, error=" ".join(posted.warnings))

    ok = [p for p, o in outcomes.items() if o["state"] == "published"]
    bad = {p: o for p, o in outcomes.items() if o["state"] != "published"}
    message = "; ".join(f"{ra_settings.PLATFORM_LABELS[p]}: {o['error'] or o['state']}" for p, o in bad.items())
    db.finish_publish_job(job_id, "done" if ok else "failed", error=message)
    worker.emit(job_id, job["clip_id"], phase="done" if ok else "failed", fraction=1.0 if ok else 0,
                provider="meta", terminal="done" if ok else "failed",
                message=(f"Published to {', '.join(ra_settings.PLATFORM_LABELS[p] for p in ok)}"
                         if ok else message or "Nothing was published."),
                error=message, results=outcomes)


def _published(db, clip_id: int, platform: str) -> dict | None:
    row = db.conn.execute(
        "SELECT * FROM clip_publishes WHERE clip_id = ? AND platform = ? AND provider = 'meta' "
        "AND state = 'published'", (int(clip_id), platform),
    ).fetchone()
    if row is None:
        return None
    return {"state": "published", "post_id": row["post_id"], "url": row["post_url"], "error": "",
            "platform": platform}


def _record(db, review: dict, clip, platform: str, state: str, *, post_id: str = "", url: str = "",
            error: str = "") -> dict:
    fields = {
        "provider": "meta",
        "video_id": clip["video_id"],
        "start_s": clip["start_s"],
        "end_s": clip["end_s"],
        "state": state,
        "post_id": post_id,
        "post_url": url,
        "error": error[:500],
        "request_id": review["approval_id"],
    }
    if state == "processing":
        fields["created_at"] = store.stamp()
    db.record_clip_publish(int(clip["id"]), platform, fields)
    store.audit(db, review["id"], "publisher", f"{platform}_{state}",
                f"clip {clip['id']}" + (f": {error[:120]}" if error and state != "published" else ""))
    return {"state": state, "post_id": post_id, "url": url, "error": error, "platform": platform}
