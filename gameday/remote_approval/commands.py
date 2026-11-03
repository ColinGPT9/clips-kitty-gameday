"""What a decision made on the phone is allowed to do, checked here (ported from the event branch).

The relay already checked the link, the PIN and the expiry before it handed
a command over. This module checks again, against this PC's own record of
the offer, because the PC is the one about to post a video in the
creator's name and a relay is a server on the internet:

* the command is signed with this PC's device secret (relay.verify_command);
* it has not been seen before (remote_commands): a command delivered twice
  is acknowledged twice and applied once;
* it names an offer this PC made, by the relay's approval id AND this PC's
  own id for it, so an approval can only ever apply to the job it was for;
* the clips' fingerprint matches the offer's, and every chosen clip is
  still the same file on disk, so an old approval never applies to new or
  re-rendered clips;
* it was decided before the offer expired, and is not stale (a PC that was
  off for days does not post on its return without asking again);
* remote approval is still switched on here, and each platform is one this
  creator connected.

A generic "yes" cannot pass: an approve names its offer, its fingerprint,
its clips and its platforms, or it is refused.

A defer or cancel asked of the creator's Muse agent carries via "muse",
inside what the relay signed, and is logged as Muse's. An approve never
comes from Muse (the relay has no way to make one), and one that says it
does is refused here as well.
"""

import json
from datetime import timedelta

from gameday.remote_approval import relay as relay_mod
from gameday.remote_approval import settings as ra_settings
from gameday.remote_approval import states, store, summary

#: An approval older than this is not acted on, even if it was made in time:
#: the creator approved for then, not for whenever the PC comes back.
MAX_COMMAND_AGE = timedelta(hours=48)

KINDS = ("approve", "cancel", "defer", "reminded", "delivery", "expired")


class Outcome:
    def __init__(self, outcome: str, reason: str = "", *, queued: int = 0, review_id: int = 0):
        self.outcome = outcome   # accepted | rejected | ignored
        self.reason = reason
        self.queued = queued
        self.review_id = review_id

    def __repr__(self):
        return f"Outcome({self.outcome!r}, {self.reason!r}, queued={self.queued})"


def handle(db, command: dict, *, secret: str, now=None) -> Outcome:
    """Check one command and apply it if it passes. Never raises for a bad
    command: the outcome says why, and is what the relay is told."""
    now = now or store.now_utc()
    cid = str(command.get("id") or "")
    kind = str(command.get("kind") or "")
    if not cid:
        return Outcome("rejected", "The command has no id.")
    seen = store.seen_command(db, cid)
    if seen:
        return Outcome(seen["outcome"], seen["reason"], review_id=seen["review_id"])
    if not relay_mod.verify_command(secret, command):
        # Not recorded under its id: a forged command must not be able to
        # block the real one with the same id.
        return Outcome("rejected", "The command's signature did not check out.")
    review = store.review_for_approval(db, str(command.get("approval_id") or ""))
    if review is None or int(command.get("review_ref") or 0) != review["id"]:
        return _record(db, cid, 0, kind, Outcome("rejected", "That offer is not one this PC made."))
    if kind not in KINDS:
        return _record(db, cid, review["id"], kind, Outcome("ignored", f"Unknown command {kind!r}."))
    handler = {
        "approve": _approve, "cancel": _cancel, "defer": _defer, "reminded": _reminded,
        "delivery": _delivery, "expired": _expired,
    }[kind]
    try:
        result = handler(db, review, command, now)
    except states.IllegalMove as e:
        result = Outcome("ignored", _already(e.current))
    result.review_id = review["id"]
    return _record(db, cid, review["id"], kind, result, via=str(command.get("via") or ""))


def _record(db, cid: str, review_id: int, kind: str, result: Outcome, *, via: str = "") -> Outcome:
    store.record_command(db, cid, review_id=review_id, kind=kind, outcome=result.outcome, reason=result.reason)
    if review_id:
        store.audit(db, review_id, _actor(kind, via), f"{kind}_{result.outcome}", result.reason)
    return result


def _actor(kind: str, via: str) -> str:
    """Who decided: the creator's Muse agent or the phone (the approval page,
    a WhatsApp button) for a decision, the relay for everything else."""
    if kind not in ("approve", "cancel", "defer"):
        return "relay"
    return "muse" if via == "muse" else "phone"


