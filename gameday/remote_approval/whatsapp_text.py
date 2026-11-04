"""The line WhatsApp's "highlights ready" message carries, written by Muse Spark.

The relay sends an offer's summary_text as the second body parameter of its
ck_highlights_ready template and, inside the 24 hours after the owner last
wrote to the business number (pairing counts), as plain text:

    Clips Kitty finished your video <title>. Ready to review: <line>. Choose
    where to publish them before the link expires.

When the offer goes out on WhatsApp (messages on, a number paired and
confirmed) and there is a Meta key, Muse Spark writes <line>: one small
request per offer (MetaAPI.chat_json, purpose "whatsapp_text"; MetaClient
asks Muse Spark at low reasoning effort), told only how many clips there are,
the counts, and each clip's title and kind of play. Never the commentary, a
player or team field, or the credit line. The line is kept on the offer, so
a resend sends the same words and an offer of the same clips reuses it. The
call is on the usage ledger with the job's other calls, in its Captions
stage.

What comes back is cleaned to WhatsApp's rules for a template parameter (one
line, no tabs, no run of four or more spaces; emoji are dropped) and checked.
A line that is empty, longer than MAX_UNITS, or holds a link, a hashtag or a
number that neither the counts nor a clip's title holds is not used. Then,
as with no key or when Muse Spark fails, the message carries the counted
summary (summary.ready_text). So a message never waits on a model, and never
says "3 dunks" when there were two.

A game uploaded with the judge login is never offered (notifier.py); this
checks again, and never asks for its line.
"""

import json
import re
import unicodedata

from gameday import access
from gameday.remote_approval import store, summary
from gameday.remote_approval.captions import units
from gameday.types import MODEL_SPARK

PURPOSE = "whatsapp_text"
#: The longest line used, counted as the relay and Meta count (it keeps 300).
MAX_UNITS = 200
MAX_TOKENS = 300
#: At most this many clips are described, best first.
MAX_CLIPS = 10

SCHEMA = {
    "type": "object",
    "properties": {"line": {"type": "string"}},
    "required": ["line"],
    "additionalProperties": False,
}

_TELL = (
    "You write one line of a WhatsApp message to {who}. The app sends: \"Clips Kitty finished your video "
    "<title>. Ready to review: <line>. Choose where to publish them before the link expires.\" Write only "
    "<line>: friendly, and specific to the best {plays} below (each clip's title, and its kind of {play}), "
    "at most {limit} characters, on one line, with no full stop at the end. Use ONLY what is given: never "
    "invent a score, a statistic, a result, a team or a name. Name a player or a team only if a clip's "
    "title names them. If you give a number, it must be one of the counts given. No hashtags, no links, "
    "no emojis. Answer with JSON matching the schema."
)

#: The instructions for each kind of offer (summary.kind_of).
INSTRUCTIONS_FOR = {
    "basketball": _TELL.format(who="a basketball highlights creator about a game just clipped", plays="plays",
                               play="play", limit=MAX_UNITS),
    "video": _TELL.format(who="a creator about a video just clipped", plays="moments", play="moment",
                          limit=MAX_UNITS),
}

_LINK = re.compile(r"(?i)://|\bwww\.|\b[a-z0-9-]+\.(?:com|net|org|io|ly|me|app|co|tv|gg|ai)\b")
_HASHTAG = re.compile(r"#\w")
#: Not drawn: the joiner, the variation selectors and the keycap that make emoji up.
_EMOJI_PARTS = frozenset("‍︎️⃣")


def wanted(config: dict) -> bool:
    """Whether an offer made now goes out on WhatsApp: messages on, and a
    number paired and confirmed."""
    return bool(config["whatsapp_enabled"]) and (config["pairing"] or {}).get("state") == "confirmed"


def _kind(review: dict) -> str:
    kind = str((review.get("summary") or {}).get("kind") or "basketball")
    return kind if kind in INSTRUCTIONS_FOR else "video"


def facts(review: dict) -> dict:
    """What Muse Spark is told: how many clips, the counts, and each clip's
    title and kind of play, best first."""
    info = review.get("summary") or {}
    return {
        "clip_count": int(info.get("offered") or len(review["clips"])),
        "counts": str(info.get("counts_text") or ""),
        "clips": [{"title": str(c.get("title") or c.get("label") or ""),
                   "kind": summary.word_for(str(c.get("category") or ""))} for c in review["clips"][:MAX_CLIPS]],
    }


