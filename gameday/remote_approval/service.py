"""The remote approval loop, and the actions the dashboard takes.

Ported from this week's event branch. tick() runs on the relay loop
(gameday/worker.py), between uploads. Each pass, when one is due:

1. offers whose link has expired become EXPIRED;
2. offers whose uploads have all finished are settled (PUBLISHED,
   PARTIAL_FAILURE or FAILED, from what the APIs confirmed) and their
   result is queued for the relay, which keeps it for the status page and
   Muse, and sends it on WhatsApp when that is wanted (the worker also
   sends a phone alert for each, gameday/notify.py);
3. the outbox is sent to the relay, oldest first, each message once (the
   relay keys on the same event key, so a retry after a lost answer is not
   a second message);
4. the relay is asked for decisions made on the phone (or asked of Muse);
   each is checked (commands.py) and the relay told what became of it;
5. at most one offered clip's preview is made and sent, for the approval
   page to play (previews.py). Last, so a decision already waiting is
   handled first, and not in a pass that queued uploads, which this thread
   runs.

Pacing: every few seconds while something is open (an offer waiting, a
message queued, an upload in flight), every couple of minutes otherwise,
never while neither WhatsApp nor Muse is on. All of it is outbound.

If the app was off, nothing is lost: the relay keeps the decision, the
approval page says "waiting for your PC", and the first pass after start
picks it up. An approval is honoured if it was made before the link expired
and is not stale (commands.MAX_COMMAND_AGE); otherwise the owner is asked
again rather than having it posted late.
"""

import base64
import io
import logging
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from gameday import media
from gameday.remote_approval import commands, previews, states, store, summary, usage
from gameday.remote_approval import settings as ra_settings
from gameday.remote_approval.relay import Relay, RelayError
from gameday.store import media_url

log = logging.getLogger("gameday")

FAST_SECONDS = 5
SLOW_SECONDS = 120
STATUS_SECONDS = 600


@dataclass
class TickState:
    next_pass: float = 0.0
    next_status: float = 0.0
    last_error: str = ""
    warned: set = field(default_factory=set)


def client(db) -> Relay | None:
    config = ra_settings.load(db)
    device = ra_settings.device(db)
    if not config["relay_url"] or not device:
        return None
    return Relay(config["relay_url"], **device)


def busy(db) -> bool:
    open_states = [*states.OPEN, states.NOTIFICATION_PENDING, states.APPROVED, states.PUBLISH_QUEUED,
                   states.PUBLISHING]
    # A preview never keeps the pace up: it only goes while its offer is
    # open, and an open offer does that already.
    return bool(store.reviews_in(db, open_states)) or store.pending_outbox(db, skip=previews.KIND) > 0


def tick(db, state: TickState, *, relay: Relay | None = None, emit=None, clock=time.monotonic,
         now=None, on_settled=None) -> int:
    """One pass, if one is due. Returns how many uploads it queued.
    `on_settled(review)` hears of each offer whose uploads settled (the
    worker's phone alert, gameday/notify.py)."""
    config = ra_settings.load(db)
    if not (config["whatsapp_enabled"] or ra_settings.muse_connected(config)):
        # The phone can't be reached, but what is only local still moves on: links
        # expire, and uploads already approved (they run all the same) settle.
        changed = expire_due(db, now or store.now_utc()) + settle_publishing(db, config, on_settled=on_settled)
        if changed and emit:
            emit({"type": "remote_approval", "changed": changed})
        return 0
    mono = clock()
    # A message waiting to go (a job just finished) doesn't wait for the
    # slow pace: the owner hears about it within seconds. A preview waits
    # for the next pass, whatever the pace.
    if mono < state.next_pass and not store.due_outbox(db, now, limit=1, skip=previews.KIND):
        return 0
    state.next_pass = mono + (FAST_SECONDS if busy(db) else SLOW_SECONDS)
    relay = relay or client(db)
    if relay is None:
        return 0
    now = now or store.now_utc()
    changed = expire_due(db, now) + settle_publishing(db, config, on_settled=on_settled)
    queued = 0
    try:
        if mono >= state.next_status:
            state.next_status = mono + STATUS_SECONDS
            refresh_status(db, relay)
        changed += flush_outbox(db, relay, now=now)
        queued, handled = poll_commands(db, relay, now=now)
        changed += handled
        # Uploads this pass queued start first: this thread runs them.
        previews.send_one(db, relay, encode=not queued)
        state.last_error = ""
    except RelayError as e:
        if e.message != state.last_error:
            log.warning("Relay: %s", e.message)
            if emit:
                emit({"type": "relay_error", "message": e.message})
        state.last_error = e.message
    if changed and emit:
        emit({"type": "remote_approval", "changed": changed})
    return queued


