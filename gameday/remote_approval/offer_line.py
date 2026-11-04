"""Each offer's one-line summary, written by Muse Spark.

An offer's summary_text says in one line what its clips are. The relay keeps
it with the offer (it cuts it at 300 characters) and shows it in three
places: Muse says it when it lists what's ready and when it asks which offer
to give the link for (meta-relay/lib/muse.js), and the approval page shows
it under the video's title (meta-relay/lib/page.js). The phone alert for a
game that's ready says it too (gameday/notify.py, from the worker).

Whenever an offer is made of the owner's clips and there is a Meta key,
Muse Spark writes that line: one small request per offer (MetaAPI.chat_json,
purpose "offer_line"; MetaClient asks Muse Spark at low reasoning effort),
told only how many clips there are, the counts, and each clip's title and
kind of play, best first. Never the commentary, a player or team field, the
owner's "Teams or players" or the credit line. The line is kept on the
offer, so a resend sends the same words and an offer of the same clips
reuses it. The call is on the usage ledger with the job's other calls, in
its Captions stage.

What comes back is cleaned (one line, with no tabs or runs of spaces; emoji
are dropped; no full stop at its end, as Muse's list puts one after it) and
checked. A line that is empty, longer than MAX_UNITS, or holds a link, a
hashtag or a number that neither the counts nor a clip's title holds is not
used. Then, as with no key or when Muse Spark fails, the offer carries the
counted summary (summary.ready_text). So an offer never waits on a model,
and never says "3 dunks" when there were two.

A game uploaded with the judge login is never offered (notifier.py); this
checks again, and never asks for its line.
"""

import json
import re
import unicodedata

from gameday import access
from gameday.remote_approval import store, summary
from gameday.remote_approval.captions import offer_kind, units
from gameday.types import MODEL_SPARK

PURPOSE = "offer_line"
#: Where the line is kept, in the offer's summary.
KEY = "offer_line"
#: The longest line used, counted as the relay and Meta count (the relay keeps 300).
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
    "You write the one-line summary of the highlight clips just cut from {what}, for {who}. It is shown "
    "under the video's title on the page where they approve the clips, their assistant gives it when they "
    "ask what's ready, and it is sent to their phone as an alert. Write only that line: friendly, and "
    "specific to the best {plays} below (each clip's title, and its kind of {play}), at most {limit} "
    "characters, on one line, with no full stop at the end. Use ONLY what is given: never invent a score, a "
    "statistic, a result, a team or a name. Name a player or a team only if a clip's title names them. If "
    "you give a number, it must be one of the counts given. No hashtags, no links, no emojis. Answer with "
    "JSON matching the schema."
)

#: The instructions for each kind of offer (summary.kind_of).
INSTRUCTIONS_FOR = {
    "basketball": _TELL.format(what="a basketball game", who="a basketball highlights creator", plays="plays",
                               play="play", limit=MAX_UNITS),
    "video": _TELL.format(what="a video", who="a creator", plays="moments", play="moment", limit=MAX_UNITS),
}

_LINK = re.compile(r"(?i)://|\bwww\.|\b[a-z0-9-]+\.(?:com|net|org|io|ly|me|app|co|tv|gg|ai)\b")
_HASHTAG = re.compile(r"#\w")
#: Not drawn: the joiner, the variation selectors and the keycap that make emoji up.
_EMOJI_PARTS = frozenset("\N{ZERO WIDTH JOINER}\N{VARIATION SELECTOR-15}\N{VARIATION SELECTOR-16}"
                         "\N{COMBINING ENCLOSING KEYCAP}")
#: Quotes around the whole line: straight, and curly.
_QUOTES = (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"))


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
    """One line: control characters and every run of whitespace become one
    space, emoji are dropped, quotes around the whole line come off, and so
    does a full stop at its end (Muse's list puts one after it)."""
    kept = []
    for ch in str(text or ""):
        group = unicodedata.category(ch)
        if group in ("So", "Cf", "Cs", "Co", "Cn") or ch in _EMOJI_PARTS or 0x1F3FB <= ord(ch) <= 0x1F3FF:
            continue  # pictographs and emoji, invisible marks, and the skin tones
        kept.append(" " if group == "Cc" else ch)
    line = re.sub(r"\s+", " ", "".join(kept)).strip()
    for left, right in _QUOTES:
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
    """Why this line can't be the offer's summary, or "" when it can."""
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
    got = (review.get("summary") or {}).get(KEY)
    if not isinstance(got, dict) or got.get("fingerprint") != review.get("fingerprint"):
        return ""
    return str(got.get("line") or "")


def summary_text(review: dict) -> str:
    """The offer's summary_text, what Muse says and the approval page shows:
    Muse Spark's line, or else the counted summary."""
    return line_of(review) or summary.ready_text(review["summary"])


def _keep(db, review: dict, line: str) -> None:
    kept = {"line": line, "fingerprint": review["fingerprint"], "model": MODEL_SPARK}
    store.update_review(db, review["id"], summary={**(review.get("summary") or {}), KEY: kept})


def _reuse(db, review: dict) -> bool:
    """A line already written for exactly these clips, on an earlier offer of the same job."""
    for other in store.reviews_for_job(db, review["job_id"]):
        line = line_of(other) if other["id"] != review["id"] and other["fingerprint"] == review["fingerprint"] else ""
        if line:
            _keep(db, review, line)
            store.audit(db, review["id"], "desktop", "muse_spark_offer_line", f"the line of offer {other['id']}")
            return True
    return False


def _skipped(db, review_id: int, why: str) -> str:
    why = str(why or "Muse Spark was not available.")[:200]
    store.audit(db, review_id, "desktop", "offer_line_skipped", why)
    return why


def write(db, review_id: int, meta) -> str:
    """Muse Spark's line for this offer, kept on it. "" when it was written
    (or one already was), else why not: the offer then carries the counted
    summary."""
    review = store.get_review(db, review_id)
    if review is None:
        return "The offer is gone."
    if access.made_by_judge(db.get_job(review["job_id"]) or {}):
        return access.NOT_OFFERED
    if line_of(review) or _reuse(db, review):
        return ""
    messages = [
        {"role": "system", "content": INSTRUCTIONS_FOR[offer_kind(review)]},
        {"role": "user", "content": json.dumps(facts(review), ensure_ascii=False)},
    ]
    try:
        answer = meta.chat_json(messages, SCHEMA, name="clips_kitty_offer_line", purpose=PURPOSE,
                                job_id=review.get("job_id"), max_tokens=MAX_TOKENS, temperature=0.5)
    except Exception as e:  # the counted summary goes instead: an offer never waits on a model
        return _skipped(db, review_id, str(e))
    line = clean(answer.get("line") if isinstance(answer, dict) else "")
    why = problem(line, review)
    if why:
        return _skipped(db, review_id, why)
    _keep(db, review, line)
    store.audit(db, review_id, "desktop", "muse_spark_offer_line", f"{units(line)} characters, {MODEL_SPARK}")
    return ""