def _already(state: str) -> str:
    return {
        states.APPROVED: "This offer was already approved.",
        states.PUBLISH_QUEUED: "This offer was already approved and is queued.",
        states.PUBLISHING: "This offer is already publishing.",
        states.PUBLISHED: "This offer was already published.",
        states.PARTIAL_FAILURE: "This offer was already published (some posts failed).",
        states.FAILED: "This offer already finished.",
        states.CANCELLED: "This offer was cancelled.",
        states.EXPIRED: "This offer expired. Ask for a new one from the app.",
    }.get(state, f"This offer is {state.lower().replace('_', ' ')}.")


# ---- approve -----------------------------------------------------------------------


def _approve(db, review: dict, command: dict, now) -> Outcome:
    if command.get("via") == "muse":
        return Outcome("rejected", "An assistant can't approve publishing. Approve on the approval page with your PIN.")
    config = ra_settings.load(db)
    if not config["remote_approvals"]:
        return Outcome("rejected", "Remote approval is switched off on this PC.")
    if review["state"] not in states.OPEN:
        return Outcome("ignored", _already(review["state"]))
    if str(command.get("fingerprint") or "") != review["fingerprint"]:
        return Outcome("rejected", "These are not the clips that were offered. Ask for a new offer.")

    decided = store.parse(command.get("decided_at") or "")
    expires = store.parse(review["expires_at"])
    if decided is None or expires is None:
        return Outcome("rejected", "The approval has no time on it.")
    if decided > expires:
        store.move(db, review["id"], states.EXPIRED, actor="desktop", detail="approved after expiry")
        return Outcome("rejected", "The link had expired. Ask for a new offer.")
    if now - decided > MAX_COMMAND_AGE:
        store.move(db, review["id"], states.EXPIRED, actor="desktop", detail="approval reached this PC too late")
        return Outcome("rejected", "This approval is too old to act on. Ask for a new offer.")

    offered = {c["clip_id"]: c for c in review["clips"]}
    chosen = _ints(command.get("clips"))
    if not chosen:
        return Outcome("rejected", "No clips were chosen.")
    if any(c not in offered for c in chosen):
        return Outcome("rejected", "A chosen clip was not in this offer.")
    platforms = [p for p in ra_settings.PLATFORMS if p in (command.get("platforms") or [])]
    if not platforms or len(platforms) != len(set(command.get("platforms") or [])):
        return Outcome("rejected", "The destinations were not ones Clips Kitty can publish to.")
    connected = config.get("accounts") or {}
    missing = [p for p in platforms if not (connected.get(p) or {}).get("connected")]
    if missing:
        return Outcome("rejected", f"{', '.join(ra_settings.PLATFORM_LABELS[p] for p in missing)} "
                                   "is not connected on this PC.")
    changed = [c for c in chosen if not summary.file_matches(offered[c])]
    if changed:
        return Outcome("rejected", "A clip changed after the message was sent, so this approval no longer "
                                   "covers it. Ask for a new offer.")

    decision = {
        "command_id": str(command["id"]),
        "clips": chosen,
        "platforms": platforms,
        "decided_at": store.stamp(decided),
        "via": "whatsapp",
    }
    store.move(db, review["id"], states.APPROVED, actor="phone", detail=_describe(chosen, platforms),
               decision=decision)
    queued = queue_publishes(db, review, decision)
    if not queued:
        why = "Every chosen clip is already posted, or on its way, where you chose."
        store.move(db, review["id"], states.FAILED, actor="desktop", detail="nothing could be queued", error=why)
        return Outcome("rejected", why)
    store.move(db, review["id"], states.PUBLISH_QUEUED, actor="desktop", detail=f"{queued} upload(s) queued")
    return Outcome("accepted", f"{queued} clip(s) queued for {', '.join(platforms)}.", queued=queued)