# ---- 1. expiry ---------------------------------------------------------------------


def expire_due(db, now) -> int:
    moved = 0
    for review in store.reviews_in(db, [states.NOTIFICATION_PENDING, *states.OPEN]):
        expires = store.parse(review["expires_at"])
        if expires is not None and now > expires:
            try:
                store.move(db, review["id"], states.EXPIRED, actor="desktop", detail="the link expired")
                moved += 1
            except states.IllegalMove:
                # It was decided or cancelled since the list was read.
                pass
    return moved


# ---- 2. settling what was published -------------------------------------------------


def settle_publishing(db, config: dict, *, on_settled=None) -> int:
    settled = 0
    for review in store.reviews_in(db, [states.PUBLISH_QUEUED, states.PUBLISHING]):
        jobs = commands.jobs_for_review(db, review["id"])
        if not jobs:
            continue
        if review["state"] == states.PUBLISH_QUEUED and any(j["status"] != "queued" for j in jobs):
            review = store.move(db, review["id"], states.PUBLISHING, actor="publisher")
        if any(j["status"] in ("queued", "running") for j in jobs):
            continue
        results = results_for(db, review, jobs)
        totals = results["totals"]
        if totals["published"] and not totals["failed"] and not totals["unconfirmed"]:
            final = states.PUBLISHED
        elif totals["published"]:
            final = states.PARTIAL_FAILURE
        else:
            final = states.FAILED
        review = store.move(db, review["id"], final, actor="publisher", results=results,
                            detail=f"{totals['published']} published, {totals['failed']} failed, "
                                   f"{totals['unconfirmed']} unconfirmed")
        # Always sent, so the status page and Muse ("what got posted?") have
        # it. The relay keeps it first and messages WhatsApp only when
        # notify_on_publish is on (push_preferences tells it).
        nth = len([r for r in store.audit_log(db, review["id"], 500) if r["action"] in
                   (states.PUBLISHED.lower(), states.PARTIAL_FAILURE.lower(), states.FAILED.lower())])
        store.enqueue(db, f"result:review:{review['id']}:{nth}", "result", review_id=review["id"])
        settled += 1
        if on_settled is not None:
            try:
                on_settled(review)
            except Exception as e:  # the alert is extra: the offer has settled whatever it does
                log.warning("Couldn't tell the worker an offer settled (%s).", type(e).__name__)
    return settled


