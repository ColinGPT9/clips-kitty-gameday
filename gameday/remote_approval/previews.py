"""A small video of each offered clip, for the approval page to play.

Ported from this week's event branch. The page shows a still of each clip;
with a preview the owner can watch it before choosing. Right after an offer
is sent, and only when the relay said it keeps previews, service.py queues one
outbox row per clip, best clip first (queue). Each relay pass then makes and
uploads at most one (send_one), after the decisions from the phone are
handled, so a decision already waiting never waits on an encode.

A preview is the rendered file at 360 px wide (make): H.264 Main at no more
than 30 frames a second, mono AAC at 48 kHz, its index at the front so a phone
starts playing before it has the whole file, and never over MAX_BYTES. It goes
up only while its offer is open, under the link that offer was sent with,
from the exact file the offer holds; a row for anything else is dropped
before any encode. A preview that can't be made or sent only leaves its clip
with its thumbnail, and nothing here ever stops the relay loop.
"""

import subprocess
import tempfile
from pathlib import Path

from gameday import media
from gameday.remote_approval import states, store, summary
from gameday.remote_approval.relay import RelayError

KIND = "preview"
#: The relay refuses a bigger preview (meta-relay/worker.js).
MAX_BYTES = 4 * 1024 * 1024
#: Tries of an upload the relay couldn't take for now (no answer, a preview
#: it couldn't keep): the first and two more, with the outbox's waits between.
TRIES = 3
ENCODE_TIMEOUT = 180
#: Below normal on Windows, so a render never waits on a preview.
_PRIORITY = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)


def queue(db, review: dict, approval_id: str) -> int:
    """One outbox row per clip of an offer just sent, in the offer's own
    order, which is best clip first. Returns how many were queued."""
    if not approval_id:
        return 0
    queued = 0
    for entry in review["clips"]:
        clip_id = int(entry["clip_id"])
        queued += store.enqueue(db, f"preview:{approval_id}:{clip_id}", KIND, review_id=review["id"],
                                body={"approval_id": approval_id, "clip_id": clip_id})
    return queued


def make(entry: dict, out: Path) -> bool:
    """Encode the preview of an offer's clip (its snapshot entry) into
    `out`. False, with nothing kept at `out`, when ffmpeg failed, made
    nothing or made more than MAX_BYTES."""
    try:
        # The video's cap in kbit/s: what fits in MAX_BYTES over the clip's
        # length, with five seconds and a fifth to spare and the audio's 64k
        # taken off.
        seconds = float(entry.get("duration") or 0) + 5
        k = max(60, min(800, int(MAX_BYTES * 8 * 0.8 / seconds / 1000) - 64))
        done = subprocess.run(
            [media.FFMPEG, "-nostdin", "-v", "error", "-y", "-i", entry["path"], "-map_metadata", "-1", "-sn",
             "-dn", "-vf", "scale=360:-2", "-fpsmax", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
             "-maxrate", f"{k}k", "-bufsize", f"{2 * k}k", "-profile:v", "main", "-pix_fmt", "yuv420p",
             # 48 kHz: a 96 kHz source would otherwise stay 96 kHz, which
             # phones aren't specified to play.
             "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-ar", "48000", "-movflags", "+faststart", str(out)],
            capture_output=True, timeout=ENCODE_TIMEOUT, creationflags=_PRIORITY,
        )
        if done.returncode == 0 and 0 < out.stat().st_size <= MAX_BYTES:
            return True
    except Exception:
        # No ffmpeg, a file it can't read, or one it couldn't finish in time.
        pass
    try:
        out.unlink(missing_ok=True)
    except OSError:
        # Still held open: it goes with the caller's temporary folder.
        pass
    return False


def send_one(db, relay, *, now=None, encode: bool = True) -> bool:
    """Make and upload one preview, the first due that still has something
    to show, after dropping every due row that doesn't. True when one went
    up. Never raises. With `encode` False (uploads were just queued), it
    only drops."""
    try:
        return _send_one(db, relay, now, encode)
    except Exception:
        # A preview is never worth stopping the loop for: its clip keeps its thumbnail.
        return False


def _send_one(db, relay, now, encode) -> bool:
    found = _first_live(db, now)
    if found is None or not encode:
        return False
    item, entry = found
    with tempfile.TemporaryDirectory(prefix="gameday-preview-") as tmp:
        out = Path(tmp) / "preview.mp4"
        if not make(entry, out):
            store.outbox_done(db, item["id"], "dropped", "the preview could not be made")
            return False
        data = out.read_bytes()
    try:
        relay.upload_preview(item["body"]["approval_id"], entry["clip_id"], data)
    except RelayError as e:
        if e.code == "previews_off":
            # The relay keeps no previews: none of this offer's will go up.
            store.drop_pending(db, item["review_id"], KIND, e.message)
        elif e.retryable:
            store.outbox_retry(db, item, e.message, now, max_attempts=TRIES)
        else:
            store.outbox_done(db, item["id"], "failed", e.message)
        return False
    store.outbox_done(db, item["id"], "sent")
    return True


def _first_live(db, now) -> tuple[dict, dict] | None:
    """The first due row that still has something to show, with its clip's
    entry in the offer. Every due row that hasn't is dropped on the way, all
    of them before any encode."""
    found = None
    reviews: dict[int, dict | None] = {}
    for item in store.due_outbox(db, now, limit=500, kind=KIND):
        if item["review_id"] not in reviews:
            reviews[item["review_id"]] = store.get_review(db, item["review_id"])
        entry, why = _entry(reviews[item["review_id"]], item["body"])
        if why:
            store.outbox_done(db, item["id"], "dropped", why)
        elif found is None:
            found = item, entry
    return found


def _entry(review: dict | None, body: dict) -> tuple[dict | None, str]:
    """This row's clip in its offer, or why there is nothing to show: the
    offer is no longer open, it was sent again under a new link ("Send
    again"), it doesn't hold the clip, or the clip's file changed."""
    if review is None:
        return None, "its offer is gone"
    if review["state"] not in states.OPEN:
        return None, f"the offer is {review['state'].lower()}"
    if body.get("approval_id") != review["approval_id"]:
        return None, "the offer was sent again under a new link"
    entry = next((c for c in review["clips"] if c["clip_id"] == body.get("clip_id")), None)
    if entry is None:
        return None, "the clip is not in the offer"
    if not summary.file_matches(entry):
        return None, "the clip changed after the offer was made"
    return entry, ""