def queue_publishes(db, review: dict, decision: dict, *, only: dict | None = None) -> int:
    """One publish job per clip, carrying its platforms. Through the same
    publish_jobs queue as every upload, so its crash rule holds: an upload a
    restart cut off is marked interrupted and never sent twice.

    `only` ({clip_id: [platforms]}) narrows a local retry to what failed: a
    clip it does not name is not sent again (an upload a restart cut off is
    one, and it may already be up).

    Never a second post: a platform the clip is already published on, or on
    its way to (scheduled elsewhere, or uploading), by any provider, is left
    out of its job (store.sent_platforms)."""
    offered = {c["clip_id"]: c for c in review["clips"]}
    queued = 0
    for clip_id in decision["clips"]:
        wanted = decision["platforms"] if only is None else only.get(clip_id, [])
        done = store.sent_platforms(db, clip_id)
        platforms = [p for p in wanted if p not in done]
        if not platforms or clip_id not in offered:
            continue
        if db.active_publish_job_for_clip(clip_id) is not None:
            continue
        entry = offered[clip_id]
        request = {
            "provider": "meta",
            "review_id": review["id"],
            "approval_id": review["approval_id"],
            "command_id": decision["command_id"],
            "platforms": platforms,
        }
        db.add_publish_job(clip_id, json.dumps(request), video_id=review["video_id"],
                           start_s=entry["start_s"], end_s=entry["end_s"])
        queued += 1
    return queued


def _describe(clips: list[int], platforms: list[str]) -> str:
    n = len(clips)
    return f"{n} clip{'s' if n != 1 else ''} to {', '.join(platforms)}"


def _ints(values) -> list[int]:
    out = []
    for v in values or []:
        try:
            n = int(v)
        except (TypeError, ValueError):
            return []
        if n not in out:
            out.append(n)
    return out


# ---- everything else -----------------------------------------------------------


def _cancel(db, review: dict, command: dict, now) -> Outcome:
    actor = _actor("cancel", str(command.get("via") or ""))
    if review["state"] == states.PUBLISH_QUEUED:
        stopped = cancel_queued(db, review["id"])
        if stopped and not running_jobs(db, review["id"]):
            store.move(db, review["id"], states.CANCELLED, actor=actor, detail=f"{stopped} upload(s) stopped")
            return Outcome("accepted", "Cancelled before anything was posted.")
        return Outcome("rejected", "Publishing has already started, so it can't be cancelled from the phone.")
    store.move(db, review["id"], states.CANCELLED, actor=actor)
    return Outcome("accepted", "Cancelled. Nothing was posted.")


def _defer(db, review: dict, command: dict, now) -> Outcome:
    remind_at = store.parse(command.get("remind_at") or "")
    store.move(db, review["id"], states.DEFERRED, actor=_actor("defer", str(command.get("via") or "")),
               remind_at=store.stamp(remind_at) if remind_at else "")
    return Outcome("accepted", "Reminder set.")


def _reminded(db, review: dict, command: dict, now) -> Outcome:
    store.move(db, review["id"], states.AWAITING_APPROVAL, actor="relay", detail="reminder sent", remind_at="")
    return Outcome("accepted")


def _delivery(db, review: dict, command: dict, now) -> Outcome:
    status = str(command.get("status") or "")
    if status in ("delivered", "read", "opened"):
        if review["state"] == states.NOTIFICATION_SENT:
            store.move(db, review["id"], states.AWAITING_APPROVAL, actor="relay", detail=status)
        return Outcome("accepted")
    if status == "failed":
        store.move(db, review["id"], states.NOTIFICATION_PENDING, actor="relay", detail="WhatsApp could not deliver",
                   error=str(command.get("error") or "WhatsApp could not deliver the message.")[:300])
        rows = store.outbox_for_review(db, review["id"])
        for row in rows:
            if row["kind"] == "ready":
                store.outbox_done(db, row["id"], "failed", "WhatsApp could not deliver the message.")
        return Outcome("accepted")
    return Outcome("ignored", f"Unknown delivery status {status!r}.")


def _expired(db, review: dict, command: dict, now) -> Outcome:
    store.move(db, review["id"], states.EXPIRED, actor="relay")
    return Outcome("accepted")


# ---- publish jobs belonging to an offer -----------------------------------------


def jobs_for_review(db, review_id: int) -> list:
    return db.conn.execute(
        "SELECT * FROM publish_jobs WHERE json_extract(request, '$.provider') = 'meta' "
        "AND json_extract(request, '$.review_id') = ? ORDER BY id",
        (int(review_id),),
    ).fetchall()


def running_jobs(db, review_id: int) -> list:
    return [j for j in jobs_for_review(db, review_id) if j["status"] == "running"]


def cancel_queued(db, review_id: int) -> int:
    stopped = 0
    for job in jobs_for_review(db, review_id):
        if job["status"] == "queued":
            db.finish_publish_job(job["id"], "cancelled", error="Cancelled from the phone.")
            stopped += 1
    return stopped