def results_for(db, review: dict, jobs) -> dict:
    """Per clip and platform, as the APIs confirmed: published (with the
    link the API returned), failed (with why), unconfirmed (processing, or
    cut off by a restart: the owner is told to check), or skipped (already
    posted there, or on its way, so it was not sent again)."""
    clips_out = []
    totals = {"published": 0, "failed": 0, "unconfirmed": 0, "skipped": 0}
    by_clip = {j["clip_id"]: j for j in jobs}
    platforms = review["decision"].get("platforms") or []
    for clip_id in review["decision"].get("clips") or []:
        job = by_clip.get(clip_id)
        rows = {r["platform"]: r for r in db.clip_publishes(clip_id) if r["provider"] == "meta"}
        # Posted there, or on its way, by something other than this approval
        # (an earlier offer): commands.queue_publishes left those platforms out.
        before = store.sent_platforms(db, clip_id, but_not=review["approval_id"])
        entry = next((c for c in review["clips"] if c["clip_id"] == clip_id), {})
        posts = []
        for platform in platforms:
            row = rows.get(platform)
            if platform in before and not (row is not None and row["request_id"] == review["approval_id"]):
                totals["skipped"] += 1
                posts.append({"platform": platform, "state": "skipped", "url": "",
                              "error": "Already posted there, so it was not posted again."
                              if before[platform] == "published"
                              else "Already on its way there, so it was not posted again."})
                continue
            state = row["state"] if row else "failed"
            error = row["error"] if row else (job["error"] if job else "") or "Not posted."
            if job is not None and job["status"] == "interrupted" and state == "processing":
                state = "unconfirmed"
                error = "The app restarted while this was uploading. Check the account before trying again."
            elif state == "processing":
                state = "unconfirmed"
            elif job is not None and job["status"] == "cancelled" and state != "published":
                state = "failed"
                error = job["error"] or "Cancelled."
            key = {"published": "published", "skipped": "skipped", "unconfirmed": "unconfirmed"}.get(state, "failed")
            totals[key] += 1
            posts.append({
                "platform": platform,
                "state": key,
                "url": row["post_url"] if row and key == "published" else "",
                "error": "" if key == "published" else str(error)[:200],
            })
        clips_out.append({"clip_id": clip_id, "title": entry.get("title") or entry.get("label") or "",
                          "posts": posts})
    return {"clips": clips_out, "totals": totals}


def result_text(results: dict) -> str:
    """"3 of 4 posts published (1 failed)": counted from confirmed results
    only, for the WhatsApp confirmation. No links in it: those are on the
    status page, exactly as the APIs returned them."""
    t = results.get("totals") or {}
    done = int(t.get("published") or 0)
    total = sum(int(t.get(k) or 0) for k in ("published", "failed", "unconfirmed", "skipped"))
    extra = []
    if t.get("failed"):
        extra.append(f"{t['failed']} failed")
    if t.get("unconfirmed"):
        extra.append(f"{t['unconfirmed']} not confirmed yet")
    if t.get("skipped"):
        extra.append(f"{t['skipped']} skipped")
    text = f"{done} of {total} post{'s' if total != 1 else ''} published"
    return text + (f" ({', '.join(extra)})" if extra else "")


# ---- 3. the outbox ----------------------------------------------------------------------


#: Relay answers that mean "this will not work by waiting": the message is
#: marked failed at once, with the relay's own words, for the dashboard.
PERMANENT = {"not_paired", "not_confirmed", "opted_out", "template_missing", "template_not_approved",
             "bad_request", "not_registered", "forbidden", "no_channel"}

#: Refusals told in this app's words instead of the relay's: the relay is
#: shared with the desktop edition, and its text for these names settings
#: this dashboard doesn't have.
APP_WORDS = {
    # The relay saw no active Muse key for this app when the offer went out.
    "no_channel": "The relay has no Muse key for this app. Make a Muse key in Setup, then Send again.",
}


def flush_outbox(db, relay: Relay, *, now=None) -> int:
    sent = 0
    # Previews are not messages: previews.send_one sends them, one a pass.
    for item in store.due_outbox(db, now, skip=previews.KIND):
        try:
            body = build_body(db, item)
        except _Drop as e:
            store.outbox_done(db, item["id"], "dropped", str(e))
            continue
        try:
            answer = relay.notify(body)
        except RelayError as e:
            if e.code in PERMANENT:
                why = APP_WORDS.get(e.code, e.message)
                store.outbox_done(db, item["id"], "failed", why)
                if item["review_id"]:
                    store.update_review(db, item["review_id"], error=why)
                    store.audit(db, item["review_id"], "relay", "notify_failed", why)
                continue
            store.outbox_retry(db, item, e.message, now)
            if not e.retryable:
                continue
            raise
        store.outbox_done(db, item["id"], "sent")
        usage.record_whatsapp(db, str(answer.get("channel") or ""))
        if item["kind"] == "ready":
            _sent_offer(db, item["review_id"], answer)
        sent += 1
    return sent


class _Drop(Exception):
    """This message no longer has anything to say (its offer was cancelled)."""