def clean(text) -> str:
    """One line, as a template parameter must be: control characters and
    every run of whitespace become one space, emoji are dropped, quotes
    around the whole line come off, and so does a full stop at its end (the
    message has its own)."""
    kept = []
    for ch in str(text or ""):
        group = unicodedata.category(ch)
        if group in ("So", "Cf", "Cs", "Co", "Cn") or ch in _EMOJI_PARTS or 0x1F3FB <= ord(ch) <= 0x1F3FF:
            continue  # pictographs and emoji, invisible marks, and the skin tones
        kept.append(" " if group == "Cc" else ch)
    line = re.sub(r"\s+", " ", "".join(kept)).strip()
    for left, right in (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")):
        if len(line) > 1 and line.startswith(left) and line.endswith(right):
            line = line[1:-1].strip()
    return line.rstrip(" .!?;:,…").strip()


def _numbers(text) -> set[int]:
    return {int(n) for n in re.findall(r"\d+", str(text or ""))}


def given_numbers(review: dict) -> set[int]:
    """The numbers the line may say: the counts, how many clips, and any a
    clip's title or label holds."""
    info = review.get("summary") or {}
    out = {int(info.get("offered") or 0), int(info.get("total_clips") or 0)}
    for value in (info.get("counts") or {}).values():
        try:
            out.add(int(value))
        except (TypeError, ValueError):
            pass
    for clip in review["clips"]:
        out |= _numbers(clip.get("title")) | _numbers(clip.get("label"))
    return out


def problem(line: str, review: dict) -> str:
    """Why this line can't go in the message, or "" when it can."""
    if not line:
        return "Muse Spark's line was empty."
    if units(line) > MAX_UNITS:
        return f"Muse Spark's line was longer than {MAX_UNITS} characters."
    if _LINK.search(line):
        return "Muse Spark's line held a link."
    if _HASHTAG.search(line):
        return "Muse Spark's line held a hashtag."
    odd = sorted(_numbers(line) - given_numbers(review))
    if odd:
        return f"Muse Spark's line said {odd[0]}, a number the clips don't have."
    return ""


def line_of(review: dict) -> str:
    """The line Muse Spark wrote for exactly these clips, or ""."""
    got = (review.get("summary") or {}).get("whatsapp_text")
    if not isinstance(got, dict) or got.get("fingerprint") != review.get("fingerprint"):
        return ""
    return str(got.get("line") or "")


def summary_text(review: dict) -> str:
    """What the offer's message says after "Ready to review:": Muse Spark's
    line, or else the counted summary."""
    return line_of(review) or summary.ready_text(review["summary"])


def _keep(db, review: dict, line: str) -> None:
    kept = {"line": line, "fingerprint": review["fingerprint"], "model": MODEL_SPARK}
    store.update_review(db, review["id"], summary={**(review.get("summary") or {}), "whatsapp_text": kept})


def _reuse(db, review: dict) -> bool:
    """A line already written for exactly these clips, on an earlier offer of the same job."""
    for other in store.reviews_for_job(db, review["job_id"]):
        line = line_of(other) if other["id"] != review["id"] and other["fingerprint"] == review["fingerprint"] else ""
        if line:
            _keep(db, review, line)
            store.audit(db, review["id"], "desktop", "muse_spark_whatsapp_text", f"the line of offer {other['id']}")
            return True
    return False


def _skipped(db, review_id: int, why: str) -> str:
    why = str(why or "Muse Spark was not available.")[:200]
    store.audit(db, review_id, "desktop", "whatsapp_text_skipped", why)
    return why


def write(db, review_id: int, meta) -> str:
    """Muse Spark's line for this offer's WhatsApp message, kept on it. ""
    when it was written (or one already was), else why not: the message then
    carries the counted summary."""
    review = store.get_review(db, review_id)
    if review is None:
        return "The offer is gone."
    if access.made_by_judge(db.get_job(review["job_id"]) or {}):
        return access.NOT_OFFERED
    if line_of(review) or _reuse(db, review):
        return ""
    messages = [
        {"role": "system", "content": INSTRUCTIONS_FOR[_kind(review)]},
        {"role": "user", "content": json.dumps(facts(review), ensure_ascii=False)},
    ]
    try:
        answer = meta.chat_json(messages, SCHEMA, name="clips_kitty_whatsapp_line", purpose=PURPOSE,
                                job_id=review.get("job_id"), max_tokens=MAX_TOKENS, temperature=0.5)
    except Exception as e:  # the counted summary goes instead: an offer never waits on a model
        return _skipped(db, review_id, str(e))
    line = clean(answer.get("line") if isinstance(answer, dict) else "")
    why = problem(line, review)
    if why:
        return _skipped(db, review_id, why)
    _keep(db, review, line)
    store.audit(db, review_id, "desktop", "muse_spark_whatsapp_text", f"{units(line)} characters, {MODEL_SPARK}")
    return ""