def build_body(db, item: dict) -> dict:
    """What the relay is sent for one outbox row. The captions were written
    when the offer was made (notifier.offer); this never calls a model."""
    from gameday.remote_approval import captions

    config = ra_settings.load(db)
    kind = item["kind"]
    base = {"kind": kind, "event_key": item["event_key"]}
    if kind in ("failed", "test"):
        return base
    review = store.get_review(db, item["review_id"])
    if review is None:
        raise _Drop("its offer is gone")
    if kind == "result":
        if not review["approval_id"]:
            raise _Drop("the offer was never sent")
        return {**base, "approval_id": review["approval_id"], "state": review["state"],
                "text": result_text(review["results"]), "results": review["results"]}
    if review["state"] != states.NOTIFICATION_PENDING:
        raise _Drop(f"the offer is {review['state'].lower()}")
    clip_rows = {c["clip_id"]: db.get_clip(c["clip_id"]) for c in review["clips"]}
    clips = []
    for entry in review["clips"]:
        text = captions.texts_for(review, clip_rows.get(entry["clip_id"]), entry)
        clips.append({
            "clip_id": entry["clip_id"],
            "title": text["title"][:100],
            "label": entry.get("label") or "",
            "category": entry.get("category") or "",
            "duration": entry.get("duration") or 0,
            "texts": {"instagram": text["instagram"][:2200], "facebook": text["facebook"][:2000]},
            "text_source": text["source"],
            "thumb": thumbnail(entry, clip_rows.get(entry["clip_id"])),
            # Where it is already posted: the page says so, and it is not posted there again.
            "published": list(entry.get("published") or []),
        })
    muse_line = (review.get("captions") or {}).get("summary", "") if config["muse_summary"] else ""
    return {
        **base,
        "review_ref": review["id"],
        "fingerprint": review["fingerprint"],
        "expires_at": review["expires_at"],
        "title": review["title"],
        "summary_text": summary.ready_text(review["summary"]),
        # basketball or video: for Muse to say what kind of video it was.
        "video_kind": review["summary"].get("kind") or "",
        "counts": review["summary"].get("counts") or {},
        "muse_line": muse_line,
        "remote_approvals": bool(config["remote_approvals"]),
        "preferred": config["preferred_destinations"],
        "remind_minutes": int(config["remind_minutes"]),
        # This app sends previews of the clips: a relay that keeps them says so.
        "previews": True,
        "clips": clips,
    }


def _sent_offer(db, review_id: int, answer: dict) -> None:
    approval_id = str(answer.get("approval_id") or "")
    try:
        review = store.move(db, review_id, states.NOTIFICATION_SENT, actor="relay",
                            detail=f"sent as {answer.get('channel') or 'message'}", approval_id=approval_id,
                            error="")
    except states.IllegalMove:
        # The offer moved on (cancelled, or answered) while the message went out.
        return
    if answer.get("previews") is True:
        # The relay keeps previews (an older one doesn't say): each clip's
        # goes up after this, one a pass.
        previews.queue(db, review, approval_id)


#: The relay keeps a thumbnail of at most this much base64 (meta-relay/worker.js).
THUMB_MAX_BYTES = 60_000


def thumbnail(entry: dict, clip_row=None) -> str:
    """A small JPEG of the clip, base64, for the approval page: the engine's
    own thumbnail made small, or else a frame from the middle of the clip.
    "" when neither can be made: the page shows the play's name."""
    data = _small_thumb(str(clip_row["thumb"] or "")) if clip_row is not None else b""
    if not data:
        data = _frame(entry)
    return base64.b64encode(data).decode("ascii") if 0 < len(data) <= THUMB_MAX_BYTES else ""


def _small_thumb(path: str) -> bytes:
    if not path or not Path(path).is_file():
        return b""
    try:
        from PIL import Image

        with Image.open(path) as image:
            image = image.convert("RGB")
            image.thumbnail((240, 480))
            out = io.BytesIO()
            image.save(out, "JPEG", quality=70)
            return out.getvalue()
    except Exception:
        return b""


def _frame(entry: dict) -> bytes:
    try:
        with tempfile.TemporaryDirectory(prefix="gameday-thumb-") as tmp:
            out = Path(tmp) / "t.jpg"
            subprocess.run(
                [media.FFMPEG, "-v", "error", "-y", "-ss",
                 f"{max(0.0, float(entry.get('duration') or 0) / 2):.2f}",
                 "-i", entry["path"], "-frames:v", "1", "-vf", "scale=240:-2", "-q:v", "7", str(out)],
                capture_output=True, timeout=20,
            )
            return out.read_bytes() if out.exists() else b""
    except Exception:
        return b""


# ---- 4. decisions from the phone --------------------------------------------------------


def poll_commands(db, relay: Relay, *, now=None) -> tuple[int, int]:
    device = ra_settings.device(db)
    got = relay.commands()
    if any(c.get("kind") == "approve" for c in got):
        # The relay is the authority on what is connected; read it fresh
        # before checking an approval against it.
        refresh_status(db, relay)
    queued = handled = 0
    for command in got:
        result = commands.handle(db, command, secret=device.get("device_secret", ""), now=now)
        relay.ack(str(command.get("id") or ""), result.outcome, result.reason)
        queued += result.queued
        handled += 1
    return queued, handled


def refresh_status(db, relay: Relay) -> dict:
    status = relay.status()
    ra_settings.save(db, {
        "pairing": status.get("pairing") or {},
        "pin_set": bool(status.get("pin_set")),
        "accounts": status.get("accounts") or {},
        "templates": status.get("templates") or {},
        "muse": _muse_state(status.get("muse")),
    }, internal=True)
    return status


def _muse_state(block) -> dict:
    """The relay's word on this app's Muse key, in the only fields kept:
    whether one works, and when it was made and last used. Never the key."""
    block = block if isinstance(block, dict) else {}
    return {"enabled": bool(block.get("enabled")), "created_at": int(block.get("created_at") or 0),
            "last_used": int(block.get("last_used") or 0)}


# ---- actions from the dashboard ----------------------------------------------------------


def register(db, enroll_key: str) -> dict:
    """Make this app's device secret and register it with the relay, with the
    relay owner's enrolment key. The key is used once and not kept."""
    config = ra_settings.load(db)
    if not config["relay_url"]:
        raise RelayError("bad_request", "Enter your relay's address first.")
    fresh = not ra_settings.device(db)
    device = ra_settings.device(db) or ra_settings.new_device(db)
    relay = Relay(config["relay_url"], **device)
    try:
        relay.register(enroll_key)
    except RelayError:
        if fresh:  # a secret the relay never accepted is not kept
            ra_settings.forget_device(db)
        raise
    push_preferences(db, relay)
    return refresh_status(db, relay)


def push_preferences(db, relay: Relay) -> None:
    """What the relay needs to know to answer the phone on its own: whether
    WhatsApp is wanted at all (off, offers are only kept for Muse), which
    messages are wanted, whether approving is allowed, the destinations to
    tick in advance, and how long "Remind me later" waits."""
    config = ra_settings.load(db)
    relay.preferences({
        "whatsapp_enabled": bool(config["whatsapp_enabled"]),
        "notify_on_finish": bool(config["notify_on_finish"]),
        "notify_on_fail": bool(config["notify_on_fail"]),
        "notify_on_publish": bool(config["notify_on_publish"]),
        "remote_approvals": bool(config["remote_approvals"]),
        "preferred": list(config["preferred_destinations"]),
        "remind_minutes": int(config["remind_minutes"]),
    })


def can_enable_remote_approval(db) -> str:
    """"" when remote approval may be switched on, else why not. A connected
    Muse agent stands in for a paired WhatsApp number: either way the offer
    reaches the owner's phone, and the PIN is still asked on the page."""
    config = ra_settings.load(db)
    if not ra_settings.muse_connected(config):
        # A paired, confirmed WhatsApp number still counts here, but the
        # dashboard doesn't offer WhatsApp, so the note asks for Muse only.
        if not config["whatsapp_enabled"] or (config["pairing"] or {}).get("state") != "confirmed":
            return "Make a Muse key first, so your Muse agent can hand you the approval link."
    if not config["pin_set"]:
        return "Set an approval PIN first. The approval page asks for it before anything is published."
    return ""


def make_muse_key(db, relay: Relay) -> str:
    """A new key for the owner's Muse agent, replacing any older one. It is
    returned for the dashboard to show once and is not kept, logged or queued
    anywhere in this app: the relay keeps its hash, Muse keeps the key."""
    got = relay.muse_token()
    ra_settings.save(db, {"muse": _muse_state(got.get("muse"))}, internal=True)
    return str(got.get("key") or "")


def revoke_muse_key(db, relay: Relay) -> None:
    """Stop the Muse key working. With no paired WhatsApp number left either,
    approving from the phone switches off, as unpairing does: nothing can
    reach the owner any more."""
    got = relay.muse_revoke()
    ra_settings.save(db, {"muse": _muse_state(got.get("muse"))}, internal=True)
    config = ra_settings.load(db)
    if not (config["whatsapp_enabled"] and (config["pairing"] or {}).get("state") == "confirmed"):
        ra_settings.save(db, {"remote_approvals": False})


def send_test(db) -> bool:
    return store.enqueue(db, f"test:{store.stamp()}:{time.monotonic_ns()}", "test")


def ask_again(db, review_id: int, *, meta=None) -> int | None:
    """A new offer for an expired or cancelled one's job, with a new link:
    that job's clips again, those still on disk and not posted everywhere."""
    from gameday.remote_approval import captions, notifier
    from gameday.store import loads

    old = store.get_review(db, review_id)
    if old is None or old["state"] not in (states.EXPIRED, states.CANCELLED):
        return None
    job = db.get_job(old["job_id"])
    if job is None:
        ids = {c["clip_id"] for c in old["clips"]}
        clips = [c for c in (db.get_clip(i) for i in ids) if c is not None and summary.offerable(c)]
        hint = ""
        own_credit = captions.credit_of(old)  # the game's own line is gone with its job: the offer's
    else:
        clips = notifier.clips_of_job(db, old["job_id"])
        options = loads(job["options"], {})
        hint = str(options.get("hint") or "")
        own_credit = options.get("caption_credit")
    return notifier.offer(db, job_id=old["job_id"], video_id=old["video_id"],
                          title=(notifier.job_title(db, job) if job is not None else "") or old["title"],
                          clips=clips, kind=(old.get("summary") or {}).get("kind") or "basketball",
                          meta=meta, hint=hint, credit=notifier.credit_line(db, own_credit))


def resend(db, review_id: int) -> bool:
    """"Send again" for an offer WhatsApp could not deliver (or the relay
    refused, say before pairing was confirmed). It goes out under a new
    event key: the relay keeps one message per key, and it cancelled the
    old link when delivery failed, so the owner gets a fresh one. The clips
    and the expiry are the offer's own; nothing is re-rendered."""
    review = store.get_review(db, review_id)
    if review is None or review["state"] != states.NOTIFICATION_PENDING:
        return False
    rows = [r for r in store.outbox_for_review(db, review_id) if r["kind"] == "ready"]
    if any(r["status"] == "pending" for r in rows):
        return False
    key = f"ready:review:{review_id}:resend:{len(rows)}"
    if store.enqueue(db, key, "ready", review_id=review_id):
        store.update_review(db, review_id, error="")
        store.audit(db, review_id, "desktop", "resend", "sent again from the app")
        return True
    return False


def cancel_from_app(db, review_id: int) -> dict:
    """Cancel from the dashboard: always allowed before anything is uploading."""
    review = store.get_review(db, review_id)
    if review is None:
        raise KeyError(review_id)
    if review["state"] == states.PUBLISH_QUEUED:
        # Refused before anything changes: an upload already started can't be
        # stopped, and the rest stay queued with it.
        if commands.running_jobs(db, review_id):
            raise states.IllegalMove(review["state"], states.CANCELLED)
        commands.cancel_queued(db, review_id, where="the app")
    review = store.move(db, review_id, states.CANCELLED, actor="desktop", detail="cancelled in the app")
    relay = client(db)
    if relay is not None and review["approval_id"]:
        try:
            relay.cancel(review["approval_id"])
        except RelayError as e:
            store.audit(db, review_id, "relay", "cancel_not_delivered", e.message)
    return review


def retry_failed(db, review_id: int) -> int:
    """Queue again only what failed, under the same approval, from the app.
    The person at the dashboard is already signed in; the phone is not asked."""
    review = store.get_review(db, review_id)
    if review is None or review["state"] not in (states.PARTIAL_FAILURE, states.FAILED):
        return 0
    only: dict[int, list[str]] = {}
    for clip in (review["results"] or {}).get("clips") or []:
        wanted = [p["platform"] for p in clip["posts"] if p["state"] == "failed"]
        if wanted:
            only[clip["clip_id"]] = wanted
    if not only:
        return 0
    queued = commands.queue_publishes(db, review, review["decision"], only=only)
    if queued:
        store.move(db, review_id, states.PUBLISH_QUEUED, actor="desktop", detail=f"retry: {queued} clip(s)")
    return queued


def disconnect(db) -> None:
    """Disconnect from the dashboard: the relay unpairs the WhatsApp number,
    cancels this app's open offers and stops its Muse key working, then this
    app forgets its device secret, so it can't sign anything to the relay
    again until it is connected anew. Open offers here are cancelled. Works
    even when the relay can't be reached: this app stops either way."""
    relay = client(db)
    if relay is not None:
        try:
            relay.disconnect()
        except RelayError as e:
            log.warning("Relay: %s", e.message)
    for review in store.reviews_in(db, [states.READY_FOR_REVIEW, states.NOTIFICATION_PENDING, *states.OPEN]):
        try:
            store.move(db, review["id"], states.CANCELLED, actor="desktop", detail="disconnected in the app")
        except states.IllegalMove:
            # Already past the point where it can be cancelled.
            pass
    db.conn.execute("UPDATE remote_outbox SET status = 'dropped', error = 'disconnected' WHERE status = 'pending'")
    db.conn.commit()
    ra_settings.forget_device(db)
    ra_settings.save(db, {"whatsapp_enabled": False, "remote_approvals": False, "pairing": {}, "pin_set": False,
                          "accounts": {}, "templates": {}, "muse": {}}, internal=True)


def review_view(db, review: dict, *, detail: bool = False) -> dict:
    """One offer as the dashboard shows it: what was offered, where it stands,
    what was confirmed. No file path (a clip's files are /media URLs), no secret."""
    totals = (review.get("results") or {}).get("totals") or {}
    rows = {c["clip_id"]: db.get_clip(c["clip_id"]) for c in review["clips"]}
    out = {
        "id": review["id"],
        "job_id": review["job_id"],
        "title": review["title"],
        "state": review["state"],
        "summary": summary.ready_text(review["summary"]) if review.get("summary") else "",
        "clips": [{"clip_id": c["clip_id"], "title": c.get("title") or c.get("label") or "",
                   "category": c.get("category") or "",
                   "video_url": media_url(db.data_dir, c.get("path")),
                   "thumb_url": media_url(db.data_dir, rows[c["clip_id"]]["thumb"])
                   if rows.get(c["clip_id"]) is not None else None} for c in review["clips"]],
        "decision": {"clips": review["decision"].get("clips") or [],
                     "platforms": review["decision"].get("platforms") or []} if review.get("decision") else {},
        "result_text": result_text(review["results"]) if totals else "",
        "results": review.get("results") or {},
        "error": review.get("error") or "",
        "expires_at": review["expires_at"],
        "remind_at": review.get("remind_at") or "",
        "created_at": review["created_at"],
        "updated_at": review["updated_at"],
        "captions_by": (review.get("captions") or {}).get("model", ""),
        "can_cancel": review["state"] in (*states.OPEN, states.NOTIFICATION_PENDING, states.READY_FOR_REVIEW,
                                          states.PUBLISH_QUEUED),
        "can_retry": review["state"] in (states.PARTIAL_FAILURE, states.FAILED),
        "can_ask_again": review["state"] in (states.EXPIRED, states.CANCELLED),
        "can_resend": review["state"] == states.NOTIFICATION_PENDING and bool(review.get("error")),
    }
    if detail:
        out["audit"] = store.audit_log(db, review["id"], 200)
        out["messages"] = store.outbox_for_review(db, review["id"])
    return out
